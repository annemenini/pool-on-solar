"""Read-only status collector (with TTL cache) and HTML renderer for Pool on Solar."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import datetime
from html import escape
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from tesla_fleet import (
    DEFAULT_API_BASE_URL,
    TeslaFleetClient,
    create_token_store,
)


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(',') if item.strip()]


@dataclass
class DashboardConfig:
    tesla_client_id: str = ''
    tesla_token_secret_name: str = ''
    tesla_token_file: str = ''
    tesla_api_base_url: str = DEFAULT_API_BASE_URL
    energy_site_id: str = ''
    iaqualink_user_id: str = ''
    iaqualink_password: str = ''
    pool_devices: List[str] = field(default_factory=lambda: ['pool_pump', 'aux_1'])
    light_devices: List[str] = field(default_factory=lambda: ['aux_B1', 'aux_B3', 'aux_B4'])
    activation_excess_power: int = 2000
    deactivation_excess_power: int = 500
    gcp_project_id: str = ''
    cloud_run_region: str = 'us-west1'
    cloud_run_job_name: str = 'pool-on-solar'
    scheduler_job_name: str = 'pool-on-solar-scheduler-trigger'
    cache_ttl_seconds: int = 60

    @classmethod
    def from_env(cls) -> 'DashboardConfig':
        project_id = (
            os.getenv('GCP_PROJECT_ID')
            or os.getenv('GOOGLE_CLOUD_PROJECT')
            or os.getenv('GCP_PROJECT')
            or ''
        )
        token_secret = os.getenv('TESLA_TOKEN_SECRET_NAME', '')
        if not project_id and token_secret.startswith('projects/'):
            parts = token_secret.split('/')
            if len(parts) >= 2:
                project_id = parts[1]

        return cls(
            tesla_client_id=os.getenv('TESLA_CLIENT_ID', ''),
            tesla_token_secret_name=token_secret,
            tesla_token_file=os.getenv('TESLA_TOKEN_FILE', ''),
            tesla_api_base_url=os.getenv('TESLA_API_BASE_URL', DEFAULT_API_BASE_URL),
            energy_site_id=os.getenv('ENERGY_SITE_ID', ''),
            iaqualink_user_id=os.getenv('IAQUALINK_USER_ID', ''),
            iaqualink_password=os.getenv('IAQUALINK_PASSWORD', ''),
            pool_devices=_split_csv(os.getenv('POOL_DEVICES', 'pool_pump,aux_1')),
            light_devices=_split_csv(os.getenv('LIGHT_DEVICES', 'aux_B1,aux_B3,aux_B4')),
            activation_excess_power=int(os.getenv('ACTIVATION_EXCESS_POWER', '2000')),
            deactivation_excess_power=int(os.getenv('DEACTIVATION_EXCESS_POWER', '500')),
            gcp_project_id=project_id,
            cloud_run_region=os.getenv('CLOUD_RUN_REGION', 'us-west1'),
            cloud_run_job_name=os.getenv('CLOUD_RUN_JOB_NAME', 'pool-on-solar'),
            scheduler_job_name=os.getenv(
                'SCHEDULER_JOB_NAME', 'pool-on-solar-scheduler-trigger'
            ),
            cache_ttl_seconds=max(5, int(os.getenv('STATUS_CACHE_TTL_SECONDS', '60'))),
        )


def _fetch_tesla_live(config: DashboardConfig) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Fetches live Tesla Energy metrics and non-sensitive OAuth token health."""
    if not config.tesla_client_id:
        return (
            {'status': 'unconfigured', 'error': 'TESLA_CLIENT_ID not configured'},
            {'status': 'unconfigured'},
        )
    if not config.tesla_token_secret_name and not config.tesla_token_file:
        return (
            {'status': 'unconfigured', 'error': 'Tesla token store not configured'},
            {'status': 'unconfigured'},
        )

    token_store = create_token_store(
        secret_name=config.tesla_token_secret_name or None,
        cache_file=config.tesla_token_file or None,
    )
    with TeslaFleetClient(
        client_id=config.tesla_client_id,
        token_store=token_store,
        api_base_url=config.tesla_api_base_url,
    ) as client:
        site_id = config.energy_site_id or client.get_energy_site_id()
        live = client.get_live_status(site_id)
        token = client._token

    solar_power = live.get('solar_power')
    load_power = live.get('load_power')
    excess_power = (
        solar_power - load_power
        if solar_power is not None and load_power is not None
        else None
    )

    total_pack_energy = live.get('total_pack_energy')
    battery_count = (
        max(1, round(float(total_pack_energy) / 13500.0))
        if total_pack_energy and float(total_pack_energy) >= 9000.0
        else None
    )

    tesla_data: Dict[str, Any] = {
        'status': 'ok',
        'solar_power': solar_power,
        'load_power': load_power,
        'excess_power': excess_power,
        'battery_power': live.get('battery_power'),
        'grid_power': live.get('grid_power'),
        'grid_status': live.get('grid_status'),
        'percentage_charged': live.get('percentage_charged'),
        'total_pack_energy': total_pack_energy,
        'battery_count': battery_count,
        'storm_mode_active': live.get('storm_mode_active'),
        'timestamp': live.get('timestamp'),
    }

    token_health: Dict[str, Any] = {'status': 'ok'}
    if token is not None:
        seconds_left = max(0, int(token.expires_at - time.time()))
        token_health['expires_in_seconds'] = seconds_left
        token_health['expires_at'] = datetime.datetime.fromtimestamp(
            token.expires_at, tz=datetime.timezone.utc
        ).isoformat()

    return tesla_data, token_health


