"""Vercel Function serving the Pool on Solar dashboard, API, and Tesla Fleet OAuth routes."""

from __future__ import annotations

from html import escape
from http import cookies
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import secrets
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from pool_on_solar.cache import AppCache, create_app_cache
from pool_on_solar.config import AppConfig, load_config, parse_bool_env
from pool_on_solar.dashboard import (
    DASHBOARD_STATUS_TAG,
    StatusCollector,
    render_dashboard_html,
)
from pool_on_solar.oauth import (
    build_authorization_url,
    discover_energy_site_id,
    exchange_code_for_tokens,
    register_partner_domain,
)
from pool_on_solar.storage import StateStore, create_state_store
from pool_on_solar.tesla_fleet import (
    DEFAULT_SCOPES,
    TOKEN_STATE_KEY,
    build_httpx_client,
)

STATE_COOKIE_NAME = 'tesla_oauth_state'
SESSION_COOKIE_NAME = 'pool_dashboard_session'
WELL_KNOWN_KEY_PATH = '/.well-known/appspecific/com.tesla.3p.public-key.pem'

_SHARED_COLLECTOR: Optional[StatusCollector] = None
_SHARED_STORE: Optional[StateStore] = None
_SHARED_CACHE: Optional[AppCache] = None
_SHARED_CONFIG: Optional[AppConfig] = None


def load_public_key_pem() -> Optional[str]:
    env_pem = os.getenv('TESLA_PUBLIC_KEY_PEM')
    if env_pem:
        return env_pem.replace('\\n', '\n').strip() + '\n'

    key_file = os.getenv('TESLA_PUBLIC_KEY_FILE')
    if key_file and Path(key_file).is_file():
        return Path(key_file).read_text(encoding='utf-8').strip() + '\n'
    return None


def _get_runtime() -> tuple[AppConfig, StateStore, StatusCollector, AppCache]:
    global _SHARED_CONFIG, _SHARED_STORE, _SHARED_COLLECTOR, _SHARED_CACHE
    if _SHARED_CONFIG is None:
        _SHARED_CONFIG = load_config()
    if _SHARED_STORE is None:
        _SHARED_STORE = create_state_store()
    if _SHARED_CACHE is None:
        _SHARED_CACHE = create_app_cache()
    if _SHARED_COLLECTOR is None:
        _SHARED_COLLECTOR = StatusCollector(_SHARED_CONFIG, _SHARED_STORE, cache=_SHARED_CACHE)
    return _SHARED_CONFIG, _SHARED_STORE, _SHARED_COLLECTOR, _SHARED_CACHE


