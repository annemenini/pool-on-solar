import asyncio
import datetime
import json
import os
import sys
from typing import Any, Dict, Optional

from google.protobuf import text_format
from iaqualink.client import AqualinkClient
from iaqualink.device import AqualinkDevice
import pytz
from suntime import Sun

import config_pb2
from tesla_fleet import (
    DEFAULT_API_BASE_URL,
    TeslaFleetClient,
    create_token_store,
)


def parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


# [START cloudrun_jobs_env_vars]
# Retrieve Job-defined env vars
TASK_INDEX = os.getenv('CLOUD_RUN_TASK_INDEX', '0')
TASK_ATTEMPT = os.getenv('CLOUD_RUN_TASK_ATTEMPT', '0')
DRY_RUN = parse_bool_env('DRY_RUN', default=False)
# Retrieve User-defined env vars
CONFIG_FILE = os.getenv('CONFIG_FILE', '/app/config/config.pbtxt')
TESLA_CLIENT_ID = os.getenv('TESLA_CLIENT_ID')
TESLA_TOKEN_SECRET_NAME = os.getenv('TESLA_TOKEN_SECRET_NAME')
TESLA_API_BASE_URL = os.getenv('TESLA_API_BASE_URL')
TESLA_TOKEN_FILE = os.getenv('TESLA_TOKEN_FILE')
TESLA_USER_ID = os.getenv('TESLA_USER_ID')  # Legacy optional field
ENERGY_SITE_ID = os.getenv('ENERGY_SITE_ID')
IAQUALINK_USER_ID = os.getenv('IAQUALINK_USER_ID')
IAQUALINK_PASSWORD = os.getenv('IAQUALINK_PASSWORD')
# [END cloudrun_jobs_env_vars]


async def try_switch_device(device: AqualinkDevice, mode: str) -> bool:
    """Checks an iAquaLink device status and toggles it if needed.

    Args:
        device: AqualinkDevice
        mode: 'on' or 'off'
    Returns:
        Boolean whether the device was toggled.
    """
    if mode == 'off' and device.is_on:
        if not DRY_RUN:
            print(f'Trying to turn {device.label} OFF (state: {device.state})')
            await device.turn_off()
        print(f'Turning {device.label} OFF (state: {device.state})')
        return True
    elif mode == 'on' and not device.is_on:
        if not DRY_RUN:
            print(f'Trying to turn {device.label} ON (state: {device.state})')
            await device.turn_on()
        print(f'Turning {device.label} ON (state: {device.state})')
        return True
    return False


