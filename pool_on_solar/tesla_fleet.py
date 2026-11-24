"""Tesla Fleet API client with OAuth2 refresh token rotation backed by StateStore."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Dict, List, Optional

import httpx

from pool_on_solar.cache import AppCache, TESLA_SITE_TAG
from pool_on_solar.config import DEFAULT_API_BASE_URL
from pool_on_solar.storage import StateStore

DEFAULT_AUTH_BASE_URL = 'https://auth.tesla.com'
DEFAULT_TOKEN_URL = 'https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token'
DEFAULT_SCOPES = 'openid offline_access energy_device_data'
DEFAULT_REFRESH_BUFFER_SECONDS = 300
TOKEN_STATE_KEY = 'tesla/token.json'
ENERGY_SITE_ID_CACHE_TTL_SECONDS = 24 * 60 * 60


class TeslaFleetError(RuntimeError):
    """Raised when a Tesla Fleet API or authentication call fails."""


@dataclass
class TokenData:
    """OAuth2 token payload persisted across stateless Vercel Function invocations."""

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


def build_httpx_client(timeout: float = 30.0) -> httpx.Client:
    try:
        return httpx.Client(http2=True, timeout=timeout)
    except ImportError:
        return httpx.Client(timeout=timeout)


def select_energy_site_id(products: List[Dict[str, Any]]) -> Optional[str]:
    """Picks the primary battery energy site ID (or first energy site ID) from Tesla products."""
    for prefer_battery in (True, False):
        for item in products:
            if not isinstance(item, dict) or item.get('energy_site_id') is None:
                continue
            if not prefer_battery or item.get('resource_type') == 'battery':
                return str(item['energy_site_id'])
    return None


class TeslaFleetClient:
    """Client for Tesla Fleet API energy endpoints with automatic token refresh."""

    def __init__(
        self,
        client_id: str,
        state_store: StateStore,
        api_base_url: str = DEFAULT_API_BASE_URL,
        token_url: str = DEFAULT_TOKEN_URL,
        token_key: str = TOKEN_STATE_KEY,
        http_client: Optional[httpx.Client] = None,
        cache: Optional[AppCache] = None,
    ) -> None:
        if not client_id:
            raise ValueError('TESLA_CLIENT_ID is required for Tesla Fleet API authentication.')
        self._client_id = client_id
        self._state_store = state_store
        self._api_base_url = api_base_url.rstrip('/')
        self._token_url = token_url
        self._token_key = token_key
        self._owns_http_client = http_client is None
        self._http = http_client or build_httpx_client()
        self._cache = cache
        self._token: Optional[TokenData] = None

    def __enter__(self) -> 'TeslaFleetClient':
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    @property
    def current_token(self) -> Optional[TokenData]:
        return self._token

    def _load_token(self) -> TokenData:
        raw = self._state_store.get_json(self._token_key)
        if not raw:
            raise TeslaFleetError(
                'Tesla OAuth tokens not found in storage. Visit /oauth to authorize.'
            )
        return TokenData.from_dict(raw)

    def _get_token(self, force_refresh: bool = False) -> TokenData:
        if self._token is None:
            self._token = self._load_token()
        if force_refresh or self._token.is_expired():
            self._token = self._refresh_token(self._token)
        return self._token

    def _refresh_token(self, current: TokenData) -> TokenData:
        if not current.refresh_token:
            raise TeslaFleetError(
                'No Tesla refresh_token available. Re-authorize via /oauth/login.'
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
        self._state_store.set_json(self._token_key, new_token.to_dict())
        print(f'Refreshed and persisted Tesla Fleet API OAuth tokens ({self._state_store.backend_name}).')
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
        cache_key = f'tesla:energy-site-id:{self._client_id}'
        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached:
                return str(cached)

        site_id = select_energy_site_id(self.get_products())
        if not site_id:
            raise TeslaFleetError('Could not find an energy_site_id in Tesla products list.')

        if self._cache is not None:
            self._cache.set(
                cache_key,
                site_id,
                ttl=ENERGY_SITE_ID_CACHE_TTL_SECONDS,
                tags=[TESLA_SITE_TAG],
                name='tesla-energy-site-id',
            )
        return site_id

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
