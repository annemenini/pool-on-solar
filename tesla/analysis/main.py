import datetime
import json
import os
import sys
from typing import Any, Dict

import teslapy

# [START cloudrun_jobs_env_vars]
# Retrieve Job-defined env vars
TASK_INDEX = os.getenv('CLOUD_RUN_TASK_INDEX', 0)
TASK_ATTEMPT = os.getenv('CLOUD_RUN_TASK_ATTEMPT', 0)
# Retrieve User-defined env vars
TESLA_USER_ID = os.getenv('TESLA_USER_ID')
TESLAPY_CACHE_FILE = os.getenv('TESLAPY_CACHE_FILE', '/app/config/cache.json')
# [END cloudrun_jobs_env_vars]


def get_energy_site_history(user_id: str, cache_file: str) -> Dict[str, Any]:
    """Gets live status from the energy site useful to control the iAqualink devices.

    Returns:
        Dictionary containing:
            * excess power (i.e. solar - house) in Watt
            * battery charge in %            
    """
    tesla = teslapy.Tesla(user_id, cache_file=cache_file)
    products =  tesla.api('PRODUCT_LIST')['response']  
    product = [p for p in products if p.get('resource_type') == 'battery'][0]
    energy_site = teslapy.Product(product, tesla)
    data = energy_site.get_calendar_history_data(kind='power', period='day', start_date='2023-09-05T07:00:00.000Z', end_date='2023-09-06T06:59:59.999Z', installation_timezone='US/Pacific', timezone='US/Pacific')
    print(data)
    tesla.close()
    return {}


# Define main script
def main(tesla_user_id: str, teslapy_cache_file: str) -> None:
    """Log, print status and reset tesla and iAquaLink devices."""
    print(f"Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...")
    data = get_energy_site_history(tesla_user_id, teslapy_cache_file)
    print(f"Completed Task #{TASK_INDEX}.")


# Start script
if __name__ == '__main__':
    try:
        main(TESLA_USER_ID, TESLAPY_CACHE_FILE)
    except Exception as err:
        message = (
            f'Task #{TASK_INDEX}, ' + f'Attempt #{TASK_ATTEMPT} failed: {str(err)}'
        )

        print(json.dumps({'message': message, 'severity': 'ERROR'}))
        # [START cloudrun_jobs_exit_process]
        sys.exit(1)  # Retry Job Task by exiting the process
        # [END cloudrun_jobs_exit_process]