class Controller:
    def __init__(
        self,
        config_file: str,
        tesla_client_id: Optional[str],
        energy_site_id: Optional[str],
        iaqualink_user_id: Optional[str],
        iaqualink_password: Optional[str],
        tesla_token_secret_name: Optional[str] = None,
        tesla_api_base_url: Optional[str] = None,
        tesla_token_file: Optional[str] = None,
        tesla_user_id: Optional[str] = None,
    ):
        with open(config_file, 'rb') as f:
            self._config = text_format.Parse(f.read(), config_pb2.Config())

        self._tesla_client_id = tesla_client_id
        if tesla_user_id:
            self._config.tesla.user_id = tesla_user_id
        if energy_site_id:
            self._config.tesla.energy_site_id = energy_site_id
        if tesla_token_secret_name:
            self._config.tesla.token_secret_name = tesla_token_secret_name
        if tesla_api_base_url:
            self._config.tesla.api_base_url = tesla_api_base_url
        if tesla_token_file:
            self._config.tesla.cache_file = tesla_token_file
        if iaqualink_user_id:
            self._config.iaqualink.user_id = iaqualink_user_id
        if iaqualink_password:
            self._config.iaqualink.password = iaqualink_password

    def get_energy_site_status(self) -> Dict[str, Any]:
        """Gets live status from the Tesla energy site useful to control iAquaLink devices.

        Returns:
            Dictionary containing:
                * excess_power (i.e. solar - house) in Watts
                * battery_percentage in %
        """
        token_store = create_token_store(
            secret_name=self._config.tesla.token_secret_name or None,
            cache_file=self._config.tesla.cache_file or None,
        )
        api_base_url = self._config.tesla.api_base_url or DEFAULT_API_BASE_URL

        with TeslaFleetClient(
            client_id=self._tesla_client_id or '',
            token_store=token_store,
            api_base_url=api_base_url,
        ) as tesla:
            if not self._config.tesla.energy_site_id:
                self._config.tesla.energy_site_id = tesla.get_energy_site_id()
            data = tesla.get_live_status(self._config.tesla.energy_site_id)

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
            # Solar-only sites without a Powerwall omit percentage_charged.
            battery_percentage = 100.0
        print(
            f'Excess power: {solar_power} - {load_power} = {excess_power}, '
            f'battery percentage: {battery_percentage}'
        )
        return {'excess_power': excess_power, 'battery_percentage': battery_percentage}

    def convert_to_local_time(self, time: datetime.datetime) -> datetime.datetime:
        local_time_zone = pytz.timezone(self._config.location.timezone)
        return time.astimezone(local_time_zone)

    def _now_local(self) -> datetime.datetime:
        return self.convert_to_local_time(datetime.datetime.now(datetime.timezone.utc))

    def get_forced_operation_mode(self) -> str:
        """Checks if the current time is within the pool forced on/off operating window."""
        current_time = self._now_local()
        if (
            self._config.iaqualink.pool.min_operating_window_start >= 0
            and self._config.iaqualink.pool.min_operating_window_end >= 0
        ):
            if (
                current_time.hour >= self._config.iaqualink.pool.min_operating_window_start
                and current_time.hour < self._config.iaqualink.pool.min_operating_window_end
            ):
                return 'on'
        if (
            current_time.hour < self._config.iaqualink.pool.max_operating_window_start
            or current_time.hour >= self._config.iaqualink.pool.max_operating_window_end
        ):
            return 'off'
        return 'flexible'

    async def update_pool(
        self,
        devices: Dict[str, AqualinkDevice],
        excess_power: Optional[float],
        battery_percentage: float,
    ) -> None:
        """Switches various iAquaLink pool devices.

        In particular:
        * Turn off all pool devices outside of the operating hours and return
        * Turn on all pool devices during the mandatory minimum operating window
        * Turn on one pool device if enough power is available
        * Turn off one pool device if too little power is available
        """
        forced_operation_mode = self.get_forced_operation_mode()
        if forced_operation_mode == 'off':
            for device_key in reversed(self._config.iaqualink.pool.devices):
                await try_switch_device(devices[device_key], 'off')
            return
        if forced_operation_mode == 'on':
            for device_key in self._config.iaqualink.pool.devices:
                await try_switch_device(devices[device_key], 'on')
            return

        if excess_power is None:
            return

        if (
            battery_percentage < self._config.iaqualink.pool.minimum_battery
            or excess_power < self._config.iaqualink.pool.deactivation_excess_power
        ):
            is_consumption_reduced = False
            for device_key in reversed(self._config.iaqualink.pool.devices):
                is_consumption_reduced = await try_switch_device(devices[device_key], 'off')
                if is_consumption_reduced:
                    break
            if not is_consumption_reduced:
                print('Pool System: Nothing to turn OFF')
        elif excess_power < self._config.iaqualink.pool.activation_excess_power:
            print('Pool System: Standby')
        else:
            is_consumption_increased = False
            for device_key in self._config.iaqualink.pool.devices:
                is_consumption_increased = await try_switch_device(devices[device_key], 'on')
                if is_consumption_increased:
                    break
            if not is_consumption_increased:
                print('Pool System: Nothing to turn ON')

    async def update_lights(
        self,
        devices: Dict[str, AqualinkDevice],
        battery_percentage: float,
    ) -> None:
        """Turns on the lights after sunset if enough battery is available and turns them off after operating hours."""
        sun = Sun(self._config.location.latitude, self._config.location.longitude)
        current_time = self._now_local()

        # Get today's sunset in UTC for the local calendar date, then convert to local time.
        sunset_time = self.convert_to_local_time(sun.get_sunset_time(current_time.date()))
        turn_on_time = sunset_time
        turn_off_time = current_time.replace(
            hour=self._config.iaqualink.light.max_operating_window_end,
            minute=0,
            second=0,
            microsecond=0,
        )
        if current_time.time() < turn_on_time.time() or current_time.time() > turn_off_time.time():
            for light_key in self._config.iaqualink.light.devices:
                await try_switch_device(devices[light_key], 'off')
        elif battery_percentage > self._config.iaqualink.light.minimum_battery:
            for light_key in self._config.iaqualink.light.devices:
                await try_switch_device(devices[light_key], 'on')

    async def update_iaqualink(self, energy_site_status: Optional[Dict[str, Any]]) -> None:
        """Connects to the iAquaLink system and controls the pool and light devices."""
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
            # Assumes enough battery is available if connection to energy site cannot be established.
            battery_percentage = (
                energy_site_status['battery_percentage'] if energy_site_status else 100.0
            )
            await self.update_pool(devices, excess_power, battery_percentage)
            await self.update_lights(devices, battery_percentage)

    async def control_home(self) -> None:
        """Tries to retrieve Tesla energy site status and adjusts iAquaLink devices accordingly.

        If energy site connection fails, still ensure devices are turned off outside of operating
        hours before re-raising the error.
        """
        try:
            energy_site_status = self.get_energy_site_status()
            await self.update_iaqualink(energy_site_status)
        except Exception as err:
            # Try to control the pool without the energy site status before failing.
            await self.update_iaqualink(None)
            raise err


