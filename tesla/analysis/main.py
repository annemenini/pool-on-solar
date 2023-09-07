import datetime
import json
import os
import sys
from typing import Any, Dict, List, Tuple

from google.cloud import bigquery

import pytz
import teslapy

# [START cloudrun_jobs_env_vars]
# Retrieve Job-defined env vars
TASK_INDEX = os.getenv('CLOUD_RUN_TASK_INDEX', 0)
TASK_ATTEMPT = os.getenv('CLOUD_RUN_TASK_ATTEMPT', 0)
# Retrieve User-defined env vars
TESLA_USER_ID = os.getenv('TESLA_USER_ID')
TESLAPY_CACHE_FILE = os.getenv('TESLAPY_CACHE_FILE', '/app/config/cache.json')
# [END cloudrun_jobs_env_vars]
_TIMEZONE = 'US/Pacific'

def get_yesterday_start_end_date_in_utc() -> Tuple[str, str]:
    current_date = datetime.datetime.now()
    local_time_zone = pytz.timezone(_TIMEZONE)
    current_date = current_date.astimezone(local_time_zone)
    start_date = current_date - datetime.timedelta(days=1)
    start_date = start_date.replace(hour=0, minute=0, second=0)
    end_date = start_date.replace(hour=23, minute=59, second=59)
    utc_time_zone = pytz.timezone('UTC')
    start_date = start_date.astimezone(utc_time_zone)
    end_date = end_date.astimezone(utc_time_zone)
    start_date_str = start_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    end_date_str = end_date.strftime('%Y-%m-%dT%H:%M:%S.999Z')
    return start_date_str, end_date_str


def get_energy_site_history(user_id: str, cache_file: str) -> List[Dict[str, Any]]:
    """Gets historical power data from yesterday by 5-minutes increments."""
    tesla = teslapy.Tesla(user_id, cache_file=cache_file)
    products =  tesla.api('PRODUCT_LIST')['response']  
    product = [p for p in products if p.get('resource_type') == 'battery'][0]
    energy_site = teslapy.Product(product, tesla)
    start_date, end_date = get_yesterday_start_end_date_in_utc()
    data = energy_site.get_calendar_history_data(kind='power', period='day', start_date=start_date, end_date=end_date, installation_timezone=_TIMEZONE, timezone=_TIMEZONE)
    tesla.close()
    return data['time_series']


def store_data(data: List[Dict[str, Any]]) -> None:
    client = bigquery.Client()
    table_id = 'pool-on-solar.hilo_tesla.power'
    errors = client.insert_rows_json(table_id, data) 


# Define main script
def main(tesla_user_id: str, teslapy_cache_file: str) -> None:
    """Log, print status and reset tesla and iAquaLink devices."""
    print(f"Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...")
    data = get_energy_site_history(tesla_user_id, teslapy_cache_file)
    store_data(data)
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