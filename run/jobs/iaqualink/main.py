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

import iaqualink
from iaqualink.client import AqualinkClient
import asyncio

# [START cloudrun_jobs_env_vars]
# Retrieve Job-defined env vars
TASK_INDEX = os.getenv("CLOUD_RUN_TASK_INDEX", 0)
TASK_ATTEMPT = os.getenv("CLOUD_RUN_TASK_ATTEMPT", 0)
# Retrieve User-defined env vars
IAQUALINK_USER_ID = os.getenv("IAQUALINK_USER_ID", 0)
IAQUALINK_PASSWORD = os.getenv("IAQUALINK_PASSWORD", 0)
# [END cloudrun_jobs_env_vars]


# Define main script
async def main(user_id: str, password: str):
    """Program that print status of iAquaLink device."""
    print(f"Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...")
    async with AqualinkClient(user_id, password) as c:
        s = await c.get_systems()
        print(s)
        d = await list(s.values())[0].get_devices()
        print(d)
        # Turn on Filter pump
        # await d['pool_pump'].turn_on()
        # Rest Thermostat pool
        await d['pool_set_point'].set_temperature(30)
        d = await list(s.values())[0].get_devices()
        print(d)

    print(f"Completed Task #{TASK_INDEX}.")


# Start script
if __name__ == "__main__":
    try:
        asyncio.run(main(IAQUALINK_USER_ID, IAQUALINK_PASSWORD))
    except Exception as err:
        message = (
            f"Task #{TASK_INDEX}, " + f"Attempt #{TASK_ATTEMPT} failed: {str(err)}"
        )

        print(json.dumps({"message": message, "severity": "ERROR"}))
        # [START cloudrun_jobs_exit_process]
        sys.exit(1)  # Retry Job Task by exiting the process
        # [END cloudrun_jobs_exit_process]
# [END cloudrun_jobs_quickstart]
