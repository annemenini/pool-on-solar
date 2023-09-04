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


def tesla_live_status(user_id: str, cache_file: str):
    tesla = teslapy.Tesla(user_id, cache_file=cache_file)
    data = tesla.api('SITE_DATA')['response']]
    print(data)
    tesla.close()


# Define iAquaLink script
async def main_iaqualink(user_id: str, password: str):
    """Program that log, print status and set pool temperature target of iAquaLink device."""
    async with AqualinkClient(user_id, password) as client:
        systems = await client.get_systems()
        print(systems)
        devices = await list(systems.values())[0].get_devices()
        print(devices)
        # # Turn on Filter pump
        # # await devices['pool_pump'].turn_on()
        # # Rest Thermostat pool
        # await devices['pool_set_point'].set_temperature(30)
        # devices = await list(systems.values())[0].get_devices()
        # print(devices)


# Define main script
async def main(tesla_user_id: str, teslapy_cache_file: str, iaqualink_user_id: str, iaqualink_password: str):
    """Log, print status and reset tesla and iAquaLink devices."""
    print(f"Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...")
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
