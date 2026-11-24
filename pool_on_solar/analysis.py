"""Daily Tesla Energy history job pushing yesterday's power & energy data to BigQuery."""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

from pool_on_solar.cache import AppCache
from pool_on_solar.config import AppConfig
from pool_on_solar.storage import StateStore
from pool_on_solar.tesla_fleet import TeslaFleetClient

BIGQUERY_SCOPE = 'https://www.googleapis.com/auth/bigquery'
ANALYSIS_EXEC_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


def get_yesterday_window_utc(timezone_name: str) -> Tuple[str, str, str]:
    """Returns (yesterday_iso_date, start_utc_iso, end_utc_iso) for the given local timezone."""
    local_tz = ZoneInfo(timezone_name)
    now_local = datetime.datetime.now(datetime.timezone.utc).astimezone(local_tz)
    yesterday = now_local.date() - datetime.timedelta(days=1)
    start_utc = datetime.datetime.combine(
        yesterday, datetime.time(0, 0, 0), tzinfo=local_tz
    ).astimezone(datetime.timezone.utc)
    end_utc = datetime.datetime.combine(
        yesterday, datetime.time(23, 59, 59), tzinfo=local_tz
    ).astimezone(datetime.timezone.utc)
    return (
        yesterday.isoformat(),
        start_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        end_utc.strftime('%Y-%m-%dT%H:%M:%S.999Z'),
    )


def exchange_vercel_oidc_for_gcp_access_token(
    oidc_token: str,
    project_number: str,
    pool_id: str,
    provider_id: str,
    service_account_email: str,
    http_client: Optional[httpx.Client] = None,
) -> str:
    """Exchanges a Vercel OIDC JWT for a short-lived GCP BigQuery access token via WIF."""
    audience = (
        f'//iam.googleapis.com/projects/{project_number}/locations/global/'
        f'workloadIdentityPools/{pool_id}/providers/{provider_id}'
    )
    sts_body = {
        'audience': audience,
        'grantType': 'urn:ietf:params:oauth:grant-type:token-exchange',
        'requestedTokenType': 'urn:ietf:params:oauth:token-type:access_token',
        'scope': 'https://www.googleapis.com/auth/cloud-platform',
        'subjectTokenType': 'urn:ietf:params:oauth:token-type:jwt',
        'subjectToken': oidc_token,
    }
    impersonate_url = (
        f'https://iamcredentials.googleapis.com/v1/projects/-/'
        f'serviceAccounts/{service_account_email}:generateAccessToken'
    )

    def _run(client: httpx.Client) -> str:
        sts_resp = client.post('https://sts.googleapis.com/v1/token', json=sts_body)
        if sts_resp.status_code >= 400:
            raise RuntimeError(
                f'GCP STS token exchange failed ({sts_resp.status_code}): {sts_resp.text}'
            )
        federated_token = sts_resp.json()['access_token']

        imp_resp = client.post(
            impersonate_url,
            json={'scope': [BIGQUERY_SCOPE]},
            headers={'Authorization': f'Bearer {federated_token}'},
        )
        if imp_resp.status_code >= 400:
            raise RuntimeError(
                f'GCP service account impersonation failed ({imp_resp.status_code}): {imp_resp.text}'
            )
        return imp_resp.json()['accessToken']

    if http_client is not None:
        return _run(http_client)
    with httpx.Client(timeout=15.0) as client:
        return _run(client)


def _resolve_vercel_oidc_token(explicit_token: Optional[str]) -> Optional[str]:
    if explicit_token:
        return explicit_token
    try:
        from vercel.oidc import get_vercel_oidc_token

        token = get_vercel_oidc_token()
        if token:
            return token
    except Exception:
        pass
    return os.getenv('VERCEL_OIDC_TOKEN')


