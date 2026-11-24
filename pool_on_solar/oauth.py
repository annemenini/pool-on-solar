"""Tesla Fleet API partner domain registration and OAuth2 authorization helpers."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx

from pool_on_solar.tesla_fleet import (
    DEFAULT_AUTH_BASE_URL,
    DEFAULT_SCOPES,
    DEFAULT_TOKEN_URL,
    select_energy_site_id,
)

DEFAULT_AUTH_URL = f'{DEFAULT_AUTH_BASE_URL}/oauth2/v3/authorize'


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: str = DEFAULT_SCOPES,
    auth_url: str = DEFAULT_AUTH_URL,
) -> str:
    params = {
        'client_id': client_id,
        'locale': 'en-US',
        'prompt': 'login',
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': scopes,
        'state': state,
    }
    return f'{auth_url}?{urlencode(params)}'


def register_partner_domain(
    http: httpx.Client,
    client_id: str,
    client_secret: str,
    domain: str,
    api_base_url: str,
    scopes: str = DEFAULT_SCOPES,
    token_url: str = DEFAULT_TOKEN_URL,
) -> Dict[str, Any]:
    api_base = api_base_url.rstrip('/')
    token_resp = http.post(
        token_url,
        data={
            'grant_type': 'client_credentials',
            'client_id': client_id,
            'client_secret': client_secret,
            'scope': scopes,
            'audience': api_base,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    if token_resp.status_code >= 400:
        raise RuntimeError(
            f'Failed to get partner token ({token_resp.status_code}): {token_resp.text}'
        )
    partner_token = token_resp.json()['access_token']

    reg_resp = http.post(
        f'{api_base}/api/1/partner_accounts',
        json={'domain': domain.strip().lower()},
        headers={'Authorization': f'Bearer {partner_token}'},
    )
    if reg_resp.status_code >= 400:
        raise RuntimeError(
            f'Partner registration failed ({reg_resp.status_code}): {reg_resp.text}'
        )
    return reg_resp.json()


def exchange_code_for_tokens(
    http: httpx.Client,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    api_base_url: str,
    scopes: str = DEFAULT_SCOPES,
    token_url: str = DEFAULT_TOKEN_URL,
) -> Dict[str, Any]:
    api_base = api_base_url.rstrip('/')
    resp = http.post(
        token_url,
        data={
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'client_secret': client_secret,
            'code': code,
            'audience': api_base,
            'redirect_uri': redirect_uri,
            'scope': scopes,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    if resp.status_code >= 400:
        raise RuntimeError(f'Token exchange failed ({resp.status_code}): {resp.text}')

    payload = resp.json()
    if 'access_token' not in payload or 'refresh_token' not in payload:
        raise RuntimeError(
            f'Token response missing access_token or refresh_token (check offline_access scope): {payload}'
        )
    return {
        'access_token': payload['access_token'],
        'refresh_token': payload['refresh_token'],
        'expires_at': time.time() + float(payload.get('expires_in', 28800)),
        'token_type': payload.get('token_type', 'Bearer'),
    }


def discover_energy_site_id(
    http: httpx.Client,
    access_token: str,
    api_base_url: str,
) -> Optional[str]:
    resp = http.get(
        f'{api_base_url.rstrip("/")}/api/1/products',
        headers={'Authorization': f'Bearer {access_token}'},
    )
    if resp.status_code >= 400:
        return None
    products = resp.json().get('response')
    return select_energy_site_id(products) if isinstance(products, list) else None
