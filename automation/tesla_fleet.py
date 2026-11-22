"""Minimal Tesla Fleet API client with OAuth2 refresh token rotation and persistence."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List, Optional, Protocol

import httpx

DEFAULT_AUTH_BASE_URL = 'https://auth.tesla.com'
DEFAULT_TOKEN_URL = 'https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token'
DEFAULT_API_BASE_URL = 'https://fleet-api.prd.na.vn.cloud.tesla.com'
DEFAULT_SCOPES = 'openid offline_access energy_device_data'
DEFAULT_REFRESH_BUFFER_SECONDS = 300


class TeslaFleetError(RuntimeError):
    """Raised when a Tesla Fleet API or authentication call fails."""


@dataclass
class TokenData:
    """OAuth2 token payload persisted across stateless job runs."""

    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = 'Bearer'

    def is_expired(
        self,
        buffer_seconds: int = DEFAULT_REFRESH_BUFFER_SECONDS,
        now: Optional[float] = None,
    ) -> bool:
        current_time = time.time() if now is None else now
        return not self.access_token or current_time >= (self.expires_at - buffer_seconds)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'expires_at': self.expires_at,
            'token_type': self.token_type,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TokenData':
        return cls(
            access_token=str(data.get('access_token', '')),
            refresh_token=str(data.get('refresh_token', '')),
            expires_at=float(data.get('expires_at', 0.0)),
            token_type=str(data.get('token_type', 'Bearer')),
        )

    @classmethod
    def from_oauth_response(
        cls,
        payload: Dict[str, Any],
        fallback_refresh_token: str = '',
        now: Optional[float] = None,
    ) -> 'TokenData':
        current_time = time.time() if now is None else now
        access_token = payload.get('access_token')
        if not access_token:
            raise TeslaFleetError('Tesla token response did not include access_token.')
        refresh_token = payload.get('refresh_token') or fallback_refresh_token
        if not refresh_token:
            raise TeslaFleetError('Tesla token response did not include refresh_token.')
        expires_in = float(payload.get('expires_in', 28800))
        return cls(
            access_token=str(access_token),
            refresh_token=str(refresh_token),
            expires_at=current_time + expires_in,
            token_type=str(payload.get('token_type', 'Bearer')),
        )


class TokenStore(Protocol):
    """Persistence backend for rotating Tesla OAuth2 tokens."""

    def load(self) -> TokenData: ...

    def save(self, token: TokenData) -> None: ...


class LocalFileTokenStore:
    """Persists OAuth2 tokens as JSON on the local filesystem."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def load(self) -> TokenData:
        if not self._path.exists():
            raise FileNotFoundError(
                f'Tesla token file not found at {self._path}. '
                'Run tesla/authentication/main.py to generate initial tokens.'
            )
        with self._path.open('r', encoding='utf-8') as f:
            return TokenData.from_dict(json.load(f))

    def save(self, token: TokenData) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), prefix='.tesla_token_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(token.to_dict(), f, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


class SecretManagerTokenStore:
    """Persists OAuth2 tokens in Google Cloud Secret Manager.

    Secret Manager versions are immutable, so each token refresh writes a new
    version and immediately destroys all older non-destroyed versions so only
    the latest version is kept.
    """

    def __init__(
        self,
        secret_name: str,
        destroy_old_versions: bool = True,
        client: Optional[Any] = None,
    ) -> None:
        self._secret_name = normalize_secret_name(secret_name)
        self._destroy_old_versions = destroy_old_versions
        self._client = client
        self._last_loaded_version_name: Optional[str] = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def load(self) -> TokenData:
        client = self._get_client()
        version_path = f'{self._secret_name}/versions/latest'
        response = client.access_secret_version(request={'name': version_path})
        self._last_loaded_version_name = response.name
        payload = json.loads(response.payload.data.decode('utf-8'))
        return TokenData.from_dict(payload)

    def _destroy_prior_versions(self, client: Any, keep_version_name: str) -> None:
        try:
            versions = client.list_secret_versions(
                request={
                    'parent': self._secret_name,
                    'filter': 'state:ENABLED OR state:DISABLED',
                }
            )
            names_to_destroy = [
                v.name for v in versions if v.name != keep_version_name
            ]
        except Exception as err:
            print(
                f'Warning: could not list older versions of {self._secret_name}: {err}'
            )
            names_to_destroy = (
                [self._last_loaded_version_name]
                if self._last_loaded_version_name
                and self._last_loaded_version_name != keep_version_name
                else []
            )

        for version_name in names_to_destroy:
            try:
                client.destroy_secret_version(request={'name': version_name})
            except Exception as err:
                print(
                    f'Warning: saved new Tesla token version {keep_version_name}, '
                    f'but could not destroy older version {version_name}: {err}'
                )

    def save(self, token: TokenData) -> None:
        client = self._get_client()
        data = json.dumps(token.to_dict()).encode('utf-8')
        new_version = client.add_secret_version(
            request={
                'parent': self._secret_name,
                'payload': {'data': data},
            }
        )
        if self._destroy_old_versions:
            self._destroy_prior_versions(client, keep_version_name=new_version.name)
        self._last_loaded_version_name = new_version.name