def create_bigquery_client(oidc_token: Optional[str] = None) -> Any:
    """Creates a BigQuery client using Vercel OIDC WIF, service account JSON, or ADC."""
    from google.cloud import bigquery
    from google.oauth2 import credentials as oauth2_credentials
    from google.oauth2 import service_account

    project_id = os.getenv('GCP_PROJECT_ID') or os.getenv('GOOGLE_CLOUD_PROJECT')

    # 1. Keyless Vercel OIDC -> Google Cloud Workload Identity Federation
    project_number = os.getenv('GCP_PROJECT_NUMBER')
    pool_id = os.getenv('GCP_WORKLOAD_IDENTITY_POOL_ID')
    provider_id = os.getenv('GCP_WORKLOAD_IDENTITY_POOL_PROVIDER_ID')
    sa_email = os.getenv('GCP_SERVICE_ACCOUNT_EMAIL')
    token = _resolve_vercel_oidc_token(oidc_token)
    if project_number and pool_id and provider_id and sa_email and token:
        access_token = exchange_vercel_oidc_for_gcp_access_token(
            oidc_token=token,
            project_number=project_number,
            pool_id=pool_id,
            provider_id=provider_id,
            service_account_email=sa_email,
        )
        creds = oauth2_credentials.Credentials(token=access_token)
        return bigquery.Client(project=project_id, credentials=creds)

    # 2. Static service account JSON stored in a Vercel Secret env var
    sa_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
    if sa_json:
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[BIGQUERY_SCOPE]
        )
        return bigquery.Client(project=project_id or info.get('project_id'), credentials=creds)

    # 3. Local Application Default Credentials fallback
    return bigquery.Client(project=project_id)


def store_in_bigquery(
    power_data: List[Dict[str, Any]],
    energy_data: List[Dict[str, Any]],
    power_table_id: Optional[str] = None,
    energy_table_id: Optional[str] = None,
    bq_client: Optional[Any] = None,
    oidc_token: Optional[str] = None,
) -> None:
    resolved_power_table = power_table_id or os.getenv('POWER_TABLE_ID')
    resolved_energy_table = energy_table_id or os.getenv('ENERGY_TABLE_ID')
    if not resolved_power_table and not resolved_energy_table:
        raise ValueError('POWER_TABLE_ID and/or ENERGY_TABLE_ID must be configured for BigQuery.')

    client = bq_client or create_bigquery_client(oidc_token=oidc_token)
    if resolved_power_table and power_data:
        errors = client.insert_rows_json(
            resolved_power_table,
            power_data,
            ignore_unknown_values=True,
        )
        if errors:
            raise RuntimeError(f'BigQuery power insert errors: {errors}')
    if resolved_energy_table and energy_data:
        errors = client.insert_rows_json(
            resolved_energy_table,
            energy_data,
            ignore_unknown_values=True,
        )
        if errors:
            raise RuntimeError(f'BigQuery energy insert errors: {errors}')


def run_daily_analysis(
    config: AppConfig,
    state_store: StateStore,
    execution_id: Optional[str] = None,
    oidc_token: Optional[str] = None,
    bq_client: Optional[Any] = None,
    cache: Optional[AppCache] = None,
) -> Dict[str, Any]:
    """Fetches yesterday's 5-min power & daily energy history and inserts rows into BigQuery."""
    exec_cache_key = f'schedule:exec:analysis:{execution_id}' if execution_id else None
    if cache is not None and exec_cache_key and cache.get(exec_cache_key):
        return {
            'status': 'skipped',
            'reason': 'duplicate_execution',
            'execution_id': execution_id,
        }

    day_str, start_date, end_date = get_yesterday_window_utc(config.location.timezone)

    with TeslaFleetClient(
        client_id=config.tesla.client_id,
        state_store=state_store,
        api_base_url=config.tesla.api_base_url,
        cache=cache,
    ) as tesla:
        site_id = config.tesla.energy_site_id or tesla.get_energy_site_id()
        power_data = tesla.get_calendar_history(
            energy_site_id=site_id,
            kind='power',
            period='day',
            start_date=start_date,
            end_date=end_date,
            time_zone=config.location.timezone,
        )
        energy_data = tesla.get_calendar_history(
            energy_site_id=site_id,
            kind='energy',
            period='day',
            start_date=start_date,
            end_date=end_date,
            time_zone=config.location.timezone,
        )

    power_series = power_data.get('time_series', [])
    energy_series = energy_data.get('time_series', [])
    store_in_bigquery(
        power_data=power_series,
        energy_data=energy_series,
        bq_client=bq_client,
        oidc_token=oidc_token,
    )

    if cache is not None and exec_cache_key:
        cache.set(
            exec_cache_key,
            {
                'date': day_str,
                'power_points': len(power_series),
                'energy_points': len(energy_series),
                'completed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
            ttl=ANALYSIS_EXEC_IDEMPOTENCY_TTL_SECONDS,
            tags=['schedule-exec'],
            name='analysis-execution-idempotency',
        )

    return {
        'status': 'succeeded',
        'execution_id': execution_id,
        'date': day_str,
        'power_points': len(power_series),
        'energy_points': len(energy_series),
    }
