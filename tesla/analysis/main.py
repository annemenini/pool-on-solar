import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from google.cloud import bigquery
import pytz

from tesla_fleet import (
    DEFAULT_API_BASE_URL,
    TeslaFleetClient,
    create_token_store,
)


def parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


# [START cloudrun_jobs_env_vars]
# Retrieve Job-defined env vars
TASK_INDEX = os.getenv('CLOUD_RUN_TASK_INDEX', '0')
TASK_ATTEMPT = os.getenv('CLOUD_RUN_TASK_ATTEMPT', '0')
# Retrieve User-defined env vars
TESLA_CLIENT_ID = os.getenv('TESLA_CLIENT_ID')
TESLA_TOKEN_SECRET_NAME = os.getenv('TESLA_TOKEN_SECRET_NAME')
TESLA_API_BASE_URL = os.getenv('TESLA_API_BASE_URL', DEFAULT_API_BASE_URL)
TESLA_TOKEN_FILE = os.getenv('TESLA_TOKEN_FILE') or os.getenv(
    'TESLAPY_CACHE_FILE', '/app/config/cache.json'
)
ENERGY_SITE_ID = os.getenv('ENERGY_SITE_ID')
TIMEZONE = os.getenv('TIMEZONE', 'UTC')
INITIALIZE_TABLES = parse_bool_env('INITIALIZE_TABLES', default=False)
DATASET_ID = os.getenv('DATASET_ID')
POWER_TABLE_ID = os.getenv('POWER_TABLE_ID')
ENERGY_TABLE_ID = os.getenv('ENERGY_TABLE_ID')
# [END cloudrun_jobs_env_vars]


def initialize_bigquery_tables() -> None:
    if not DATASET_ID:
        raise ValueError('DATASET_ID must be set when INITIALIZE_TABLES is true.')
    client = bigquery.Client()
    project = client.project
    dataset_ref = bigquery.DatasetReference(project, DATASET_ID)

    power_table_ref = dataset_ref.table('power')
    power_schema = [
        bigquery.SchemaField('timestamp', 'TIMESTAMP'),
        bigquery.SchemaField('solar_power', 'FLOAT'),
        bigquery.SchemaField('battery_power', 'FLOAT'),
        bigquery.SchemaField('grid_power', 'FLOAT'),
        bigquery.SchemaField('grid_services_power', 'FLOAT'),
        bigquery.SchemaField('generator_power', 'FLOAT'),
    ]
    power_table = bigquery.Table(power_table_ref, schema=power_schema)
    power_table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.MONTH,
        field='timestamp',
        expiration_ms=7776000000,  # 90 days
    )
    client.create_table(power_table, exists_ok=True)


def get_yesterday_start_end_date_in_utc() -> Tuple[str, str]:
    local_time_zone = pytz.timezone(TIMEZONE)
    current_date = datetime.datetime.now(datetime.timezone.utc).astimezone(local_time_zone)
    yesterday = current_date.date() - datetime.timedelta(days=1)
    start_local = local_time_zone.localize(
        datetime.datetime.combine(yesterday, datetime.time(0, 0, 0))
    )
    end_local = local_time_zone.localize(
        datetime.datetime.combine(yesterday, datetime.time(23, 59, 59))
    )
    start_utc = start_local.astimezone(pytz.UTC)
    end_utc = end_local.astimezone(pytz.UTC)
    start_date_str = start_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    end_date_str = end_utc.strftime('%Y-%m-%dT%H:%M:%S.999Z')
    return start_date_str, end_date_str


def get_energy_site_history(
    client_id: Optional[str],
    token_secret_name: Optional[str],
    cache_file: Optional[str],
    energy_site_id: Optional[str] = None,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Gets historical power and energy data from yesterday in 5-minute / daily increments."""
    token_store = create_token_store(
        secret_name=token_secret_name,
        cache_file=cache_file,
    )
    start_date, end_date = get_yesterday_start_end_date_in_utc()

    with TeslaFleetClient(
        client_id=client_id or '',
        token_store=token_store,
        api_base_url=api_base_url,
    ) as tesla:
        site_id = energy_site_id or tesla.get_energy_site_id()
        power_data = tesla.get_calendar_history(
            energy_site_id=site_id,
            kind='power',
            period='day',
            start_date=start_date,
            end_date=end_date,
            time_zone=TIMEZONE,
        )
        energy_data = tesla.get_calendar_history(
            energy_site_id=site_id,
            kind='energy',
            period='day',
            start_date=start_date,
            end_date=end_date,
            time_zone=TIMEZONE,
        )

    return power_data.get('time_series', []), energy_data.get('time_series', [])


def store_data(power_data: List[Dict[str, Any]], energy_data: List[Dict[str, Any]]) -> None:
    client = bigquery.Client()
    if POWER_TABLE_ID and power_data:
        errors = client.insert_rows_json(
            POWER_TABLE_ID,
            power_data,
            ignore_unknown_values=True,
        )
        if errors:
            raise RuntimeError(f'BigQuery power insert errors: {errors}')
    if ENERGY_TABLE_ID and energy_data:
        errors = client.insert_rows_json(
            ENERGY_TABLE_ID,
            energy_data,
            ignore_unknown_values=True,
        )
        if errors:
            raise RuntimeError(f'BigQuery energy insert errors: {errors}')


# Define main script
def main(
    tesla_client_id: Optional[str],
    tesla_token_secret_name: Optional[str],
    tesla_token_file: Optional[str],
    energy_site_id: Optional[str],
    tesla_api_base_url: str,
) -> None:
    """Fetch yesterday's Tesla energy history and store it in BigQuery."""
    print(f'Starting Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT}...')
    if INITIALIZE_TABLES:
        initialize_bigquery_tables()
    else:
        power_data, energy_data = get_energy_site_history(
            client_id=tesla_client_id,
            token_secret_name=tesla_token_secret_name,
            cache_file=tesla_token_file,
            energy_site_id=energy_site_id,
            api_base_url=tesla_api_base_url,
        )
        store_data(power_data, energy_data)
    print(f'Completed Task #{TASK_INDEX}.')


# Start script
if __name__ == '__main__':
    try:
        main(
            tesla_client_id=TESLA_CLIENT_ID,
            tesla_token_secret_name=TESLA_TOKEN_SECRET_NAME,
            tesla_token_file=TESLA_TOKEN_FILE,
            energy_site_id=ENERGY_SITE_ID,
            tesla_api_base_url=TESLA_API_BASE_URL,
        )
    except Exception as err:
        message = f'Task #{TASK_INDEX}, Attempt #{TASK_ATTEMPT} failed: {str(err)}'

        print(json.dumps({'message': message, 'severity': 'ERROR'}))
        # [START cloudrun_jobs_exit_process]
        sys.exit(1)  # Retry Job Task by exiting the process
        # [END cloudrun_jobs_exit_process]