def normalize_secret_name(secret_name: str, project_id: Optional[str] = None) -> str:
    """Normalizes a Secret Manager secret name to projects/<project>/secrets/<id>."""
    cleaned = secret_name.strip().rstrip('/')
    if '/versions/' in cleaned:
        cleaned = cleaned.split('/versions/', 1)[0]
    if cleaned.startswith('projects/') and '/secrets/' in cleaned:
        return cleaned
    project = (
        project_id
        or os.getenv('GOOGLE_CLOUD_PROJECT')
        or os.getenv('GCP_PROJECT')
    )
    if not project:
        raise ValueError(
            'TESLA_TOKEN_SECRET_NAME must be a full resource name '
            '(projects/<project-id>/secrets/<secret-id>) or GOOGLE_CLOUD_PROJECT must be set.'
        )
    return f'projects/{project}/secrets/{cleaned}'


def create_token_store(
    secret_name: Optional[str] = None,
    cache_file: Optional[str] = None,
) -> TokenStore:
    """Selects SecretManagerTokenStore when secret_name is configured, else LocalFileTokenStore."""
    if secret_name:
        return SecretManagerTokenStore(secret_name)
    if cache_file:
        return LocalFileTokenStore(cache_file)
    raise ValueError('Either a Secret Manager secret name or a local token cache file must be configured.')


def _build_httpx_client(timeout: float = 30.0) -> httpx.Client:
    try:
        return httpx.Client(http2=True, timeout=timeout)
    except ImportError:
        return httpx.Client(timeout=timeout)


class TeslaFleetClient:
    """Client for Tesla Fleet API energy endpoints with automatic token refresh."""

    def __init__(
        self,
        client_id: str,
        token_store: TokenStore,
        api_base_url: str = DEFAULT_API_BASE_URL,
        token_url: str = DEFAULT_TOKEN_URL,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        if not client_id:
            raise ValueError('TESLA_CLIENT_ID is required for Tesla Fleet API authentication.')
        self._client_id = client_id
        self._token_store = token_store
        self._api_base_url = api_base_url.rstrip('/')
        self._token_url = token_url
        self._owns_http_client = http_client is None
        self._http = http_client or _build_httpx_client()
        self._token: Optional[TokenData] = None

    def __enter__(self) -> 'TeslaFleetClient':
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def _get_token(self, force_refresh: bool = False) -> TokenData:
        if self._token is None:
            self._token = self._token_store.load()
        if force_refresh or self._token.is_expired():
            self._token = self._refresh_token(self._token)
        return self._token

    def _refresh_token(self, current: TokenData) -> TokenData:
        if not current.refresh_token:
            raise TeslaFleetError(
                'No Tesla refresh_token available. Re-run tesla/authentication/main.py.'
            )
        response = self._http.post(
            self._token_url,
            data={
                'grant_type': 'refresh_token',
                'client_id': self._client_id,
                'refresh_token': current.refresh_token,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        if response.status_code >= 400:
            raise TeslaFleetError(
                f'Tesla token refresh failed ({response.status_code}): {response.text}'
            )
        new_token = TokenData.from_oauth_response(
            response.json(),
            fallback_refresh_token=current.refresh_token,
        )
        self._token_store.save(new_token)
        print('Refreshed and persisted Tesla Fleet API OAuth tokens.')
        return new_token

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f'{self._api_base_url}{path}'
        token = self._get_token(force_refresh=False)
        response = self._http.request(
            method,
            url,
            params=params,
            json=json_body,
            headers={'Authorization': f'Bearer {token.access_token}'},
        )
        if response.status_code == 401:
            token = self._get_token(force_refresh=True)
            response = self._http.request(
                method,
                url,
                params=params,
                json=json_body,
                headers={'Authorization': f'Bearer {token.access_token}'},
            )
        if response.status_code >= 400:
            raise TeslaFleetError(
                f'Tesla Fleet API {method} {path} failed ({response.status_code}): {response.text}'
            )
        return response.json()

    def get_products(self) -> List[Dict[str, Any]]:
        payload = self._request('GET', '/api/1/products')
        products = payload.get('response')
        if not isinstance(products, list):
            raise TeslaFleetError(f'Unexpected /api/1/products response: {payload}')
        return products

    def get_energy_site_id(self) -> str:
        products = self.get_products()
        battery_sites = [
            p.get('energy_site_id')
            for p in products
            if isinstance(p, dict)
            and p.get('resource_type') == 'battery'
            and p.get('energy_site_id') is not None
        ]
        if battery_sites:
            return str(battery_sites[0])

        any_energy_sites = [
            p.get('energy_site_id')
            for p in products
            if isinstance(p, dict) and p.get('energy_site_id') is not None
        ]
        if any_energy_sites:
            return str(any_energy_sites[0])

        raise TeslaFleetError('Could not find an energy_site_id in Tesla products list.')

    def get_live_status(self, energy_site_id: str) -> Dict[str, Any]:
        payload = self._request('GET', f'/api/1/energy_sites/{energy_site_id}/live_status')
        data = payload.get('response')
        if not isinstance(data, dict):
            raise TeslaFleetError(f'Unexpected live_status response: {payload}')
        return data

    def get_calendar_history(
        self,
        energy_site_id: str,
        kind: str,
        period: str,
        start_date: str,
        end_date: str,
        time_zone: str,
    ) -> Dict[str, Any]:
        payload = self._request(
            'GET',
            f'/api/1/energy_sites/{energy_site_id}/calendar_history',
            params={
                'kind': kind,
                'period': period,
                'start_date': start_date,
                'end_date': end_date,
                'time_zone': time_zone,
            },
        )
        data = payload.get('response')
        if not isinstance(data, dict):
            raise TeslaFleetError(f'Unexpected calendar_history response: {payload}')
        return data
