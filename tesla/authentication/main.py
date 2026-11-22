"""Interactive CLI to register a Tesla Fleet API partner domain and generate OAuth2 tokens."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

DEFAULT_AUTH_URL = 'https://auth.tesla.com/oauth2/v3/authorize'
DEFAULT_TOKEN_URL = 'https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token'
DEFAULT_API_BASE_URL = 'https://fleet-api.prd.na.vn.cloud.tesla.com'
DEFAULT_SCOPES = 'openid offline_access energy_device_data'


def build_httpx_client(timeout: float = 30.0) -> httpx.Client:
    try:
        return httpx.Client(http2=True, timeout=timeout)
    except ImportError:
        return httpx.Client(timeout=timeout)


def normalize_secret_name(secret_name: str, project_id: Optional[str] = None) -> str:
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
            'Secret name must be projects/<project-id>/secrets/<secret-id> '
            'or GOOGLE_CLOUD_PROJECT must be set.'
        )
    return f'projects/{project}/secrets/{cleaned}'


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str,
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


def extract_code_from_input(raw_input: str, expected_state: Optional[str] = None) -> str:
    """Extracts the authorization code from either a full redirect URL or a bare code."""
    cleaned = raw_input.strip()
    if not cleaned:
        raise ValueError('Empty authorization response.')

    if '://' in cleaned or '?' in cleaned:
        parsed = urlparse(cleaned)
        query = parse_qs(parsed.query)
        if 'error' in query:
            err = query['error'][0]
            desc = query.get('error_description', [''])[0]
            raise RuntimeError(f'Tesla authorization failed: {err} {desc}'.strip())
        if expected_state and 'state' in query and query['state'][0] != expected_state:
            raise ValueError('OAuth state mismatch in redirect URL. Please retry.')
        if 'code' not in query or not query['code'][0]:
            raise ValueError('Redirect URL does not contain a "code" query parameter.')
        return query['code'][0]

    return cleaned


def register_partner_domain(
    http: httpx.Client,
    client_id: str,
    client_secret: str,
    domain: str,
    api_base_url: str,
    scopes: str,
    token_url: str = DEFAULT_TOKEN_URL,
) -> Dict[str, Any]:
    """Registers the application domain (hosting the public key PEM) with Tesla Fleet API."""
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
        json={'domain': domain},
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
    scopes: str,
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


def save_tokens_to_file(token_data: Dict[str, Any], path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix='.tesla_token_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(token_data, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def save_tokens_to_secret_manager(
    token_data: Dict[str, Any],
    secret_name: str,
    client: Optional[Any] = None,
) -> str:
    if client is None:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()

    normalized = normalize_secret_name(secret_name)
    new_version = client.add_secret_version(
        request={
            'parent': normalized,
            'payload': {'data': json.dumps(token_data).encode('utf-8')},
        }
    )
    try:
        versions = client.list_secret_versions(
            request={
                'parent': normalized,
                'filter': 'state:ENABLED OR state:DISABLED',
            }
        )
        for v in versions:
            if v.name != new_version.name:
                try:
                    client.destroy_secret_version(request={'name': v.name})
                except Exception as err:
                    print(f'Warning: could not destroy older version {v.name}: {err}')
    except Exception as err:
        print(f'Warning: could not list older versions of {normalized}: {err}')
    return new_version.name


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
        print(f'Warning: could not fetch /api/1/products ({resp.status_code}): {resp.text}')
        return None
    products = resp.json().get('response')
    if not isinstance(products, list):
        return None

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
    return str(any_energy_sites[0]) if any_energy_sites else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Tesla Fleet API partner registration and OAuth2 token setup.'
    )
    parser.add_argument(
        '--client-id',
        default=os.getenv('TESLA_CLIENT_ID'),
        help='Tesla developer application Client ID (or set TESLA_CLIENT_ID).',
    )
    parser.add_argument(
        '--client-secret',
        default=os.getenv('TESLA_CLIENT_SECRET'),
        help='Tesla developer application Client Secret (or set TESLA_CLIENT_SECRET).',
    )
    parser.add_argument(
        '--redirect-uri',
        default=os.getenv('TESLA_REDIRECT_URI'),
        help='Registered OAuth redirect URI (or set TESLA_REDIRECT_URI).',
    )
    parser.add_argument(
        '--api-base-url',
        default=os.getenv('TESLA_API_BASE_URL', DEFAULT_API_BASE_URL),
        help='Regional Tesla Fleet API base URL (default: NA).',
    )
    parser.add_argument(
        '--scopes',
        default=os.getenv('TESLA_SCOPES', DEFAULT_SCOPES),
        help='Space-separated OAuth2 scopes.',
    )
    parser.add_argument(
        '--register-partner',
        metavar='DOMAIN',
        default=os.getenv('TESLA_PARTNER_DOMAIN'),
        help='One-time step: register DOMAIN hosting /.well-known/appspecific/com.tesla.3p.public-key.pem.',
    )
    parser.add_argument(
        '--secret-name',
        default=os.getenv('TESLA_TOKEN_SECRET_NAME'),
        help='Save tokens to GCP Secret Manager (projects/<project>/secrets/<id>).',
    )
    parser.add_argument(
        '--output',
        default=os.getenv('TESLA_TOKEN_FILE'),
        help='Save tokens to a local JSON file (defaults to ./cache.json if --secret-name is unset).',
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.client_id or not args.client_secret:
        raise ValueError(
            'Both --client-id (TESLA_CLIENT_ID) and --client-secret (TESLA_CLIENT_SECRET) are required.'
        )

    with build_httpx_client() as http:
        if args.register_partner:
            print(f'Registering partner domain "{args.register_partner}" with {args.api_base_url}...')
            reg_result = register_partner_domain(
                http=http,
                client_id=args.client_id,
                client_secret=args.client_secret,
                domain=args.register_partner,
                api_base_url=args.api_base_url,
                scopes=args.scopes,
            )
            print(f'Partner registration succeeded: {json.dumps(reg_result)}')

        if not args.redirect_uri:
            if args.register_partner:
                print('Skipping OAuth user token flow because --redirect-uri was not provided.')
                return
            raise ValueError('--redirect-uri (or TESLA_REDIRECT_URI) is required for OAuth login.')

        state = secrets.token_urlsafe(24)
        auth_url = build_authorization_url(
            client_id=args.client_id,
            redirect_uri=args.redirect_uri,
            scopes=args.scopes,
            state=state,
        )
        print('Open this URL in your browser and approve access to your Tesla Energy data:\n')
        print(f'  {auth_url}\n')
        callback_input = input('Paste the full redirect URL (or just the "code" parameter): ')
        code = extract_code_from_input(callback_input, expected_state=state)

        token_data = exchange_code_for_tokens(
            http=http,
            client_id=args.client_id,
            client_secret=args.client_secret,
            code=code,
            redirect_uri=args.redirect_uri,
            api_base_url=args.api_base_url,
            scopes=args.scopes,
        )

        saved_anywhere = False
        if args.secret_name:
            version_name = save_tokens_to_secret_manager(token_data, args.secret_name)
            print(f'Saved Tesla Fleet API tokens to Secret Manager: {version_name}')
            saved_anywhere = True

        output_path = args.output or (None if saved_anywhere else './cache.json')
        if output_path:
            save_tokens_to_file(token_data, output_path)
            print(f'Saved Tesla Fleet API tokens to local file: {output_path}')

        energy_site_id = discover_energy_site_id(
            http=http,
            access_token=token_data['access_token'],
            api_base_url=args.api_base_url,
        )
        if energy_site_id:
            print(f'Discovered ENERGY_SITE_ID: {energy_site_id}')


if __name__ == '__main__':
    try:
        main()
    except Exception as err:
        print(json.dumps({'message': str(err), 'severity': 'ERROR'}))
        sys.exit(1)