async def _fetch_iaqualink_async(config: DashboardConfig) -> Dict[str, Any]:
    from iaqualink.client import AqualinkClient

    async with AqualinkClient(config.iaqualink_user_id, config.iaqualink_password) as client:
        systems = await client.get_systems()
        if not systems:
            return {'status': 'error', 'error': 'No iAquaLink systems found'}
        system = list(systems.values())[0]
        devices = await system.get_devices()

    def _serialize_device(key: str) -> Dict[str, Any]:
        dev = devices.get(key)
        if dev is None:
            return {'key': key, 'label': key, 'found': False, 'is_on': False, 'state': None}
        return {
            'key': key,
            'label': getattr(dev, 'label', key) or key,
            'found': True,
            'is_on': bool(getattr(dev, 'is_on', False)),
            'state': getattr(dev, 'state', None),
        }

    temps: Dict[str, Optional[str]] = {}
    for temp_key in ('pool_temp', 'spa_temp', 'air_temp'):
        dev = devices.get(temp_key)
        if dev is not None and getattr(dev, 'state', None) not in (None, ''):
            temps[temp_key] = str(dev.state)

    return {
        'status': 'ok',
        'system_name': getattr(system, 'name', None) or 'iAquaLink',
        'pool': [_serialize_device(k) for k in config.pool_devices],
        'lights': [_serialize_device(k) for k in config.light_devices],
        'temperatures': temps,
    }


def _fetch_iaqualink(config: DashboardConfig) -> Dict[str, Any]:
    if not config.iaqualink_user_id or not config.iaqualink_password:
        return {'status': 'unconfigured', 'error': 'iAquaLink credentials not configured'}
    return asyncio.run(_fetch_iaqualink_async(config))


def _fetch_gcp_job_health(config: DashboardConfig) -> Dict[str, Any]:
    if not config.gcp_project_id:
        return {'status': 'unconfigured', 'error': 'GCP project ID not configured'}

    import google.auth
    from google.auth.transport.requests import Request

    creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    creds.refresh(Request())
    headers = {'Authorization': f'Bearer {creds.token}'}

    result: Dict[str, Any] = {'status': 'ok'}
    with httpx.Client(timeout=15.0) as http:
        if config.cloud_run_job_name:
            job_url = (
                f'https://run.googleapis.com/v2/projects/{config.gcp_project_id}'
                f'/locations/{config.cloud_run_region}/jobs/{config.cloud_run_job_name}'
            )
            resp = http.get(job_url, headers=headers)
            if resp.status_code == 200:
                job_json = resp.json()
                latest = job_json.get('latestCreatedExecution') or {}
                exec_name = str(latest.get('name', '')).split('/')[-1] or None
                completion_status = latest.get('completionStatus')
                result['cloud_run_job'] = {
                    'name': config.cloud_run_job_name,
                    'execution': exec_name,
                    'completion_status': completion_status,
                    'create_time': latest.get('createTime'),
                    'completion_time': latest.get('completionTime'),
                    'execution_count': job_json.get('executionCount'),
                }
            else:
                result['cloud_run_job'] = {
                    'name': config.cloud_run_job_name,
                    'error': f'HTTP {resp.status_code}',
                }

        if config.scheduler_job_name:
            sched_url = (
                f'https://cloudscheduler.googleapis.com/v1/projects/{config.gcp_project_id}'
                f'/locations/{config.cloud_run_region}/jobs/{config.scheduler_job_name}'
            )
            resp = http.get(sched_url, headers=headers)
            if resp.status_code == 200:
                sched_json = resp.json()
                result['scheduler'] = {
                    'name': config.scheduler_job_name,
                    'state': sched_json.get('state'),
                    'schedule': sched_json.get('schedule'),
                    'last_attempt_time': sched_json.get('lastAttemptTime'),
                }
            else:
                result['scheduler'] = {
                    'name': config.scheduler_job_name,
                    'error': f'HTTP {resp.status_code}',
                }

    return result


