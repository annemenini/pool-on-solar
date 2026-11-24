"""Pool & lights automation controller backed by Tesla Fleet API and iAquaLink."""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from iaqualink.client import AqualinkClient
from iaqualink.device import AqualinkDevice
from suntime import Sun

from pool_on_solar.cache import (
    AUTOMATION_LAST_RUN_CACHE_KEY,
    DASHBOARD_STATUS_TAG,
    AppCache,
)
from pool_on_solar.config import AppConfig
from pool_on_solar.storage import StateStore
from pool_on_solar.tesla_fleet import TeslaFleetClient

STALE_RUN_THRESHOLD_SECONDS = 15 * 60
AUTOMATION_EXEC_IDEMPOTENCY_TTL_SECONDS = 60 * 60
AUTOMATION_LAST_RUN_TTL_SECONDS = 24 * 60 * 60


async def try_switch_device(
    device: AqualinkDevice,
    mode: str,
    dry_run: bool = False,
    actions: Optional[List[str]] = None,
) -> bool:
    """Checks an iAquaLink device status and toggles it if needed."""
    if mode == 'off' and device.is_on:
        if not dry_run:
            print(f'Trying to turn {device.label} OFF (state: {device.state})')
            await device.turn_off()
        msg = f'Turned {device.label} OFF' + (' (dry run)' if dry_run else '')
        print(msg)
        if actions is not None:
            actions.append(msg)
        return True
    if mode == 'on' and not device.is_on:
        if not dry_run:
            print(f'Trying to turn {device.label} ON (state: {device.state})')
            await device.turn_on()
        msg = f'Turned {device.label} ON' + (' (dry run)' if dry_run else '')
        print(msg)
        if actions is not None:
            actions.append(msg)
        return True
    return False