async def main(
    config_file: str,
    tesla_client_id: Optional[str],
    energy_site_id: Optional[str],
    iaqualink_user_id: Optional[str],
    iaqualink_password: Optional[str],
    tesla_token_secret_name: Optional[str] = None,
    tesla_api_base_url: Optional[str] = None,
    tesla_token_file: Optional[str] = None,
    tesla_user_id: Optional[str] = None,
) -> None:
    """Load config and update home devices according to its energy status."""
    print(f'Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...')
    controller = Controller(
        config_file=config_file,
        tesla_client_id=tesla_client_id,
        energy_site_id=energy_site_id,
        iaqualink_user_id=iaqualink_user_id,
        iaqualink_password=iaqualink_password,
        tesla_token_secret_name=tesla_token_secret_name,
        tesla_api_base_url=tesla_api_base_url,
        tesla_token_file=tesla_token_file,
        tesla_user_id=tesla_user_id,
    )
    await controller.control_home()
    print(f'Completed Task #{TASK_INDEX}.')


# Start script
if __name__ == '__main__':
    try:
        asyncio.run(
            main(
                config_file=CONFIG_FILE,
                tesla_client_id=TESLA_CLIENT_ID,
                energy_site_id=ENERGY_SITE_ID,
                iaqualink_user_id=IAQUALINK_USER_ID,
                iaqualink_password=IAQUALINK_PASSWORD,
                tesla_token_secret_name=TESLA_TOKEN_SECRET_NAME,
                tesla_api_base_url=TESLA_API_BASE_URL,
                tesla_token_file=TESLA_TOKEN_FILE,
                tesla_user_id=TESLA_USER_ID,
            )
        )
    except Exception as err:
        message = f'Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT} failed: {str(err)}'

        print(json.dumps({'message': message, 'severity': 'ERROR'}))
        # [START cloudrun_jobs_exit_process]
        sys.exit(1)  # Retry Job Task by exiting the process
        # [END cloudrun_jobs_exit_process]