def toggle_gcp_scheduler_job(config: DashboardConfig, action: str) -> Dict[str, Any]:
    """Pauses or resumes the Cloud Scheduler job via the Cloud Scheduler v1 REST API."""
    if action not in {'pause', 'resume'}:
        raise ValueError(f'Invalid scheduler action: {action}')
    if not config.gcp_project_id or not config.scheduler_job_name:
        raise ValueError('GCP_PROJECT_ID and SCHEDULER_JOB_NAME must be configured.')

    import google.auth
    from google.auth.transport.requests import Request

    creds, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    creds.refresh(Request())
    headers = {'Authorization': f'Bearer {creds.token}'}

    url = (
        f'https://cloudscheduler.googleapis.com/v1/projects/{config.gcp_project_id}'
        f'/locations/{config.cloud_run_region}/jobs/{config.scheduler_job_name}:{action}'
    )
    with httpx.Client(timeout=15.0) as http:
        resp = http.post(url, headers=headers, json={})
    if resp.status_code >= 400:
        raise RuntimeError(
            f'Cloud Scheduler {action} failed ({resp.status_code}): {resp.text}'
        )
    return resp.json()


class StatusCollector:
    """Thread-safe TTL-cached collector for Tesla, iAquaLink, and Cloud Run job health."""

    def __init__(
        self,
        config: DashboardConfig,
        tesla_fetcher: Optional[Callable[[DashboardConfig], Tuple[Dict[str, Any], Dict[str, Any]]]] = None,
        iaqualink_fetcher: Optional[Callable[[DashboardConfig], Dict[str, Any]]] = None,
        job_health_fetcher: Optional[Callable[[DashboardConfig], Dict[str, Any]]] = None,
        scheduler_toggler: Optional[Callable[[DashboardConfig, str], Dict[str, Any]]] = None,
    ) -> None:
        self._config = config
        self._tesla_fetcher = tesla_fetcher or _fetch_tesla_live
        self._iaqualink_fetcher = iaqualink_fetcher or _fetch_iaqualink
        self._job_health_fetcher = job_health_fetcher or _fetch_gcp_job_health
        self._scheduler_toggler = scheduler_toggler or toggle_gcp_scheduler_job
        self._lock = threading.Lock()
        self._cached_snapshot: Optional[Dict[str, Any]] = None
        self._cached_at: float = 0.0

    def toggle_scheduler(self, action: str) -> Dict[str, Any]:
        result = self._scheduler_toggler(self._config, action)
        with self._lock:
            self._cached_snapshot = None
            self._cached_at = 0.0
        return result

    def get_snapshot(self, force_refresh: bool = False) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            if (
                not force_refresh
                and self._cached_snapshot is not None
                and (now - self._cached_at) < self._config.cache_ttl_seconds
            ):
                age = int(now - self._cached_at)
                return {**self._cached_snapshot, 'cached': True, 'age_seconds': age}

            snapshot = self._collect_uncached()
            self._cached_snapshot = snapshot
            self._cached_at = time.time()
            return {**snapshot, 'cached': False, 'age_seconds': 0}

    def _collect_uncached(self) -> Dict[str, Any]:
        def safe_tesla() -> Tuple[Dict[str, Any], Dict[str, Any]]:
            try:
                return self._tesla_fetcher(self._config)
            except Exception as err:
                return {'status': 'error', 'error': str(err)}, {'status': 'error', 'error': str(err)}

        def safe_iaqualink() -> Dict[str, Any]:
            try:
                return self._iaqualink_fetcher(self._config)
            except Exception as err:
                return {'status': 'error', 'error': str(err)}

        def safe_job_health() -> Dict[str, Any]:
            try:
                return self._job_health_fetcher(self._config)
            except Exception as err:
                return {'status': 'error', 'error': str(err)}

        with ThreadPoolExecutor(max_workers=3) as pool:
            tesla_future = pool.submit(safe_tesla)
            iaq_future = pool.submit(safe_iaqualink)
            job_future = pool.submit(safe_job_health)
            tesla_data, token_health = tesla_future.result()
            iaq_data = iaq_future.result()
            job_health = job_future.result()

        return {
            'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'cache_ttl_seconds': self._config.cache_ttl_seconds,
            'thresholds': {
                'activation_excess_power': self._config.activation_excess_power,
                'deactivation_excess_power': self._config.deactivation_excess_power,
            },
            'tesla': tesla_data,
            'iaqualink': iaq_data,
            'job_health': job_health,
            'token_health': token_health,
        }


def _format_kw(watts: Optional[float]) -> str:
    """Formats power in Tesla Energy style: '0 kW', '0.3 kW', '4.2 kW'."""
    if watts is None:
        return '—'
    kw = round(abs(float(watts)) / 1000.0, 1)
    if kw == 0.0:
        return '0 kW'
    return f'{kw:.1f} kW'


def _format_signed_kw(watts: Optional[float]) -> str:
    if watts is None:
        return '—'
    kw = round(float(watts) / 1000.0, 1)
    if kw == 0.0:
        return '0 kW'
    sign = '+' if kw > 0 else '−'
    return f'{sign}{abs(kw):.1f} kW'


