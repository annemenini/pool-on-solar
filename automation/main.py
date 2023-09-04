import json
import os
import sys

import asyncio

import iaqualink
from iaqualink.client import AqualinkClient
import teslapy

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


def get_excess_power(user_id: str, cache_file: str) -> int:
    tesla = teslapy.Tesla(user_id, cache_file=cache_file)
    products =  tesla.api('PRODUCT_LIST')['response']  
    energy_site_id = [p.get('energy_site_id') for p in products if p.get('resource_type') == 'battery'][0]
    data = tesla.api('SITE_DATA', {'site_id': energy_site_id})['response']
    # print(data)
    solar_power = data.get('solar_power')
    load_power = data.get('load_power')
    excess_power = solar_power - load_power
    print(f"Excess power: {solar_power} - {load_power} = {excess_power}")
    tesla.close()
    return excess_power


# Define iAquaLink script
async def update_pool(user_id: str, password: str, excess_power: int) -> None:
    """Program that log, print status and set pool temperature target of iAquaLink device."""
    print(f'######### TEST0 {user_id}, {password}')
    async with AqualinkClient(user_id, password) as client:
        print('######### TEST1')
        systems = await client.get_systems()
        print('######### TEST2')
        print(systems)
        devices = await list(systems.values())[0].get_devices()
        print(devices)

        if excess_power < 0:
            if devices['aux_1'].is_on():  # Cleaner is ON
                # await devices['aux_1'].turn_off()
                print('Turning Cleaner OFF')
            elif devices['pool_pump'].is_on():  # Filter pump is ON & Cleaner is OFF
                # await devices['pool_pump'].turn_off()
                print('Turning Filter Pump OFF')
            else:
                print('Nothing to turn OFF')
        if excess_power > 1500:            
            if not devices['pool_pump'].is_on():
                # await devices['pool_pump'].turn_on()
                print('Turning Filter Pump ON')
            elif devices['pool_pump'].is_on() and not devices['aux_1'].is_on(): 
                # await devices['aux_1'].turn_on()
                print('Turning Cleaner ON')
            else:
                print('Nothing to turn ON')
        


# Define main script
async def main(tesla_user_id: str, teslapy_cache_file: str, iaqualink_user_id: str, iaqualink_password: str):
    """Log, print status and reset tesla and iAquaLink devices."""
    print(f"Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...")
    excess_power = get_excess_power(tesla_user_id, teslapy_cache_file)
    await update_pool(iaqualink_user_id, iaqualink_password, excess_power)

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
