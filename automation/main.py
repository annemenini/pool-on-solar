import datetime
import json
import os
import sys
from typing import Any, Dict, Optional

import asyncio
from google.protobuf import text_format

import config_pb2
import iaqualink
from iaqualink.client import AqualinkClient
from iaqualink.device import AqualinkDevice
import pytz
import teslapy
from suntime import Sun

# [START cloudrun_jobs_env_vars]
# Retrieve Job-defined env vars
TASK_INDEX = os.getenv('CLOUD_RUN_TASK_INDEX', 0)
TASK_ATTEMPT = os.getenv('CLOUD_RUN_TASK_ATTEMPT', 0)
DRY_RUN = os.getenv('DRY_RUN', default=False)
# Retrieve User-defined env vars
CONFIG_FILE = os.getenv('CONFIG_FILE', '/app/config/config.pbtxt')
TESLA_USER_ID = os.getenv('TESLA_USER_ID')
ENERGY_SITE_ID = os.getenv('ENERGY_SITE_ID')
IAQUALINK_USER_ID = os.getenv('IAQUALINK_USER_ID')
IAQUALINK_PASSWORD = os.getenv('IAQUALINK_PASSWORD')
# [END cloudrun_jobs_env_vars]


async def try_switch_device(device: AqualinkDevice, mode: str) -> bool:
    """Checks a iAqualink device status and toggles it if needed.
    
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
    def __init__(self, config_file: str, tesla_user_id: str, energy_site_id: str, iaqualink_user_id: str, iaqualink_password: str):
        with open(config_file, 'rb') as f:
            self._config = text_format.Parse(f.read(), config_pb2.Config())
        self._config.tesla.user_id = tesla_user_id
        self._config.tesla.energy_site_id = energy_site_id
        self._config.iaqualink.user_id = iaqualink_user_id
        self._config.iaqualink.password = iaqualink_password

    def get_energy_site_status(self) -> Dict[str, Any]:
        """Gets live status from the energy site useful to control the iAqualink devices.

        Returns:
            Dictionary containing:
                * excess power (i.e. solar - house) in Watt
                * battery charge in %            
        """
        tesla = teslapy.Tesla(self._config.tesla.user_id, cache_file=self._config.tesla.cache_file)
        if not self._config.tesla.energy_site_id:
            products =  tesla.api('PRODUCT_LIST')['response']
            if isinstance(products, dict): 
                self._config.tesla.energy_site_id = [p.get('energy_site_id') for p in products if p.get('resource_type') == 'battery'][0]
            else:
                raise ValueError('Could not retrieve energy_site_id.')
        data = tesla.api('SITE_DATA', {'site_id': self._config.tesla.energy_site_id})['response']
        solar_power = data.get('solar_power')
        load_power = data.get('load_power')
        excess_power = solar_power - load_power
        battery_percentage = data.get('percentage_charged')
        print(f'Excess power: {solar_power} - {load_power} = {excess_power}, battery percentage: {battery_percentage}')
        tesla.close()
        return {'excess_power': excess_power, 'battery_percentage': battery_percentage}

    def convert_to_local_time(self, time: datetime.datetime) -> datetime.datetime:
        local_time_zone = pytz.timezone(self._config.location.timezone)
        return time.astimezone(local_time_zone)

    def get_forced_operation_mode(self) -> None:
        """Checks if the current time is within the pool forced on/off operating window."""
        current_time = self.convert_to_local_time(datetime.datetime.now())
        if self._config.iaqualink.pool.min_operating_window_start >= 0 and self._config.iaqualink.pool.min_operating_window_end >= 0:
            if current_time.hour >= self._config.iaqualink.pool.min_operating_window_start and current_time.hour < self._config.iaqualink.pool.min_operating_window_end:
                return 'on'
        if current_time.hour < self._config.iaqualink.pool.max_operating_window_start or current_time.hour >= self._config.iaqualink.pool.max_operating_window_end:
            return 'off'
        return 'flexible'

    async def update_pool(self, devices: Dict[str, AqualinkDevice], excess_power: Optional[int], battery_percentage: float) -> None:
        """Switches various iAqualink devices.
        
        In particular:
        * Turn off all pool devices outside of the operating hours and returns
        * Turn on one pool device if enough power is available
        * Turn off one pool device if too little power is available

        Args:
            devices: available devices
            excess power
        """
        get_forced_operation_mode = self.get_forced_operation_mode()
        if get_forced_operation_mode == 'off':
            for device_key in reversed(self._config.iaqualink.pool.devices):
                await try_switch_device(devices[device_key], 'off')
            return
        if get_forced_operation_mode == 'on':
            for device_key in self._config.iaqualink.pool.devices:
                await try_switch_device(devices[device_key], 'on')
            return

        if excess_power is None:
            return
        
        if battery_percentage < self._config.iaqualink.pool.minimum_battery or excess_power < self._config.iaqualink.pool.deactivation_excess_power:
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

    async def update_lights(self, devices: Dict[str, AqualinkDevice], battery_percentage: float) -> None:
        """Turns on the lights after sunset if enough battery is available and turns them off after operating hours."""
        sun = Sun(self._config.location.latitude, self._config.location.longitude)

        # Get today's sunrise and sunset in UTC
        sunset_time = self.convert_to_local_time(sun.get_sunset_time())
        turn_on_time = sunset_time
        current_time = self.convert_to_local_time(datetime.datetime.now())
        turn_off_time = current_time.replace(hour=self._config.iaqualink.light.max_operating_window_end, minute=0, second=0)
        if current_time.time() < turn_on_time.time() or current_time.time() > turn_off_time.time():
            for light_key in self._config.iaqualink.light.devices:
                await try_switch_device(devices[light_key], 'off')
        elif battery_percentage > self._config.iaqualink.light.minimum_battery:
            for light_key in self._config.iaqualink.light.devices:
                await try_switch_device(devices[light_key], 'on')

    async def update_iaqualink(self, energy_site_status: Optional[Dict[str, Any]]) -> None:
        """Connects to the iAqualink system and controls the pool and light devices."""
        async with AqualinkClient(self._config.iaqualink.user_id, self._config.iaqualink.password) as client:
            systems = await client.get_systems()
            devices = await list(systems.values())[0].get_devices()
            excess_power = energy_site_status['excess_power'] if energy_site_status else None
            battery_percentage = energy_site_status['battery_percentage'] if energy_site_status else 100  # Assumes enough battery is available if connection to energy site cannot be established 
            await self.update_pool(devices, excess_power, battery_percentage)
            await self.update_lights(devices, battery_percentage)
        
    async def control_home(self) -> None:
        """Tries to retrieve tesla energy site status and adjusts iAquaLink devices accordingly. 
        
        If energy site connection fails, still ensure devices are turned off outside of operating hours before re-throwing the error.
        """
        try:
            energy_site_status = self.get_energy_site_status()
            await self.update_iaqualink(energy_site_status)
        except Exception as err:
            # Try to control the pool without the energy site status before failing
            await self.update_iaqualink(None)
            raise err


async def main(config_file: str, tesla_user_id: str, energy_site_id: str, iaqualink_user_id: str, iaqualink_password: str) -> bool:
    """Load config and update home devices according to its energy status."""
    print(f'Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...')
    controller = Controller(config_file, tesla_user_id, energy_site_id, iaqualink_user_id, iaqualink_password)
    await controller.control_home()
    print(f'Completed Task #{TASK_INDEX}.')   


# Start script
if __name__ == '__main__':
    try:
        asyncio.run(main(CONFIG_FILE, TESLA_USER_ID, ENERGY_SITE_ID, IAQUALINK_USER_ID, IAQUALINK_PASSWORD))
    except Exception as err:
        message = (
            f'Task #{TASK_INDEX}, ' + f'Attempt #{TASK_ATTEMPT} failed: {str(err)}'
        )

        print(json.dumps({'message': message, 'severity': 'ERROR'}))
        # [START cloudrun_jobs_exit_process]
        sys.exit(1)  # Retry Job Task by exiting the process
        # [END cloudrun_jobs_exit_process]