def _render_simple_html(title: str, body_html: str) -> bytes:
    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 680px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; background: #0c0d10; color: #f3f4f6; }}
    code, pre {{ background: #15171d; border: 1px solid #252832; padding: 0.15rem 0.35rem; border-radius: 6px; }}
    pre {{ padding: 0.75rem; overflow-x: auto; }}
    a {{ color: #60a5fa; }}
    .btn {{ display: inline-block; padding: 0.55rem 1rem; background: #f3f4f6; color: #0c0d10; text-decoration: none; border-radius: 8px; border: 0; cursor: pointer; font-size: 0.95rem; font-weight: 600; margin-right: 0.5rem; }}
    .btn.secondary {{ background: #252832; color: #f3f4f6; }}
    .box {{ background: #15171d; border: 1px solid #252832; border-radius: 12px; padding: 1rem 1.25rem; margin: 1rem 0; }}
    .ok {{ color: #4ade80; font-weight: 600; }}
    .warn {{ color: #fbbf24; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>{escape(title)}</h1>
  {body_html}
</body>
</html>
"""
    return doc.encode('utf-8')


def make_web_handler(
    config: Optional[AppConfig] = None,
    state_store: Optional[StateStore] = None,
    collector: Optional[StatusCollector] = None,
    cache: Optional[AppCache] = None,
    http_client_factory: Callable[[], Any] = build_httpx_client,
    public_key_loader: Callable[[], Optional[str]] = load_public_key_pem,
):
    class WebHandler(BaseHTTPRequestHandler):
        def _deps(self) -> tuple[AppConfig, StateStore, StatusCollector, AppCache]:
            if (
                config is not None
                and state_store is not None
                and collector is not None
                and cache is not None
            ):
                return config, state_store, collector, cache
            default_cfg, default_store, default_collector, default_cache = _get_runtime()
            return (
                config or default_cfg,
                state_store or default_store,
                collector or default_collector,
                cache or default_cache,
            )

        def _request_host(self) -> str:
            forwarded = self.headers.get('X-Forwarded-Host')
            if forwarded:
                return forwarded.split(',')[0].strip()
            return (self.headers.get('Host') or 'localhost:3000').strip()

        def _request_scheme(self) -> str:
            forwarded = self.headers.get('X-Forwarded-Proto')
            if forwarded:
                return forwarded.split(',')[0].strip()
            host = self._request_host()
            if host.startswith('localhost') or host.startswith('127.0.0.1'):
                return 'http'
            return 'https'

        def _is_same_origin(self) -> bool:
            host = self._request_host().lower()
            origin = self.headers.get('Origin')
            if origin:
                origin_host = (urlparse(origin).netloc or '').lower()
                return bool(origin_host and origin_host == host)
            referer = self.headers.get('Referer')
            if referer:
                ref_host = (urlparse(referer).netloc or '').lower()
                return bool(ref_host and ref_host == host)
            return True

        def _get_cookie(self, name: str) -> Optional[str]:
            raw = self.headers.get('Cookie')
            if not raw:
                return None
            jar = cookies.SimpleCookie()
            jar.load(raw)
            morsel = jar.get(name)
            return morsel.value if morsel else None

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            extra_headers: Optional[Dict[str, str]] = None,
        ) -> None:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _check_access_gate(self, path: str, query: Dict[str, Any]) -> bool:
            """Optional token gate (DASHBOARD_ACCESS_TOKEN) when not using Vercel Deployment Protection."""
            expected = os.getenv('DASHBOARD_ACCESS_TOKEN', '')
            if not expected:
                return True

            cookie_val = self._get_cookie(SESSION_COOKIE_NAME) or ''
            if cookie_val and secrets.compare_digest(cookie_val, expected):
                return True

            query_val = (query.get('token') or [''])[0]
            if query_val and secrets.compare_digest(query_val, expected):
                flags = 'HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000'
                if self._request_scheme() == 'https':
                    flags += '; Secure'
                self._send_bytes(
                    303,
                    b'',
                    'text/plain; charset=utf-8',
                    extra_headers={
                        'Location': path or '/',
                        'Set-Cookie': f'{SESSION_COOKIE_NAME}={expected}; {flags}',
                    },
                )
                return False

            self._send_bytes(
                401,
                _render_simple_html(
                    'Access Restricted',
                    '<p>Sign in with your access token: append <code>?token=...</code> to the URL.</p>',
                ),
                'text/html; charset=utf-8',
            )
            return False

        def _effective_redirect_uri(self) -> str:
            override = os.getenv('TESLA_REDIRECT_URI')
            if override:
                return override
            return f'{self._request_scheme()}://{self._request_host()}/oauth/callback'

        def _effective_partner_domain(self) -> str:
            override = os.getenv('TESLA_PARTNER_DOMAIN')
            if override:
                return override.strip().lower()
            parsed = urlparse(self._effective_redirect_uri())
            return (parsed.hostname or '').lower()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == WELL_KNOWN_KEY_PATH:
                pem = public_key_loader()
                if not pem:
                    self._send_bytes(
                        404,
                        b'Public key not configured (set TESLA_PUBLIC_KEY_PEM).\n',
                        'text/plain; charset=utf-8',
                    )
                    return
                self._send_bytes(200, pem.encode('utf-8'), 'application/x-pem-file')
                return

            if path == '/healthz':
                self._send_bytes(200, b'ok\n', 'text/plain; charset=utf-8')
                return

            if not self._check_access_gate(path, query):
                return

            cfg, store, active_collector, active_cache = self._deps()
            oidc_token = self.headers.get('x-vercel-oidc-token')

            if path in {'/', '/api/web'}:
                force = (query.get('refresh') or [''])[0].lower() in {'1', 'true', 'yes'}
                snapshot = active_collector.get_snapshot(force_refresh=force, oidc_token=oidc_token)
                self._send_bytes(200, render_dashboard_html(snapshot), 'text/html; charset=utf-8')
                return

            if path == '/api/status':
                force = (query.get('refresh') or [''])[0].lower() in {'1', 'true', 'yes'}
                snapshot = active_collector.get_snapshot(force_refresh=force, oidc_token=oidc_token)
                self._send_bytes(
                    200,
                    json.dumps(snapshot, indent=2).encode('utf-8'),
                    'application/json; charset=utf-8',
                )
                return

            if path in {'/oauth', '/oauth/', '/oauth/login', '/oauth/callback'}:
                if not parse_bool_env('OAUTH_ENABLED', default=False):
                    self._send_bytes(
                        403,
                        _render_simple_html(
                            'OAuth Flow Disabled',
                            '<p>Set <code>OAUTH_ENABLED=true</code> in Vercel environment variables '
                            'to run interactive Tesla OAuth setup. The public key endpoint remains active.</p>',
                        ),
                        'text/html; charset=utf-8',
                    )
                    return
                if path in {'/oauth', '/oauth/'}:
                    self._handle_oauth_index(cfg, store)
                    return
                if path == '/oauth/login':
                    self._handle_oauth_login(cfg)
                    return
                if path == '/oauth/callback':
                    self._handle_oauth_callback(cfg, store, active_cache, query)
                    return

            self._send_bytes(404, b'Not Found\n', 'text/plain; charset=utf-8')

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if not self._check_access_gate(path, query):
                return

            if not self._is_same_origin():
                self._send_bytes(403, b'Cross-origin POST rejected.\n', 'text/plain; charset=utf-8')
                return

            cfg, _, active_collector, _ = self._deps()
            oidc_token = self.headers.get('x-vercel-oidc-token')

            if path in {'/scheduler/pause', '/scheduler/resume'}:
                action = 'pause' if path == '/scheduler/pause' else 'resume'
                try:
                    active_collector.toggle_scheduler(action, oidc_token=oidc_token)
                except Exception as err:
                    self._send_bytes(
                        502,
                        _render_simple_html(
                            f'Failed to {action} schedule',
                            f'<pre>{escape(str(err))}</pre><p><a href="/">Back</a></p>',
                        ),
                        'text/html; charset=utf-8',
                    )
                    return
                self._send_bytes(
                    303,
                    b'',
                    'text/plain; charset=utf-8',
                    extra_headers={'Location': '/'},
                )
                return

            if path == '/oauth/register':
                if not parse_bool_env('OAUTH_ENABLED', default=False):
                    self._send_bytes(
                        403,
                        _render_simple_html(
                            'OAuth Flow Disabled',
                            '<p>Set <code>OAUTH_ENABLED=true</code> first.</p>',
                        ),
                        'text/html; charset=utf-8',
                    )
                    return
                self._handle_oauth_register(cfg)
                return

            self._send_bytes(404, b'Not Found\n', 'text/plain; charset=utf-8')

        def _handle_oauth_index(self, cfg: AppConfig, store: StateStore) -> None:
            redirect_uri = self._effective_redirect_uri()
            domain = self._effective_partner_domain()
            key_ok = bool(public_key_loader())
            key_status = (
                '<span class="ok">Configured</span>'
                if key_ok
                else '<span class="warn">Missing (set TESLA_PUBLIC_KEY_PEM)</span>'
            )
            creds_status = (
                '<span class="ok">Configured</span>'
                if cfg.tesla.client_id and cfg.tesla.client_secret
                else '<span class="warn">Missing TESLA_CLIENT_ID / TESLA_CLIENT_SECRET</span>'
            )
            body = (
                '<div class="box">'
                f'<p><strong>Partner Domain:</strong> <code>{escape(domain)}</code></p>'
                f'<p><strong>Redirect URI:</strong> <code>{escape(redirect_uri)}</code></p>'
                f'<p><strong>Public Key URL:</strong> <a href="{WELL_KNOWN_KEY_PATH}"><code>{WELL_KNOWN_KEY_PATH}</code></a> ({key_status})</p>'
                f'<p><strong>Tesla Client Credentials:</strong> {creds_status}</p>'
                f'<p><strong>Storage Backend:</strong> <code>{escape(store.backend_name)}</code></p>'
                '</div>'
                '<div class="box">'
                '<form method="post" action="/oauth/register" style="display:inline">'
                '<button class="btn secondary" type="submit">1. Register Partner Domain</button></form>'
                '<a class="btn" href="/oauth/login">2. Login with Tesla</a>'
                '</div>'
            )
            self._send_bytes(
                200,
                _render_simple_html('Tesla Fleet API OAuth Setup', body),
                'text/html; charset=utf-8',
            )

        def _handle_oauth_register(self, cfg: AppConfig) -> None:
            if not cfg.tesla.client_id or not cfg.tesla.client_secret:
                self._send_bytes(
                    500,
                    _render_simple_html(
                        'Missing Configuration',
                        '<p><code>TESLA_CLIENT_ID</code> and <code>TESLA_CLIENT_SECRET</code> must be set.</p>',
                    ),
                    'text/html; charset=utf-8',
                )
                return

            domain = self._effective_partner_domain()
            scopes = os.getenv('TESLA_SCOPES', DEFAULT_SCOPES)
            try:
                with http_client_factory() as http:
                    result = register_partner_domain(
                        http=http,
                        client_id=cfg.tesla.client_id,
                        client_secret=cfg.tesla.client_secret,
                        domain=domain,
                        api_base_url=cfg.tesla.api_base_url,
                        scopes=scopes,
                    )
            except Exception as err:
                self._send_bytes(
                    502,
                    _render_simple_html(
                        'Partner Registration Failed',
                        f'<p>Failed to register <code>{escape(domain)}</code>:</p><pre>{escape(str(err))}</pre>'
                        '<p><a href="/oauth">Back</a></p>',
                    ),
                    'text/html; charset=utf-8',
                )
                return

            self._send_bytes(
                200,
                _render_simple_html(
                    'Partner Domain Registered',
                    f'<p class="ok">Registered <code>{escape(domain)}</code> with Tesla Fleet API.</p>'
                    f'<pre>{escape(json.dumps(result, indent=2))}</pre>'
                    '<p><a class="btn" href="/oauth/login">Continue to Tesla Login</a> <a href="/oauth">Back</a></p>',
                ),
                'text/html; charset=utf-8',
            )

        def _handle_oauth_login(self, cfg: AppConfig) -> None:
            if not cfg.tesla.client_id or not cfg.tesla.client_secret:
                self._send_bytes(
                    500,
                    _render_simple_html(
                        'Missing Configuration',
                        '<p><code>TESLA_CLIENT_ID</code> and <code>TESLA_CLIENT_SECRET</code> must be set.</p>',
                    ),
                    'text/html; charset=utf-8',
                )
                return

            state = secrets.token_urlsafe(24)
            auth_url = build_authorization_url(
                client_id=cfg.tesla.client_id,
                redirect_uri=self._effective_redirect_uri(),
                state=state,
                scopes=os.getenv('TESLA_SCOPES', DEFAULT_SCOPES),
            )
            cookie_flags = 'HttpOnly; SameSite=Lax; Path=/; Max-Age=600'
            if self._request_scheme() == 'https':
                cookie_flags += '; Secure'

            self._send_bytes(
                302,
                b'',
                'text/plain; charset=utf-8',
                extra_headers={
                    'Location': auth_url,
                    'Set-Cookie': f'{STATE_COOKIE_NAME}={state}; {cookie_flags}',
                },
            )

        def _handle_oauth_callback(
            self,
            cfg: AppConfig,
            store: StateStore,
            active_cache: AppCache,
            query: Dict[str, Any],
        ) -> None:
            if 'error' in query:
                err = query['error'][0]
                desc = (query.get('error_description') or [''])[0]
                self._send_bytes(
                    400,
                    _render_simple_html(
                        'Tesla Authorization Error',
                        f'<p>Tesla returned an error: <code>{escape(err)}</code> {escape(desc)}</p>',
                    ),
                    'text/html; charset=utf-8',
                )
                return

            code = (query.get('code') or [''])[0]
            returned_state = (query.get('state') or [''])[0]
            expected_state = self._get_cookie(STATE_COOKIE_NAME)

            if not code:
                self._send_bytes(
                    400,
                    _render_simple_html(
                        'Missing Code',
                        '<p>Callback URL did not contain a <code>code</code> parameter.</p>',
                    ),
                    'text/html; charset=utf-8',
                )
                return
            if not expected_state or not secrets.compare_digest(returned_state, expected_state):
                self._send_bytes(
                    400,
                    _render_simple_html(
                        'Invalid State',
                        '<p>OAuth <code>state</code> cookie missing or mismatched. '
                        'Please <a href="/oauth/login">start the login flow again</a>.</p>',
                    ),
                    'text/html; charset=utf-8',
                )
                return

            try:
                with http_client_factory() as http:
                    token_data = exchange_code_for_tokens(
                        http=http,
                        client_id=cfg.tesla.client_id,
                        client_secret=cfg.tesla.client_secret,
                        code=code,
                        redirect_uri=self._effective_redirect_uri(),
                        api_base_url=cfg.tesla.api_base_url,
                        scopes=os.getenv('TESLA_SCOPES', DEFAULT_SCOPES),
                    )
                    energy_site_id = discover_energy_site_id(
                        http=http,
                        access_token=token_data['access_token'],
                        api_base_url=cfg.tesla.api_base_url,
                    )
            except Exception as err:
                self._send_bytes(
                    502,
                    _render_simple_html(
                        'Token Exchange Failed',
                        f'<p>Could not exchange authorization code with Tesla:</p><pre>{escape(str(err))}</pre>',
                    ),
                    'text/html; charset=utf-8',
                )
                return

            try:
                store.set_json(TOKEN_STATE_KEY, token_data)
                active_cache.expire_tag([DASHBOARD_STATUS_TAG, 'tesla-site'])
            except Exception as err:
                self._send_bytes(
                    500,
                    _render_simple_html(
                        'Failed to Persist Tokens',
                        f'<p>Tokens were received from Tesla, but could not be saved:</p><pre>{escape(str(err))}</pre>',
                    ),
                    'text/html; charset=utf-8',
                )
                return

            site_html = (
                f'<p><strong>Discovered <code>ENERGY_SITE_ID</code>:</strong> <code>{escape(energy_site_id)}</code></p>'
                if energy_site_id
                else ''
            )
            self._send_bytes(
                200,
                _render_simple_html(
                    'Tesla Authentication Complete',
                    f'<p class="ok">OAuth tokens saved to <code>{escape(store.backend_name)}</code>.</p>'
                    f'{site_html}'
                    '<p>Set <code>OAUTH_ENABLED=false</code> in Vercel project settings '
                    'so only the dashboard and public key endpoint remain active.</p>'
                    '<p><a class="btn" href="/">Open Dashboard</a></p>',
                ),
                'text/html; charset=utf-8',
                extra_headers={
                    'Set-Cookie': f'{STATE_COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'
                },
            )

    return WebHandler


handler = make_web_handler()
