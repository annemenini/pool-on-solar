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
TIMEZONE = os.getenv('TIMEZONE', 'UTC')
INITIALIZE_TABLES = os.getenv('INITIALIZE_TABLES', false)
DATASET_ID = os.getenv('DATASET_ID')
POWER_TABLE_ID = os.getenv('POWER_TABLE_ID')
ENERGY_TABLE_ID = os.getenv('ENERGY_TABLE_ID')
# [END cloudrun_jobs_env_vars]


def initialize_bigquery_tables() -> Tuple[str, str]:
    client = bigquery.Client()
    project = client.project
    dataset_ref = bigquery.DatasetReference(project, DATASET_ID)

    table_ref = dataset_ref.table('power')
    schema = [
        bigquery.SchemaField("timestamnp", "TIMESTAMP"),
        bigquery.SchemaField("solar_power", "FLOAT"),
        bigquery.SchemaField("battery_power", "FLOAT"),
        bigquery.SchemaField("grid_power", "FLOAT"),
        bigquery.SchemaField("grid_services_power", "FLOAT"),
        bigquery.SchemaField("generator_power", "FLOAT"),
    ]
    table = bigquery.Table(table_ref, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.MONTH,
        field="timestamnp",  # name of column to use for partitioning
        expiration_ms=7776000000,
    )  # 90 days

    table = client.create_table(table)


def get_yesterday_start_end_date_in_utc() -> Tuple[str, str]:
    current_date = datetime.datetime.now()
    local_time_zone = pytz.timezone(TIMEZONE)
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


def get_energy_site_history(user_id: str, cache_file: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Gets historical power data from yesterday by 5-minutes increments."""
    tesla = teslapy.Tesla(user_id, cache_file=cache_file)
    products =  tesla.api('PRODUCT_LIST')['response']  
    product = [p for p in products if p.get('resource_type') == 'battery'][0]
    energy_site = teslapy.Product(product, tesla)
    start_date, end_date = get_yesterday_start_end_date_in_utc()
    power_data = energy_site.get_calendar_history_data(kind='power', period='day', start_date=start_date, end_date=end_date, installation_timezone=TIMEZONE, timezone=TIMEZONE)
    energy_data = energy_site.get_calendar_history_data(kind='energy', period='day', start_date=start_date, end_date=end_date, installation_timezone=TIMEZONE, timezone=TIMEZONE)
    tesla.close()
    return power_data['time_series'], energy_data['time_series']


def store_data(power_data: List[Dict[str, Any]], energy_data: List[Dict[str, Any]]) -> None:
    client = bigquery.Client()
    client.insert_rows_json(POWER_TABLE_ID, power_data)
    client.insert_rows_json(ENERGY_TABLE_ID, energy_data) 


# Define main script
def main(tesla_user_id: str, teslapy_cache_file: str) -> None:
    """Log, print status and reset tesla and iAquaLink devices."""
    print(f"Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...")
    if INITIALIZE_TABLES:
        initialize_bigquery_tables()
    else:
        power_data, energy_data = get_energy_site_history(tesla_user_id, teslapy_cache_file)
        store_data(power_data, energy_data)
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