def _format_relative_iso(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return '—'
    try:
        dt = datetime.datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
        delta = int((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds())
        if delta < 60:
            return f'{max(0, delta)}s ago'
        if delta < 3600:
            return f'{delta // 60}m ago'
        if delta < 86400:
            return f'{delta // 3600}h {(delta % 3600) // 60}m ago'
        return f'{delta // 86400}d ago'
    except Exception:
        return iso_ts


def _format_duration_seconds(seconds: Optional[int]) -> str:
    if seconds is None:
        return '—'
    if seconds <= 0:
        return 'Refreshing on next run'
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    if hours > 0:
        return f'{hours}h {minutes}m'
    return f'{minutes}m'


def _format_cron_schedule(expr: Optional[str]) -> str:
    if not expr:
        return '—'
    parts = expr.strip().split()
    if len(parts) != 5:
        return f'Schedule: {expr}'
    minute, hour, dom, month, dow = parts
    if dom == '*' and month == '*' and dow == '*':
        if minute == '*' and hour == '*':
            return 'Runs every minute'
        if minute.startswith('*/') and hour == '*' and minute[2:].isdigit():
            n = int(minute[2:])
            return 'Runs every minute' if n == 1 else f'Runs every {n} minutes'
        if minute == '0' and hour == '*':
            return 'Runs every hour'
        if minute == '0' and hour.startswith('*/') and hour[2:].isdigit():
            n = int(hour[2:])
            return 'Runs every hour' if n == 1 else f'Runs every {n} hours'
        if minute.isdigit() and hour.isdigit():
            return f'Runs daily at {int(hour):02d}:{int(minute):02d}'
    return f'Schedule: {expr}'


def _render_device_rows(devices: List[Dict[str, Any]]) -> str:
    if not devices:
        return '<p class="muted">No devices configured.</p>'
    rows = []
    for dev in devices:
        label = escape(str(dev.get('label', dev.get('key', ''))))
        if not dev.get('found', True):
            badge = '<span class="pill muted-pill">Not found</span>'
        elif dev.get('is_on'):
            badge = '<span class="pill on-pill">ON</span>'
        else:
            badge = '<span class="pill off-pill">OFF</span>'
        rows.append(
            f'<div class="device-row"><span class="device-name">{label}</span>{badge}</div>'
        )
    return ''.join(rows)


def render_dashboard_html(
    snapshot: Dict[str, Any],
    oauth_section_html: str = '',
) -> bytes:
    tesla = snapshot.get('tesla') or {}
    iaq = snapshot.get('iaqualink') or {}
    job_health = snapshot.get('job_health') or {}
    token_health = snapshot.get('token_health') or {}
    thresholds = snapshot.get('thresholds') or {}
    act_w = thresholds.get('activation_excess_power', 2000)
    deact_w = thresholds.get('deactivation_excess_power', 500)

    # Header overall badge
    sched = job_health.get('scheduler') or {}
    cr_job = job_health.get('cloud_run_job') or {}
    sched_state = sched.get('state')
    if sched_state == 'ENABLED' and cr_job.get('completion_status') != 'EXECUTION_FAILED':
        overall_badge = '<span class="pill on-pill">Active</span>'
    elif sched_state == 'PAUSED':
        overall_badge = '<span class="pill warn-pill">Paused</span>'
    elif tesla.get('status') == 'error' or cr_job.get('completion_status') == 'EXECUTION_FAILED':
        overall_badge = '<span class="pill err-pill">Attention</span>'
    else:
        overall_badge = '<span class="pill off-pill">Monitoring</span>'

    # Tesla Energy hero diagram (Tesla app layout & labels)
    if tesla.get('status') == 'ok':
        solar_w = float(tesla.get('solar_power') or 0.0)
        load_w = float(tesla.get('load_power') or 0.0)
        batt_w = float(tesla.get('battery_power') or 0.0)
        grid_w = float(tesla.get('grid_power') or 0.0)

        solar_val = _format_kw(tesla.get('solar_power'))
        home_val = _format_kw(tesla.get('load_power'))
        grid_val = _format_kw(tesla.get('grid_power'))

        batt_pct = tesla.get('percentage_charged')
        batt_kw = _format_kw(tesla.get('battery_power'))
        if batt_pct is not None:
            powerwall_val = f'{batt_kw} · {batt_pct:.0f}%'
        else:
            powerwall_val = batt_kw

        batt_count = tesla.get('battery_count')
        powerwall_label = (
            f'POWERWALL · {int(batt_count)}x' if batt_count and int(batt_count) > 1 else 'POWERWALL'
        )

        # Animated energy-flow overlay classes based on live power direction
        solar_flow_svg = (
            '<path class="flow-line flow-solar" d="M 272 222 L 280 232 L 280 264" />'
            if solar_w > 50
            else ''
        )
        home_flow_svg = (
            '<path class="flow-line flow-home" d="M 289 278 L 368 262" />'
            if load_w > 50
            else ''
        )
        if batt_w < -50:
            # Charging: Gateway -> Powerwall
            batt_flow_svg = '<path class="flow-line flow-batt" d="M 271 281 L 204 295" />'
        elif batt_w > 50:
            # Discharging: Powerwall -> Gateway
            batt_flow_svg = '<path class="flow-line flow-batt" d="M 204 295 L 271 281" />'
        else:
            batt_flow_svg = ''

        if grid_w > 50:
            # Importing: Grid -> Gateway
            grid_flow_svg = '<path class="flow-line flow-grid" d="M 432 356 L 348 332 L 284 314 L 280 302" />'
        elif grid_w < -50:
            # Exporting: Gateway -> Grid
            grid_flow_svg = '<path class="flow-line flow-grid" d="M 280 302 L 284 314 L 348 332 L 432 356" />'
        else:
            grid_flow_svg = ''

        excess_val = tesla.get('excess_power')
        excess_str = _format_signed_kw(excess_val)
        if excess_val is None:
            excess_badge_cls, excess_note = 'off-pill', 'Excess solar unavailable'
        elif excess_val >= act_w:
            excess_badge_cls, excess_note = 'on-pill', f'Excess {excess_str} · Pool ready'
        elif excess_val >= deact_w:
            excess_badge_cls, excess_note = 'warn-pill', f'Excess {excess_str} · Standby'
        else:
            excess_badge_cls, excess_note = 'off-pill', f'Excess {excess_str}'

        grid_dir = ''
        if grid_w > 50:
            grid_dir = 'Importing from grid'
        elif grid_w < -50:
            grid_dir = 'Exporting to grid'
        elif tesla.get('grid_status'):
            grid_dir = str(tesla.get('grid_status'))

        batt_dir = ''
        if batt_w < -50:
            batt_dir = 'Powerwall charging'
        elif batt_w > 50:
            batt_dir = 'Powerwall discharging'

        sub_notes = ' · '.join(p for p in [batt_dir, grid_dir] if p)

        tesla_html = f"""
        <div class="energy-hero">
          <svg class="energy-svg" viewBox="0 0 560 450" role="img" aria-label="Tesla Energy live power flow">
            <defs>
              <radialGradient id="sceneBg" cx="68%" cy="14%" r="85%">
                <stop offset="0%" stop-color="#3a3d45" />
                <stop offset="48%" stop-color="#1b1d22" />
                <stop offset="100%" stop-color="#0c0d10" />
              </radialGradient>
              <linearGradient id="solarPanel" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stop-color="#1a2232" />
                <stop offset="100%" stop-color="#0f141e" />
              </linearGradient>
              <linearGradient id="roofEdge" x1="0%" y1="0%" x2="0%" y2="100%">
                <stop offset="0%" stop-color="#2b2e35" />
                <stop offset="100%" stop-color="#181a1f" />
              </linearGradient>
              <linearGradient id="wallFront" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stop-color="#181a1f" />
                <stop offset="100%" stop-color="#0c0d10" />
              </linearGradient>
              <linearGradient id="wallSide" x1="0%" y1="0%" x2="100%" y2="0%">
                <stop offset="0%" stop-color="#101216" />
                <stop offset="100%" stop-color="#1b1e24" />
              </linearGradient>
              <linearGradient id="pwBody" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stop-color="#f3f4f6" />
                <stop offset="100%" stop-color="#cbd5e1" />
              </linearGradient>
              <linearGradient id="winGlow" x1="0%" y1="0%" x2="0%" y2="100%">
                <stop offset="0%" stop-color="#e5e7eb" />
                <stop offset="100%" stop-color="#d4ccb8" />
              </linearGradient>
              <filter id="greenGlow" x="-50%" y="-20%" width="200%" height="140%">
                <feGaussianBlur stdDeviation="2.5" result="blur" />
                <feMerge>
                  <feMergeNode in="blur" />
                  <feMergeNode in="SourceGraphic" />
                </feMerge>
              </filter>
            </defs>

            <rect width="560" height="450" fill="url(#sceneBg)" />

            <!-- Ground reflection shadow -->
            <ellipse cx="275" cy="348" rx="215" ry="34" fill="#050608" opacity="0.72" />

            <!-- Back upper roof + solar array -->
            <polygon points="36,104 238,76 312,146 98,176" fill="url(#roofEdge)" stroke="#2e3139" stroke-width="1" />
            <polygon points="48,110 232,84 294,142 104,168" fill="url(#solarPanel)" stroke="#283144" stroke-width="1" />
            <g stroke="#283144" stroke-width="0.9" opacity="0.75">
              <line x1="94" y1="103" x2="152" y2="161" />
              <line x1="140" y1="97" x2="199" y2="155" />
              <line x1="186" y1="90" x2="246" y2="148" />
              <line x1="68" y1="129" x2="253" y2="103" />
              <line x1="86" y1="149" x2="274" y2="122" />
            </g>

            <!-- House left gable + garage wall -->
            <polygon points="36,192 98,154 176,236 176,342 36,288" fill="url(#wallSide)" stroke="#20232b" stroke-width="1" />
            <!-- Garage door slats -->
            <polygon points="52,238 148,274 148,328 52,290" fill="#14161b" stroke="#252831" stroke-width="1" />
            <g stroke="#20232b" stroke-width="1">
              <line x1="52" y1="251" x2="148" y2="287" />
              <line x1="52" y1="264" x2="148" y2="300" />
              <line x1="52" y1="277" x2="148" y2="313" />
            </g>

            <!-- House front right wall -->
            <polygon points="176,236 456,198 456,296 176,342" fill="url(#wallFront)" stroke="#22252d" stroke-width="1" />

            <!-- Front main roof + solar array -->
            <polygon points="94,154 366,118 468,198 176,238" fill="url(#roofEdge)" stroke="#323640" stroke-width="1" />
            <polygon points="122,163 356,131 440,196 190,230" fill="url(#solarPanel)" stroke="#2a3447" stroke-width="1" />
            <!-- Solar panel grid -->
            <g stroke="#2b364a" stroke-width="1" opacity="0.85">
              <line x1="168" y1="157" x2="238" y2="223" />
              <line x1="215" y1="150" x2="288" y2="216" />
              <line x1="262" y1="144" x2="338" y2="209" />
              <line x1="309" y1="138" x2="389" y2="203" />
              <line x1="145" y1="185" x2="384" y2="152" />
              <line x1="167" y1="208" x2="412" y2="174" />
            </g>

            <!-- Lit 4-pane window on right -->
            <polygon points="370,242 436,233 436,286 370,296" fill="#0b0c0f" stroke="#2d313a" stroke-width="1.5" />
            <polygon points="374,246 401,242 401,266 374,270" fill="url(#winGlow)" />
            <polygon points="405,241 432,237 432,261 405,265" fill="url(#winGlow)" />
            <polygon points="374,274 401,270 401,291 374,295" fill="url(#winGlow)" opacity="0.92" />
            <polygon points="405,269 432,265 432,286 405,290" fill="url(#winGlow)" opacity="0.92" />

            <!-- Static conduit lines -->
            <g fill="none" stroke="#2c303a" stroke-width="4" stroke-linecap="round" stroke-linejoin="round">
              <path d="M 272 222 L 280 232 L 280 264" />
              <path d="M 289 278 L 368 262" />
              <path d="M 204 295 L 271 281" />
              <path d="M 280 302 L 284 314 L 348 332 L 432 356" />
            </g>

            <!-- Active directional energy flow lines -->
            {solar_flow_svg}
            {home_flow_svg}
            {batt_flow_svg}
            {grid_flow_svg}

            <!-- Center Backup Gateway box -->
            <rect x="272" y="264" width="16" height="38" rx="2.5" fill="#4b505c" stroke="#646a78" stroke-width="1" />
            <circle cx="280" cy="292" r="2.6" fill="#cbd5e1" />

            <!-- Powerwall unit on front-left wall -->
            <polygon points="175,286 181,284 181,341 175,342" fill="#22c55e" filter="url(#greenGlow)" />
            <polygon points="181,284 209,280 209,336 181,341" fill="url(#pwBody)" />
            <polygon points="175,286 203,282 209,280 181,284" fill="#ffffff" />
            <line x1="181" y1="297" x2="209" y2="293" stroke="#94a3b8" stroke-width="1" />
            <text x="195" y="309" text-anchor="middle" fill="#64748b" font-size="4.6" font-weight="700" letter-spacing="1.2">TESLA</text>

            <!-- Leader line: SOLAR (top-left) -->
            <line x1="215" y1="68" x2="215" y2="184" stroke="#9ca3af" stroke-width="1" opacity="0.7" />
            <text x="215" y="35" text-anchor="middle" class="svg-label">SOLAR</text>
            <text x="215" y="61" text-anchor="middle" class="svg-value">{escape(solar_val)}</text>

            <!-- Leader line: HOME (top-right) -->
            <line x1="406" y1="126" x2="406" y2="254" stroke="#9ca3af" stroke-width="1" opacity="0.7" />
            <text x="406" y="92" text-anchor="middle" class="svg-label">HOME</text>
            <text x="406" y="118" text-anchor="middle" class="svg-value">{escape(home_val)}</text>

            <!-- Leader line: POWERWALL (bottom-left) -->
            <line x1="194" y1="344" x2="194" y2="375" stroke="#9ca3af" stroke-width="1" opacity="0.7" />
            <text x="194" y="395" text-anchor="middle" class="svg-label">{escape(powerwall_label)}</text>
            <text x="194" y="423" text-anchor="middle" class="svg-value">{escape(powerwall_val)}</text>

            <!-- Leader line: GRID (bottom-right) -->
            <line x1="418" y1="358" x2="418" y2="375" stroke="#9ca3af" stroke-width="1" opacity="0.7" />
            <text x="418" y="395" text-anchor="middle" class="svg-label">GRID</text>
            <text x="418" y="423" text-anchor="middle" class="svg-value">{escape(grid_val)}</text>
          </svg>
          <div class="hero-footer">
            <span class="pill {excess_badge_cls}">{escape(excess_note)}</span>
            <span class="hero-sub">{escape(sub_notes)}</span>
          </div>
        </div>
        """
    else:
        err_msg = escape(str(tesla.get('error') or 'Unavailable'))
        tesla_html = f'<div class="card error-box">Tesla Energy unavailable: <code>{err_msg}</code></div>'

    # iAquaLink section
    if iaq.get('status') == 'ok':
        temps = iaq.get('temperatures') or {}
        temp_chips = []
        if 'pool_temp' in temps:
            temp_chips.append(f'<span class="chip">Pool {escape(temps["pool_temp"])}°</span>')
        if 'spa_temp' in temps:
            temp_chips.append(f'<span class="chip">Spa {escape(temps["spa_temp"])}°</span>')
        if 'air_temp' in temps:
            temp_chips.append(f'<span class="chip">Air {escape(temps["air_temp"])}°</span>')
        temps_html = f'<div class="chips">{"".join(temp_chips)}</div>' if temp_chips else ''

        iaq_html = f"""
        {temps_html}
        <div class="grid-2">
          <div class="card">
            <div class="card-title">Pool System</div>
            {_render_device_rows(iaq.get('pool') or [])}
          </div>
          <div class="card">
            <div class="card-title">Lights</div>
            {_render_device_rows(iaq.get('lights') or [])}
          </div>
        </div>
        """
    else:
        err_msg = escape(str(iaq.get('error') or 'Unavailable'))
        iaq_html = f'<div class="card error-box">iAquaLink unavailable: <code>{err_msg}</code></div>'

    # Job & token health strip
    exec_status = cr_job.get('completion_status')
    if exec_status == 'EXECUTION_SUCCEEDED':
        job_badge = '<span class="pill on-pill">Succeeded</span>'
        job_time_prefix = 'Last run'
    elif exec_status == 'EXECUTION_FAILED':
        job_badge = '<span class="pill err-pill">Failed</span>'
        job_time_prefix = 'Last run'
    elif exec_status == 'EXECUTION_RUNNING':
        job_badge = '<span class="pill warn-pill">Running</span>'
        job_time_prefix = 'Started'
    elif exec_status == 'EXECUTION_PENDING':
        job_badge = '<span class="pill warn-pill">Starting</span>'
        job_time_prefix = 'Started'
    elif exec_status == 'EXECUTION_CANCELLED':
        job_badge = '<span class="pill off-pill">Cancelled</span>'
        job_time_prefix = 'Last run'
    elif exec_status:
        pretty_status = str(exec_status).removeprefix('EXECUTION_').replace('_', ' ').title()
        job_badge = f'<span class="pill warn-pill">{escape(pretty_status)}</span>'
        job_time_prefix = 'Last run'
    else:
        job_badge = f'<span class="pill off-pill">{escape(str(cr_job.get("error") or "Unknown"))}</span>'
        job_time_prefix = 'Last run'

    last_run_rel = _format_relative_iso(cr_job.get('completion_time') or cr_job.get('create_time'))

    if sched_state == 'ENABLED':
        sched_badge = '<span class="pill on-pill">Enabled</span>'
        sched_action_html = (
            '<form method="post" action="/scheduler/pause" style="margin-top:0.65rem">'
            '<button class="toggle-btn pause-btn" type="submit">Pause Schedule</button>'
            '</form>'
        )
    elif sched_state == 'PAUSED':
        sched_badge = '<span class="pill warn-pill">Paused</span>'
        sched_action_html = (
            '<form method="post" action="/scheduler/resume" style="margin-top:0.65rem">'
            '<button class="toggle-btn resume-btn" type="submit">Start Schedule</button>'
            '</form>'
        )
    else:
        sched_badge = f'<span class="pill off-pill">{escape(str(sched_state or sched.get("error") or "Unknown"))}</span>'
        sched_action_html = ''
    sched_desc = _format_cron_schedule(sched.get('schedule'))

    if token_health.get('status') == 'ok':
        tok_badge = '<span class="pill on-pill">Valid</span>'
        tok_detail = f'Access token refreshes in {_format_duration_seconds(token_health.get("expires_in_seconds"))}'
    else:
        tok_badge = '<span class="pill err-pill">Error</span>'
        tok_detail = str(token_health.get('error') or 'Check token secret')

    age_s = snapshot.get('age_seconds', 0)
    ttl_s = snapshot.get('cache_ttl_seconds', 60)
    freshness = 'Just updated' if age_s < 3 else f'Updated {age_s}s ago'

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pool on Solar — Live Status</title>
  <style>
    :root {{
      --bg: #0c0d10;
      --card: #15171d;
      --border: #252832;
      --text: #f3f4f6;
      --muted: #9ca3af;
      --green: #4ade80;
      --green-bg: #052e16;
      --amber: #fbbf24;
      --amber-bg: #451a03;
      --red: #f87171;
      --red-bg: #450a0a;
      --off-bg: #1e222b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 1.5rem 1rem 3rem;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }}
    .container {{ max-width: 760px; margin: 0 auto; }}
    header {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      margin-bottom: 1.25rem;
    }}
    .title-group {{ display: flex; align-items: center; gap: 0.75rem; }}
    h1 {{ margin: 0; font-size: 1.4rem; font-weight: 600; letter-spacing: -0.02em; }}
    h2 {{
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin: 1.5rem 0 0.7rem;
      font-weight: 600;
    }}
    .meta {{ display: flex; align-items: center; gap: 0.6rem; font-size: 0.82rem; color: var(--muted); }}
    .refresh-btn {{
      display: inline-block;
      padding: 0.35rem 0.7rem;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--text);
      text-decoration: none;
      font-size: 0.8rem;
      font-weight: 500;
    }}
    .refresh-btn:hover {{ background: #1e212a; }}
    .energy-hero {{
      background: #0c0d10;
      border: 1px solid var(--border);
      border-radius: 18px;
      overflow: hidden;
    }}
    .energy-svg {{
      display: block;
      width: 100%;
      height: auto;
    }}
    .svg-label {{
      fill: #9ca3af;
      font-size: 13px;
      font-weight: 500;
      letter-spacing: 0.09em;
    }}
    .svg-value {{
      fill: #f8fafc;
      font-size: 23px;
      font-weight: 600;
      letter-spacing: -0.01em;
    }}
    .flow-line {{
      fill: none;
      stroke-width: 3.5;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-dasharray: 18 26;
      animation: energyFlow 1.6s linear infinite;
    }}
    .flow-solar {{ stroke: #eab308; }}
    .flow-home {{ stroke: #facc15; }}
    .flow-batt {{ stroke: #22c55e; }}
    .flow-grid {{ stroke: #94a3b8; }}
    @keyframes energyFlow {{
      from {{ stroke-dashoffset: 44; }}
      to {{ stroke-dashoffset: 0; }}
    }}
    .hero-footer {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      padding: 0.75rem 1.1rem;
      background: var(--card);
      border-top: 1px solid var(--border);
    }}
    .hero-sub {{ font-size: 0.8rem; color: var(--muted); }}
    .grid-2 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 0.85rem;
    }}
    .grid-3 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 0.85rem;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1rem 1.1rem;
    }}
    .card-title {{ font-size: 0.88rem; font-weight: 600; margin-bottom: 0.65rem; }}
    .device-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0.45rem 0;
      border-top: 1px solid var(--border);
    }}
    .device-row:first-of-type {{ border-top: 0; padding-top: 0.1rem; }}
    .device-name {{ font-size: 0.92rem; }}
    .pill {{
      display: inline-block;
      padding: 0.16rem 0.58rem;
      border-radius: 999px;
      font-size: 0.74rem;
      font-weight: 600;
      letter-spacing: 0.02em;
    }}
    .on-pill {{ background: var(--green-bg); color: var(--green); }}
    .off-pill {{ background: var(--off-bg); color: var(--muted); }}
    .warn-pill {{ background: var(--amber-bg); color: var(--amber); }}
    .err-pill {{ background: var(--red-bg); color: var(--red); }}
    .muted-pill {{ background: var(--off-bg); color: var(--muted); }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.75rem; }}
    .chip {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0.25rem 0.7rem;
      font-size: 0.8rem;
      color: var(--muted);
      font-weight: 500;
    }}
    .health-top {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.35rem; }}
    .health-title {{ font-size: 0.82rem; font-weight: 600; color: var(--muted); }}
    .health-detail {{ font-size: 0.82rem; color: var(--muted); }}
    .error-box {{ color: var(--red); font-size: 0.9rem; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em; }}
    .toggle-btn {{
      width: 100%;
      padding: 0.45rem 0.75rem;
      border-radius: 8px;
      border: 1px solid transparent;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
    }}
    .pause-btn {{ background: var(--amber-bg); color: var(--amber); }}
    .resume-btn {{ background: var(--green-bg); color: var(--green); }}
    .toggle-btn:hover {{ opacity: 0.88; }}
    .muted {{ color: var(--muted); font-size: 0.85rem; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title-group">
        <h1>Pool on Solar</h1>
        {overall_badge}
      </div>
      <div class="meta">
        <span>{escape(freshness)} · {ttl_s}s cache</span>
        <a class="refresh-btn" href="/">Refresh</a>
      </div>
    </header>

    {tesla_html}

    <h2>Pool &amp; Lights</h2>
    {iaq_html}

    <h2>Automation Health</h2>
    <div class="grid-3">
      <div class="card">
        <div class="health-top">
          <span class="health-title">Cloud Run Job</span>
          {job_badge}
        </div>
        <div class="health-detail">{escape(job_time_prefix)} {escape(last_run_rel)}</div>
      </div>
      <div class="card">
        <div class="health-top">
          <span class="health-title">Scheduler</span>
          {sched_badge}
        </div>
        <div class="health-detail">{escape(sched_desc)}</div>
        {sched_action_html}
      </div>
      <div class="card">
        <div class="health-top">
          <span class="health-title">Tesla OAuth</span>
          {tok_badge}
        </div>
        <div class="health-detail">{escape(tok_detail)}</div>
      </div>
    </div>

    {oauth_section_html}
  </div>
</body>
</html>
"""
    return html.encode('utf-8')
