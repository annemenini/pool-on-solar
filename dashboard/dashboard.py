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

    tesla_data: Dict[str, Any] = {
        'status': 'ok',
        'solar_power': solar_power,
        'load_power': load_power,
        'excess_power': excess_power,
        'battery_power': live.get('battery_power'),
        'grid_power': live.get('grid_power'),
        'grid_status': live.get('grid_status'),
        'percentage_charged': live.get('percentage_charged'),
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


def _format_watts(watts: Optional[float]) -> Tuple[str, str]:
    if watts is None:
        return '—', ''
    val = float(watts)
    if abs(val) >= 1000:
        return f'{val / 1000:.2f} kW', f'{int(round(val)):,} W'
    return f'{int(round(val)):,} W', ''


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

    # Tesla Energy section
    if tesla.get('status') == 'ok':
        solar_main, solar_sub = _format_watts(tesla.get('solar_power'))
        load_main, load_sub = _format_watts(tesla.get('load_power'))
        excess_val = tesla.get('excess_power')
        excess_main, excess_sub = _format_watts(excess_val)
        if excess_val is None:
            excess_cls, excess_note = '', ''
        elif excess_val >= act_w:
            excess_cls, excess_note = 'accent-green', f'≥ {act_w:,} W · Pool ready'
        elif excess_val >= deact_w:
            excess_cls, excess_note = 'accent-amber', 'Standby range'
        else:
            excess_cls, excess_note = 'accent-muted', f'< {deact_w:,} W · Low surplus'

        batt_pct = tesla.get('percentage_charged')
        batt_display = f'{batt_pct:.0f}%' if batt_pct is not None else '—'
        batt_width = min(100, max(0, float(batt_pct))) if batt_pct is not None else 0
        grid_status = escape(str(tesla.get('grid_status') or ''))
        batt_power = tesla.get('battery_power')
        batt_flow = ''
        if batt_power is not None:
            bp_main, _ = _format_watts(abs(batt_power))
            if batt_power < -50:
                batt_flow = f'Charging {bp_main}'
            elif batt_power > 50:
                batt_flow = f'Discharging {bp_main}'
            else:
                batt_flow = 'Idle'
        batt_sub = ' · '.join(p for p in [batt_flow, grid_status] if p)

        tesla_html = f"""
        <div class="grid-4">
          <div class="metric-card">
            <div class="metric-label">Solar</div>
            <div class="metric-value {'accent-amber' if (tesla.get('solar_power') or 0) > 50 else ''}">{escape(solar_main)}</div>
            <div class="metric-sub">{escape(solar_sub or 'Live generation')}</div>
          </div>
          <div class="metric-card">
            <div class="metric-label">Home Load</div>
            <div class="metric-value">{escape(load_main)}</div>
            <div class="metric-sub">{escape(load_sub or 'Current draw')}</div>
          </div>
          <div class="metric-card">
            <div class="metric-label">Excess Power</div>
            <div class="metric-value {excess_cls}">{escape(excess_main)}</div>
            <div class="metric-sub">{escape(excess_note or excess_sub)}</div>
          </div>
          <div class="metric-card">
            <div class="metric-label">Powerwall</div>
            <div class="metric-value">{escape(batt_display)}</div>
            <div class="bar-track"><div class="bar-fill" style="width: {batt_width:.1f}%"></div></div>
            <div class="metric-sub">{escape(batt_sub or 'Battery charge')}</div>
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
    elif exec_status == 'EXECUTION_FAILED':
        job_badge = '<span class="pill err-pill">Failed</span>'
    elif exec_status:
        job_badge = f'<span class="pill warn-pill">{escape(str(exec_status))}</span>'
    else:
        job_badge = f'<span class="pill off-pill">{escape(str(cr_job.get("error") or "Unknown"))}</span>'

    last_run_rel = _format_relative_iso(cr_job.get('completion_time') or cr_job.get('create_time'))
    exec_id = escape(str(cr_job.get('execution') or '—'))

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
    sched_cron = escape(str(sched.get('schedule') or '—'))

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
      --bg: #f8fafc;
      --card: #ffffff;
      --border: #e2e8f0;
      --text: #0f172a;
      --muted: #64748b;
      --green: #15803d;
      --green-bg: #dcfce7;
      --amber: #b45309;
      --amber-bg: #fef3c7;
      --red: #b91c1c;
      --red-bg: #fee2e2;
      --off-bg: #f1f5f9;
      --bar-bg: #e2e8f0;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0b1120;
        --card: #111827;
        --border: #1f2937;
        --text: #f1f5f9;
        --muted: #94a3b8;
        --green: #4ade80;
        --green-bg: #052e16;
        --amber: #fbbf24;
        --amber-bg: #451a03;
        --red: #f87171;
        --red-bg: #450a0a;
        --off-bg: #1e293b;
        --bar-bg: #1e293b;
      }}
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
    .container {{ max-width: 860px; margin: 0 auto; }}
    header {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      margin-bottom: 1.5rem;
    }}
    .title-group {{ display: flex; align-items: center; gap: 0.75rem; }}
    h1 {{ margin: 0; font-size: 1.5rem; letter-spacing: -0.02em; }}
    h2 {{
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      margin: 1.5rem 0 0.75rem;
    }}
    .meta {{ display: flex; align-items: center; gap: 0.6rem; font-size: 0.85rem; color: var(--muted); }}
    .refresh-btn {{
      display: inline-block;
      padding: 0.35rem 0.7rem;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--text);
      text-decoration: none;
      font-size: 0.82rem;
      font-weight: 500;
    }}
    .refresh-btn:hover {{ opacity: 0.85; }}
    .grid-4 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 0.85rem;
    }}
    .grid-2 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 0.85rem;
    }}
    .grid-3 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 0.85rem;
    }}
    .card, .metric-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem 1.1rem;
    }}
    .metric-label {{ font-size: 0.82rem; color: var(--muted); font-weight: 500; }}
    .metric-value {{ font-size: 1.65rem; font-weight: 700; margin: 0.25rem 0 0.2rem; letter-spacing: -0.02em; }}
    .metric-sub {{ font-size: 0.8rem; color: var(--muted); }}
    .accent-green {{ color: var(--green); }}
    .accent-amber {{ color: var(--amber); }}
    .accent-muted {{ color: var(--muted); }}
    .bar-track {{
      height: 6px;
      border-radius: 999px;
      background: var(--bar-bg);
      overflow: hidden;
      margin: 0.45rem 0 0.35rem;
    }}
    .bar-fill {{
      height: 100%;
      background: var(--green);
      border-radius: 999px;
    }}
    .card-title {{ font-size: 0.9rem; font-weight: 600; margin-bottom: 0.65rem; }}
    .device-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0.45rem 0;
      border-top: 1px solid var(--border);
    }}
    .device-row:first-of-type {{ border-top: 0; padding-top: 0.1rem; }}
    .device-name {{ font-size: 0.95rem; }}
    .pill {{
      display: inline-block;
      padding: 0.15rem 0.55rem;
      border-radius: 999px;
      font-size: 0.75rem;
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
      font-size: 0.82rem;
      color: var(--muted);
      font-weight: 500;
    }}
    .health-top {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.35rem; }}
    .health-title {{ font-size: 0.82rem; font-weight: 600; color: var(--muted); }}
    .health-detail {{ font-size: 0.82rem; color: var(--muted); }}
    .error-box {{ color: var(--red); font-size: 0.9rem; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em; }}
    .btn {{
      display: inline-block;
      padding: 0.5rem 0.9rem;
      background: var(--text);
      color: var(--bg);
      text-decoration: none;
      border-radius: 6px;
      border: 0;
      cursor: pointer;
      font-size: 0.9rem;
      margin-right: 0.5rem;
    }}
    .btn.secondary {{ background: var(--muted); color: #fff; }}
    .toggle-btn {{
      width: 100%;
      padding: 0.45rem 0.75rem;
      border-radius: 8px;
      border: 1px solid var(--border);
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
    }}
    .pause-btn {{ background: var(--amber-bg); color: var(--amber); border-color: transparent; }}
    .resume-btn {{ background: var(--green-bg); color: var(--green); border-color: transparent; }}
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

    <h2>Tesla Energy</h2>
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
        <div class="health-detail">Last run {escape(last_run_rel)} (<code>{exec_id}</code>)</div>
      </div>
      <div class="card">
        <div class="health-top">
          <span class="health-title">Scheduler</span>
          {sched_badge}
        </div>
        <div class="health-detail">Cron <code>{sched_cron}</code></div>
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
