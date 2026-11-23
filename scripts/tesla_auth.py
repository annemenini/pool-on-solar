#!/usr/bin/env python3
"""Optional interactive CLI for Tesla Fleet API partner registration and OAuth token seeding."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pool_on_solar.config import DEFAULT_API_BASE_URL
from pool_on_solar.oauth import (
    build_authorization_url,
    discover_energy_site_id,
    exchange_code_for_tokens,
    extract_code_from_input,
    register_partner_domain,
)
from pool_on_solar.storage import create_state_store
from pool_on_solar.tesla_fleet import DEFAULT_SCOPES, TOKEN_STATE_KEY, build_httpx_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Tesla Fleet API partner registration and OAuth2 token setup for Vercel storage.'
    )
    parser.add_argument('--client-id', default=os.getenv('TESLA_CLIENT_ID'))
    parser.add_argument('--client-secret', default=os.getenv('TESLA_CLIENT_SECRET'))
    parser.add_argument('--redirect-uri', default=os.getenv('TESLA_REDIRECT_URI'))
    parser.add_argument(
        '--api-base-url',
        default=os.getenv('TESLA_API_BASE_URL', DEFAULT_API_BASE_URL),
    )
    parser.add_argument('--scopes', default=os.getenv('TESLA_SCOPES', DEFAULT_SCOPES))
    parser.add_argument(
        '--register-partner',
        metavar='DOMAIN',
        default=os.getenv('TESLA_PARTNER_DOMAIN'),
        help='One-time step: register DOMAIN hosting /.well-known/appspecific/com.tesla.3p.public-key.pem.',
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
                return
            raise ValueError('--redirect-uri (or TESLA_REDIRECT_URI) is required for OAuth login.')

        state = secrets.token_urlsafe(24)
        auth_url = build_authorization_url(
            client_id=args.client_id,
            redirect_uri=args.redirect_uri,
            state=state,
            scopes=args.scopes,
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

        store = create_state_store()
        store.set_json(TOKEN_STATE_KEY, token_data)
        print(f'Saved Tesla Fleet API tokens to {store.backend_name} ({TOKEN_STATE_KEY}).')

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
