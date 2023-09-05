import datetime
import json
import os
import sys
from typing import Any, Dict

import asyncio

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
TESLA_USER_ID = os.getenv('TESLA_USER_ID', 0)
TESLAPY_CACHE_FILE = os.getenv('TESLAPY_CACHE_FILE', 0)
IAQUALINK_USER_ID = os.getenv('IAQUALINK_USER_ID', 0)
IAQUALINK_PASSWORD = os.getenv('IAQUALINK_PASSWORD', 0)
# [END cloudrun_jobs_env_vars]

_POOL_DEVICES = ['pool_pump', 'aux_1']  # By decreasing order of priority
_POOL_ACTIVATION_EXCESS_POWER = 2000  # Minimum excess power required to activate a pool device (in Watt)
_POOL_DEACTIVATION_EXCESS_POWER = 500  # Excess power threshold below which pool devices start to be deactivated (in Watt)
_POOL_OPERATION_WINDOW_START = 8  # Pool will be shut off in any case after 18h00
_POOL_OPERATION_WINDOW_END = 18  # Pool will not turn on before 8h00
_LIGHT_DEVICES = ['aux_B1', 'aux_B3', 'aux_B4']
_LIGHT_MINIMUM_BATTERY_REQUIRED = 50  # %
_LIGHT_BLACKOUT_WINDOW_START = 22  # Lights will be shut off in any case after 22h00
_LATITUDE = 37.3861  # Mountain View, CA, United States Northern Latitude
_LONGITUDE = -122.0839  # Mountain View, CA, United States Eastern Longitude
_TIME_ZONE = 'US/Pacific'  # pytz time zone


def get_energy_system_status(user_id: str, cache_file: str) -> Dict[str, Any]:
    tesla = teslapy.Tesla(user_id, cache_file=cache_file)
    products =  tesla.api('PRODUCT_LIST')['response']  
    energy_site_id = [p.get('energy_site_id') for p in products if p.get('resource_type') == 'battery'][0]
    data = tesla.api('SITE_DATA', {'site_id': energy_site_id})['response']
    # print(data)
    solar_power = data.get('solar_power')
    load_power = data.get('load_power')
    excess_power = solar_power - load_power
    battery_percentage = data.get('percentage_charged')
    print(f'Excess power: {solar_power} - {load_power} = {excess_power}, battery percentage: {battery_percentage}')
    tesla.close()
    return {'excess_power': excess_power, 'battery_percentage': battery_percentage}


async def try_switch_device(device: AqualinkDevice, mode: str) -> bool:
    if mode == 'off' and device.is_on:
        if not DRY_RUN:
            await device.turn_off()
        print(f'Turning {device.label} OFF')
        return True
    elif mode == 'on' and not device.is_on:
        if not DRY_RUN:
            await device.turn_on()
        print(f'Turning {device.label} ON')
        return True
    return False


def convert_to_local_time(time: datetime.datetime) -> datetime.datetime:
    local_time_zone = pytz.timezone(_TIME_ZONE)
    return time.astimezone(local_time_zone)


def is_pool_operable() -> None:
    current_time = convert_to_local_time(datetime.datetime.now())
    return current_time.hour >= _POOL_OPERATION_WINDOW_START and current_time.hour < _POOL_OPERATION_WINDOW_END


async def update_pool(devices: Dict[str, AqualinkDevice], excess_power: int) -> None:
    """Program that log, print status and set pool temperature target of iAquaLink device."""
    if not is_pool_operable():
        for device_key in reversed(_POOL_DEVICES):
            await try_switch_device(devices[device_key], 'off')
        return
    
    if excess_power < _POOL_DEACTIVATION_EXCESS_POWER:
        is_consumption_reduced = False
        for device_key in reversed(_POOL_DEVICES):
            is_consumption_reduced = await try_switch_device(devices[device_key], 'off')
            if is_consumption_reduced:
                break
        if not is_consumption_reduced:
            print('Pool System: Nothing to turn OFF')
    elif excess_power < _POOL_ACTIVATION_EXCESS_POWER:  
        print('Pool System: Standby')
    else:       
        is_consumption_increased = False
        for device_key in _POOL_DEVICES:
            is_consumption_increased = await try_switch_device(devices[device_key], 'on')
            if is_consumption_increased:
                break
        if not is_consumption_increased:
            print('Pool System: Nothing to turn ON')


async def update_lights(devices: Dict[str, AqualinkDevice], battery_percentage: float) -> None:
    sun = Sun(_LATITUDE, _LONGITUDE)

    # Get today's sunrise and sunset in UTC
    sunset_time = convert_to_local_time(sun.get_sunset_time())
    turn_on_time = sunset_time
    current_time = convert_to_local_time(datetime.datetime.now())
    turn_off_time = current_time.replace(hour=_LIGHT_BLACKOUT_WINDOW_START, minute=0, second=0)
    # print(f'Current time: {current_time}, turn on time: {turn_on_time}, turn off time: {turn_off_time}')
    if current_time.time() < turn_on_time.time() or current_time.time() > turn_off_time.time():
        for light_key in _LIGHT_DEVICES:
            await try_switch_device(devices[light_key], 'off')
    elif battery_percentage > _LIGHT_MINIMUM_BATTERY_REQUIRED:
        for light_key in _LIGHT_DEVICES:
            await try_switch_device(devices[light_key], 'on')


async def control_iaqualink(user_id: str, password: str, energy_system_status: Dict[str, Any]) -> None:
    async with AqualinkClient(user_id, password) as client:
        systems = await client.get_systems()
        devices = await list(systems.values())[0].get_devices()
        await update_pool(devices, energy_system_status['excess_power'])
        await update_lights(devices, energy_system_status['battery_percentage'])
        

async def main(tesla_user_id: str, teslapy_cache_file: str, iaqualink_user_id: str, iaqualink_password: str):
    """Log, print status and reset tesla and iAquaLink devices."""
    print(f'Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...')
    energy_system_status = get_energy_system_status(tesla_user_id, teslapy_cache_file)
    await control_iaqualink(iaqualink_user_id, iaqualink_password, energy_system_status)
    print(f'Completed Task #{TASK_INDEX}.')


# Start script
if __name__ == '__main__':
    try:
        asyncio.run(main(TESLA_USER_ID, TESLAPY_CACHE_FILE, IAQUALINK_USER_ID, IAQUALINK_PASSWORD))
    except Exception as err:
        message = (
            f'Task #{TASK_INDEX}, ' + f'Attempt #{TASK_ATTEMPT} failed: {str(err)}'
        )

        print(json.dumps({'message': message, 'severity': 'ERROR'}))
        # [START cloudrun_jobs_exit_process]
        sys.exit(1)  # Retry Job Task by exiting the process
        # [END cloudrun_jobs_exit_process]
