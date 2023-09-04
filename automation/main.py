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
TASK_INDEX = os.getenv("CLOUD_RUN_TASK_INDEX", 0)
TASK_ATTEMPT = os.getenv("CLOUD_RUN_TASK_ATTEMPT", 0)
# Retrieve User-defined env vars
TESLA_USER_ID = os.getenv("TESLA_USER_ID", 0)
TESLAPY_CACHE_FILE = os.getenv("TESLAPY_CACHE_FILE", 0)
IAQUALINK_USER_ID = os.getenv("IAQUALINK_USER_ID", 0)
IAQUALINK_PASSWORD = os.getenv("IAQUALINK_PASSWORD", 0)
# [END cloudrun_jobs_env_vars]


def get_enery_system_status(user_id: str, cache_file: str) -> Dict[str, Any]:
    tesla = teslapy.Tesla(user_id, cache_file=cache_file)
    products =  tesla.api('PRODUCT_LIST')['response']  
    energy_site_id = [p.get('energy_site_id') for p in products if p.get('resource_type') == 'battery'][0]
    data = tesla.api('SITE_DATA', {'site_id': energy_site_id})['response']
    # print(data)
    solar_power = data.get('solar_power')
    load_power = data.get('load_power')
    excess_power = solar_power - load_power
    battery_percentage = data.get('percentage_charged')
    print(f"Excess power: {solar_power} - {load_power} = {excess_power}, battery percentage: {battery_percentage}")
    tesla.close()
    return {'excess_power': excess_power, 'battery_percentage': battery_percentage}


async def try_switch_device(device: AqualinkDevice, mode: str) -> bool:
    if mode == 'off' and device.is_on:
        await device.turn_off()
        print(f'Turning {device.label} OFF')
        return True
    elif mode == 'on' and not device.is_on:
        await device.turn_on()
        print(f'Turning {device.label} ON')
        return True
    return False


# Define iAquaLink script
async def update_pool(devices: Dict[str, AqualinkDevice], excess_power: int) -> None:
    """Program that log, print status and set pool temperature target of iAquaLink device."""
    if excess_power < 0:
        is_consumption_reduced = (await try_switch_device(devices['aux_1'], 'off')) or (await try_switch_device(devices['pool_pump'], 'off'))
        if not is_consumption_reduced:
            print('Pool System: Nothing to turn OFF')
    elif excess_power < 1500:  
        print('Pool System: Standby')
    else:       
        is_consumption_increased = (await try_switch_device(devices['pool_pump'], 'on')) or (await try_switch_device(devices['aux_1'], 'on'))
        if not is_consumption_increased:
            print('Pool System: Nothing to turn ON')


def convert_to_local_time(time: datetime.datetime) -> datetime.datetime:
    local_time_zone = pytz.timezone('US/Pacific')
    return time.astimezone(local_time_zone)


async def update_lights(devices: Dict[str, AqualinkDevice], battery_percentage: float) -> None:
    # Mountain View, CA, United States
    latitude = 37.3861
    longitude = -122.0839

    sun = Sun(latitude, longitude)

    # Get today's sunrise and sunset in UTC
    sunset_time = convert_to_local_time(sun.get_sunset_time())
    turn_on_time = sunset_time
    current_time = convert_to_local_time(datetime.datetime.now())
    turn_off_time = current_time.replace(hour=22, minute=0)
    # print(f'Current time: {current_time}, turn on time: {turn_on_time}, turn off time: {turn_off_time}')
    if current_time.time() < turn_on_time.time() or current_time.time() > turn_off_time.time():
        has_turned_some_lights_off = False
        has_turned_some_lights_off &= await try_switch_device(devices['aux_B1'], 'off')
        has_turned_some_lights_off &= await try_switch_device(devices['aux_B3'], 'off')
        has_turned_some_lights_off &= await try_switch_device(devices['aux_B4'], 'off')
        if has_turned_some_lights_off:
            print('Turned some lights OFF')
    elif battery_percentage > 50:
        has_turned_some_lights_on = False
        has_turned_some_lights_on &= await try_switch_device(devices['aux_B1'], 'on')
        has_turned_some_lights_on &= await try_switch_device(devices['aux_B3'], 'on')
        has_turned_some_lights_on &= await try_switch_device(devices['aux_B4'], 'on')
        if has_turned_some_lights_on:
            print('Turned some lights ON')


async def control_iaqualink(user_id: str, password: str, enery_system_status: Dict[str, Any]) -> None:
    async with AqualinkClient(user_id, password) as client:
        systems = await client.get_systems()
        devices = await list(systems.values())[0].get_devices()
        await update_pool(devices, enery_system_status['excess_power'])
        await update_lights(devices, enery_system_status['battery_percentage'])
        

# Define main script
async def main(tesla_user_id: str, teslapy_cache_file: str, iaqualink_user_id: str, iaqualink_password: str):
    """Log, print status and reset tesla and iAquaLink devices."""
    print(f"Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...")
    enery_system_status = get_enery_system_status(tesla_user_id, teslapy_cache_file)
    await control_iaqualink(iaqualink_user_id, iaqualink_password, enery_system_status)
    print(f"Completed Task #{TASK_INDEX}.")


# Start script
if __name__ == "__main__":
    try:
        asyncio.run(main(TESLA_USER_ID, TESLAPY_CACHE_FILE, IAQUALINK_USER_ID, IAQUALINK_PASSWORD))
    except Exception as err:
        message = (
            f"Task #{TASK_INDEX}, " + f"Attempt #{TASK_ATTEMPT} failed: {str(err)}"
        )

        print(json.dumps({"message": message, "severity": "ERROR"}))
        # [START cloudrun_jobs_exit_process]
        sys.exit(1)  # Retry Job Task by exiting the process
        # [END cloudrun_jobs_exit_process]
