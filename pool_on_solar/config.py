"""Configuration loader for Pool on Solar (JSON file + environment variable overrides)."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_API_BASE_URL = 'https://fleet-api.prd.na.vn.cloud.tesla.com'
REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _split_csv(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    items = [part.strip() for part in value.split(',') if part.strip()]
    return items or None


@dataclass
class TeslaConfig:
    client_id: str = ''
    client_secret: str = ''
    energy_site_id: str = ''
    api_base_url: str = DEFAULT_API_BASE_URL


@dataclass
class PoolConfig:
    devices: List[str] = field(default_factory=lambda: ['pool_pump', 'aux_1'])
    activation_excess_power: int = 2000
    deactivation_excess_power: int = 500
    max_operating_window_start: int = 8
    max_operating_window_end: int = 18
    min_operating_window_start: int = -1
    min_operating_window_end: int = -1
    minimum_battery: int = 50


@dataclass
class LightConfig:
    devices: List[str] = field(default_factory=lambda: ['aux_B1', 'aux_B3', 'aux_B4'])
    minimum_battery: int = 50
    max_operating_window_end: int = 22


@dataclass
class IAquaLinkConfig:
    user_id: str = ''
    password: str = ''
    pool: PoolConfig = field(default_factory=PoolConfig)
    light: LightConfig = field(default_factory=LightConfig)


@dataclass
class LocationConfig:
    timezone: str = 'US/Pacific'
    latitude: float = 37.3861
    longitude: float = -122.0839


@dataclass
class AppConfig:
    tesla: TeslaConfig = field(default_factory=TeslaConfig)
    iaqualink: IAquaLinkConfig = field(default_factory=IAquaLinkConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    dry_run: bool = False
    schedule_cron: str = '*/15 * * * *'
    cache_ttl_seconds: int = 60


def _load_raw_config(config_file: Optional[str] = None) -> Dict[str, Any]:
    env_json = os.getenv('POOL_CONFIG_JSON')
    if env_json:
        return json.loads(env_json)

    path_str = config_file or os.getenv('CONFIG_FILE')
    path = Path(path_str) if path_str else (REPO_ROOT / 'config.json')
    if not path.is_file():
        return {}
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)


def load_config(config_file: Optional[str] = None) -> AppConfig:
    """Loads Pool on Solar config from config.json (or POOL_CONFIG_JSON) plus env overrides."""
    raw = _load_raw_config(config_file)
    raw_tesla = raw.get('tesla') or {}
    raw_iaq = raw.get('iaqualink') or {}
    raw_pool = raw_iaq.get('pool') or {}
    raw_light = raw_iaq.get('light') or {}
    raw_loc = raw.get('location') or {}

    tesla = TeslaConfig(
        client_id=os.getenv('TESLA_CLIENT_ID', str(raw_tesla.get('client_id') or '')),
        client_secret=os.getenv('TESLA_CLIENT_SECRET', str(raw_tesla.get('client_secret') or '')),
        energy_site_id=os.getenv('ENERGY_SITE_ID', str(raw_tesla.get('energy_site_id') or '')),
        api_base_url=os.getenv(
            'TESLA_API_BASE_URL',
            str(raw_tesla.get('api_base_url') or DEFAULT_API_BASE_URL),
        ),
    )

    pool_devices = _split_csv(os.getenv('POOL_DEVICES')) or list(
        raw_pool.get('devices', ['pool_pump', 'aux_1'])
    )
    light_devices = _split_csv(os.getenv('LIGHT_DEVICES')) or list(
        raw_light.get('devices', ['aux_B1', 'aux_B3', 'aux_B4'])
    )

    pool = PoolConfig(
        devices=pool_devices,
        activation_excess_power=int(
            os.getenv(
                'ACTIVATION_EXCESS_POWER',
                str(raw_pool.get('activation_excess_power', 2000)),
            )
        ),
        deactivation_excess_power=int(
            os.getenv(
                'DEACTIVATION_EXCESS_POWER',
                str(raw_pool.get('deactivation_excess_power', 500)),
            )
        ),
        max_operating_window_start=int(raw_pool.get('max_operating_window_start', 8)),
        max_operating_window_end=int(raw_pool.get('max_operating_window_end', 18)),
        min_operating_window_start=int(raw_pool.get('min_operating_window_start', -1)),
        min_operating_window_end=int(raw_pool.get('min_operating_window_end', -1)),
        minimum_battery=int(raw_pool.get('minimum_battery', 50)),
    )

    light = LightConfig(
        devices=light_devices,
        minimum_battery=int(raw_light.get('minimum_battery', 50)),
        max_operating_window_end=int(raw_light.get('max_operating_window_end', 22)),
    )

    iaqualink = IAquaLinkConfig(
        user_id=os.getenv('IAQUALINK_USER_ID', str(raw_iaq.get('user_id') or '')),
        password=os.getenv('IAQUALINK_PASSWORD', str(raw_iaq.get('password') or '')),
        pool=pool,
        light=light,
    )

    location = LocationConfig(
        timezone=os.getenv('TIMEZONE', str(raw_loc.get('timezone') or 'US/Pacific')),
        latitude=float(raw_loc.get('latitude', 37.3861)),
        longitude=float(raw_loc.get('longitude', -122.0839)),
    )

    return AppConfig(
        tesla=tesla,
        iaqualink=iaqualink,
        location=location,
        dry_run=parse_bool_env('DRY_RUN', default=False),
        schedule_cron=os.getenv('AUTOMATION_SCHEDULE', '*/15 * * * *'),
        cache_ttl_seconds=max(5, int(os.getenv('STATUS_CACHE_TTL_SECONDS', '60'))),
    )
