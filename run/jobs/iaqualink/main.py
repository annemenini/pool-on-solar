# Copyright 2022 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http:#www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# [START cloudrun_jobs_quickstart]
import json
import os
import random
import sys
import time

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
TESLA_PASSWORD = os.getenv("TESLA_PASSWORD", 0)
IAQUALINK_USER_ID = os.getenv("IAQUALINK_USER_ID", 0)
IAQUALINK_PASSWORD = os.getenv("IAQUALINK_PASSWORD", 0)
# [END cloudrun_jobs_env_vars]


def genera_tesla_authentication_teslapy_cache_json(user_id: str):
    """Generate the TeslaPy Cache json required for authentication."""
    tesla = teslapy.Tesla(user_id)
    if not tesla.authorized:
        print('Use browser to login. Page Not Found will be shown at success.')
        print('Open this URL: ' + tesla.authorization_url())
        tesla.fetch_token(authorization_response=input('Enter URL after authentication: '))
    tesla.close()


def test_tesla_authentication(user_id: str, cache_file: str):
    tesla = teslapy.Tesla(user_id, cache_file=cache_file)
    vehicles = tesla.vehicle_list()
    print(vehicles[0])
    tesla.close()


# Define iAquaLink script
async def main_iaqualink(user_id: str, password: str):
    """Program that log, print status and set pool temperature target of iAquaLink device."""
    async with AqualinkClient(user_id, password) as client:
        systems = await client.get_systems()
        print(systems)
        devices = await list(systems.values())[0].get_devices()
        print(devices)
        # Turn on Filter pump
        # await devices['pool_pump'].turn_on()
        # Rest Thermostat pool
        await devices['pool_set_point'].set_temperature(30)
        devices = await list(systems.values())[0].get_devices()
        print(devices)


# Define main script
async def main(tesla_user_id: str, teslapy_cache_file: str, iaqualink_user_id: str, iaqualink_password: str):
    """Log, print status and reset tesla and iAquaLink devices."""
    print(f"Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...")
    # genera_tesla_authentication_teslapy_cache_json(tesla_user_id)
    test_tesla_authentication(tesla_user_id, teslapy_cache_file)
    await main_iaqualink(iaqualink_user_id, iaqualink_password)

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
# [END cloudrun_jobs_quickstart]