class Controller:
    def __init__(
        self,
        config: AppConfig,
        state_store: StateStore,
        cache: Optional[AppCache] = None,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._cache = cache

    def get_energy_site_status(self) -> Dict[str, Any]:
        """Gets live status from the Tesla energy site."""
        with TeslaFleetClient(
            client_id=self._config.tesla.client_id,
            state_store=self._state_store,
            api_base_url=self._config.tesla.api_base_url,
            cache=self._cache,
        ) as tesla:
            site_id = self._config.tesla.energy_site_id or tesla.get_energy_site_id()
            self._config.tesla.energy_site_id = site_id
            data = tesla.get_live_status(site_id)

        solar_power = data.get('solar_power')
        load_power = data.get('load_power')
        if solar_power is None or load_power is None:
            raise ValueError(
                f'Incomplete live_status response from Tesla energy site '
                f'(solar_power={solar_power}, load_power={load_power}).'
            )

        excess_power = solar_power - load_power
        battery_percentage = data.get('percentage_charged')
        if battery_percentage is None:
            battery_percentage = 100.0
        print(
            f'Excess power: {solar_power} - {load_power} = {excess_power}, '
            f'battery percentage: {battery_percentage}'
        )
        return {'excess_power': excess_power, 'battery_percentage': battery_percentage}

    def convert_to_local_time(self, dt: datetime.datetime) -> datetime.datetime:
        return dt.astimezone(ZoneInfo(self._config.location.timezone))

    def _now_local(self) -> datetime.datetime:
        return self.convert_to_local_time(datetime.datetime.now(datetime.timezone.utc))

    def get_forced_operation_mode(self) -> str:
        """Checks if the current time is within the pool forced on/off operating window."""
        current_time = self._now_local()
        pool = self._config.iaqualink.pool
        if pool.min_operating_window_start >= 0 and pool.min_operating_window_end >= 0:
            if pool.min_operating_window_start <= current_time.hour < pool.min_operating_window_end:
                return 'on'
        if (
            current_time.hour < pool.max_operating_window_start
            or current_time.hour >= pool.max_operating_window_end
        ):
            return 'off'
        return 'flexible'

    async def update_pool(
        self,
        devices: Dict[str, AqualinkDevice],
        excess_power: Optional[float],
        battery_percentage: float,
        actions: Optional[List[str]] = None,
    ) -> None:
        pool = self._config.iaqualink.pool
        forced_mode = self.get_forced_operation_mode()
        if forced_mode == 'off':
            for device_key in reversed(pool.devices):
                await try_switch_device(
                    devices[device_key], 'off', dry_run=self._config.dry_run, actions=actions
                )
            return
        if forced_mode == 'on':
            for device_key in pool.devices:
                await try_switch_device(
                    devices[device_key], 'on', dry_run=self._config.dry_run, actions=actions
                )
            return

        if excess_power is None:
            return

        if (
            battery_percentage < pool.minimum_battery
            or excess_power < pool.deactivation_excess_power
        ):
            reduced = False
            for device_key in reversed(pool.devices):
                reduced = await try_switch_device(
                    devices[device_key], 'off', dry_run=self._config.dry_run, actions=actions
                )
                if reduced:
                    break
            if not reduced:
                print('Pool System: Nothing to turn OFF')
        elif excess_power < pool.activation_excess_power:
            print('Pool System: Standby')
        else:
            increased = False
            for device_key in pool.devices:
                increased = await try_switch_device(
                    devices[device_key], 'on', dry_run=self._config.dry_run, actions=actions
                )
                if increased:
                    break
            if not increased:
                print('Pool System: Nothing to turn ON')

    async def update_lights(
        self,
        devices: Dict[str, AqualinkDevice],
        battery_percentage: float,
        actions: Optional[List[str]] = None,
    ) -> None:
        sun = Sun(self._config.location.latitude, self._config.location.longitude)
        current_time = self._now_local()
        sunset_time = self.convert_to_local_time(sun.get_sunset_time(current_time.date()))
        light = self._config.iaqualink.light
        turn_off_time = current_time.replace(
            hour=light.max_operating_window_end,
            minute=0,
            second=0,
            microsecond=0,
        )
        if current_time.time() < sunset_time.time() or current_time.time() > turn_off_time.time():
            for light_key in light.devices:
                await try_switch_device(
                    devices[light_key], 'off', dry_run=self._config.dry_run, actions=actions
                )
        elif battery_percentage > light.minimum_battery:
            for light_key in light.devices:
                await try_switch_device(
                    devices[light_key], 'on', dry_run=self._config.dry_run, actions=actions
                )

    async def update_iaqualink(
        self,
        energy_site_status: Optional[Dict[str, Any]],
        actions: Optional[List[str]] = None,
    ) -> None:
        if not self._config.iaqualink.user_id or not self._config.iaqualink.password:
            raise ValueError('IAQUALINK_USER_ID and IAQUALINK_PASSWORD must be configured.')

        async with AqualinkClient(
            self._config.iaqualink.user_id,
            self._config.iaqualink.password,
        ) as client:
            systems = await client.get_systems()
            if not systems:
                raise RuntimeError('No iAquaLink systems found for this account.')
            devices = await list(systems.values())[0].get_devices()
            excess_power = energy_site_status['excess_power'] if energy_site_status else None
            battery_percentage = (
                energy_site_status['battery_percentage'] if energy_site_status else 100.0
            )
            await self.update_pool(devices, excess_power, battery_percentage, actions=actions)
            await self.update_lights(devices, battery_percentage, actions=actions)

    async def control_home(self) -> Dict[str, Any]:
        """Retrieves Tesla energy status and adjusts iAquaLink devices accordingly."""
        actions: List[str] = []
        try:
            energy_site_status = self.get_energy_site_status()
            await self.update_iaqualink(energy_site_status, actions=actions)
            return {
                'excess_power': energy_site_status['excess_power'],
                'battery_percentage': energy_site_status['battery_percentage'],
                'actions': actions,
            }
        except Exception as err:
            await self.update_iaqualink(None, actions=actions)
            raise err


async def run_scheduled_automation(
    config: AppConfig,
    state_store: StateStore,
    execution_id: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    controller: Optional[Controller] = None,
    cache: Optional[AppCache] = None,
) -> Dict[str, Any]:
    """Runs one scheduled automation pass (idempotent by execution_id, skipping stale deliveries)."""
    exec_cache_key = f'schedule:exec:automation:{execution_id}' if execution_id else None
    if cache is not None and exec_cache_key and cache.get(exec_cache_key):
        return {
            'status': 'skipped',
            'reason': 'duplicate_execution',
            'execution_id': execution_id,
        }

    if scheduled_at:
        try:
            sched_dt = datetime.datetime.fromisoformat(scheduled_at.replace('Z', '+00:00'))
            age_seconds = (datetime.datetime.now(datetime.timezone.utc) - sched_dt).total_seconds()
            if age_seconds > STALE_RUN_THRESHOLD_SECONDS:
                return {
                    'status': 'skipped',
                    'reason': 'stale_occurrence',
                    'execution_id': execution_id,
                }
        except ValueError:
            pass

    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if cache is not None:
        cache.set(
            AUTOMATION_LAST_RUN_CACHE_KEY,
            {
                'status': 'running',
                'started_at': started_at,
                'execution_id': execution_id,
            },
            ttl=AUTOMATION_LAST_RUN_TTL_SECONDS,
            tags=['automation-run'],
            name='automation-last-run',
        )

    active_controller = controller or Controller(config, state_store, cache=cache)
    try:
        result = await active_controller.control_home()
    except Exception as err:
        if cache is not None:
            cache.set(
                AUTOMATION_LAST_RUN_CACHE_KEY,
                {
                    'status': 'failed',
                    'started_at': started_at,
                    'completed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'execution_id': execution_id,
                    'error': str(err),
                },
                ttl=AUTOMATION_LAST_RUN_TTL_SECONDS,
                tags=['automation-run'],
                name='automation-last-run',
            )
            cache.expire_tag(DASHBOARD_STATUS_TAG)
        raise

    actions = result.get('actions', [])
    completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if cache is not None:
        if exec_cache_key:
            cache.set(
                exec_cache_key,
                {'completed_at': completed_at},
                ttl=AUTOMATION_EXEC_IDEMPOTENCY_TTL_SECONDS,
                tags=['schedule-exec'],
                name='automation-execution-idempotency',
            )
        cache.set(
            AUTOMATION_LAST_RUN_CACHE_KEY,
            {
                'status': 'succeeded',
                'started_at': started_at,
                'completed_at': completed_at,
                'execution_id': execution_id,
                'excess_power': result.get('excess_power'),
                'battery_percentage': result.get('battery_percentage'),
                'actions': actions,
            },
            ttl=AUTOMATION_LAST_RUN_TTL_SECONDS,
            tags=['automation-run'],
            name='automation-last-run',
        )
        if actions:
            cache.expire_tag(DASHBOARD_STATUS_TAG)

    return {
        'status': 'succeeded',
        'execution_id': execution_id,
        'excess_power': result.get('excess_power'),
        'battery_percentage': result.get('battery_percentage'),
        'actions': actions,
    }
