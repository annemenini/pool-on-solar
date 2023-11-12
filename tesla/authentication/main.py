import json
import os
import sys

import teslapy

# [START cloudrun_jobs_env_vars]
# Retrieve Job-defined env vars
TASK_INDEX = os.getenv("CLOUD_RUN_TASK_INDEX", 0)
TASK_ATTEMPT = os.getenv("CLOUD_RUN_TASK_ATTEMPT", 0)
# Retrieve User-defined env vars
TESLA_USER_ID = os.getenv("TESLA_USER_ID", 0)
# [END cloudrun_jobs_env_vars]


def generate_tesla_authentication_teslapy_cache_json(user_id: str) -> None:
    """Generate the TeslaPy Cache json required for authentication."""
    tesla = teslapy.Tesla(user_id)
    if not tesla.authorized:
        print('Use browser to login. Page Not Found will be shown at success.')
        print('Open this URL: ' + tesla.authorization_url())
        tesla.fetch_token(authorization_response=input('Enter URL after authentication: '))
        print('Your TeslaPy cache file is now saved in ./cache.json')
        products =  tesla.api('PRODUCT_LIST')['response']
        if isinstance(products, dict): 
            energy_site_id = [p.get('energy_site_id') for p in products if p.get('resource_type') == 'battery'][0]
            print(f'FYI, your energy site ID is: {energy_site_id}')
    tesla.close()


# Define main script
def main(tesla_user_id: str) -> None:
    """Log, print status and reset tesla and iAquaLink devices."""
    print(f"Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...")
    genera_tesla_authentication_teslapy_cache_json(tesla_user_id)
    print(f"Completed Task #{TASK_INDEX}.")


# Start script
if __name__ == "__main__":
    try:
        main(TESLA_USER_ID)
    except Exception as err:
        message = (
            f"Task #{TASK_INDEX}, " + f"Attempt #{TASK_ATTEMPT} failed: {str(err)}"
        )

        print(json.dumps({"message": message, "severity": "ERROR"}))
        # [START cloudrun_jobs_exit_process]
        sys.exit(1)  # Retry Job Task by exiting the process
        # [END cloudrun_jobs_exit_process]
