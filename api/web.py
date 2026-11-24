"""Vercel Function serving the Pool on Solar dashboard, API, and Tesla Fleet OAuth routes."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from html import escape
from http import cookies
from http.server import BaseHTTPRequestHandler
import json
import os
import secrets
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from pool_on_solar.cache import (
    DASHBOARD_STATUS_TAG,
    TESLA_SITE_TAG,
    AppCache,
    create_app_cache,
)
from pool_on_solar.config import AppConfig, load_config, parse_bool_env
from pool_on_solar.dashboard import StatusCollector, render_dashboard_html
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


@dataclass(frozen=True)
class _Runtime:
    config: AppConfig
    store: StateStore
    cache: AppCache
    collector: StatusCollector


@lru_cache(maxsize=1)
def _default_runtime() -> _Runtime:
    cfg = load_config()
    store = create_state_store()
    cache = create_app_cache()
    return _Runtime(
        config=cfg,
        store=store,
        cache=cache,
        collector=StatusCollector(cfg, store, cache=cache),
    )


def load_public_key_pem() -> Optional[str]:
    env_pem = os.getenv('TESLA_PUBLIC_KEY_PEM')
    return (env_pem.replace('\\n', '\n').strip() + '\n') if env_pem else None


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
        def _runtime(self) -> _Runtime:
            if config and state_store and collector and cache:
                return _Runtime(config=config, store=state_store, cache=cache, collector=collector)
            default = _default_runtime()
            return _Runtime(
                config=config or default.config,
                store=state_store or default.store,
                cache=cache or default.cache,
                collector=collector or default.collector,
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
            return 'http' if host.startswith(('localhost', '127.0.0.1')) else 'https'

        def _is_same_origin(self) -> bool:
            host = self._request_host().lower()
            for header in ('Origin', 'Referer'):
                value = self.headers.get(header)
                if value:
                    return (urlparse(value).netloc or '').lower() == host
            return True

        def _get_cookie(self, name: str) -> Optional[str]:
            raw = self.headers.get('Cookie')
            if not raw:
                return None
            jar = cookies.SimpleCookie()
            jar.load(raw)
            morsel = jar.get(name)
            return morsel.value if morsel else None

        def _cookie_flags(self, max_age: int) -> str:
            flags = f'HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}'
            return f'{flags}; Secure' if self._request_scheme() == 'https' else flags

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
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_html(
            self,
            status: int,
            title: str,
            body_html: str,
            extra_headers: Optional[Dict[str, str]] = None,
        ) -> None:
            self._send_bytes(
                status,
                _render_simple_html(title, body_html),
                'text/html; charset=utf-8',
                extra_headers=extra_headers,
            )

        def _redirect(
            self,
            location: str,
            status: int = 303,
            extra_headers: Optional[Dict[str, str]] = None,
        ) -> None:
            headers = {'Location': location, **(extra_headers or {})}
            self._send_bytes(status, b'', 'text/plain; charset=utf-8', extra_headers=headers)

        def _check_access_gate(self, path: str, query: Dict[str, Any]) -> bool:
            """Optional token gate (DASHBOARD_ACCESS_TOKEN) when not using Deployment Protection."""
            expected = os.getenv('DASHBOARD_ACCESS_TOKEN', '')
            if not expected:
                return True

            cookie_val = self._get_cookie(SESSION_COOKIE_NAME) or ''
            if cookie_val and secrets.compare_digest(cookie_val, expected):
                return True

            query_val = (query.get('token') or [''])[0]
            if query_val and secrets.compare_digest(query_val, expected):
                self._redirect(
                    path or '/',
                    extra_headers={
                        'Set-Cookie': f'{SESSION_COOKIE_NAME}={expected}; {self._cookie_flags(2592000)}'
                    },
                )
                return False

            self._send_html(
                401,
                'Access Restricted',
                '<p>Sign in with your access token: append <code>?token=...</code> to the URL.</p>',
            )
            return False

        def _effective_redirect_uri(self) -> str:
            return os.getenv('TESLA_REDIRECT_URI') or (
                f'{self._request_scheme()}://{self._request_host()}/oauth/callback'
            )

        def _effective_partner_domain(self) -> str:
            override = os.getenv('TESLA_PARTNER_DOMAIN')
            if override:
                return override.strip().lower()
            return (urlparse(self._effective_redirect_uri()).hostname or '').lower()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)

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

            rt = self._runtime()
            oidc_token = self.headers.get('x-vercel-oidc-token')
            force = (query.get('refresh') or [''])[0].lower() in {'1', 'true', 'yes'}

            if path in {'/', '/api/web'}:
                snapshot = rt.collector.get_snapshot(force_refresh=force, oidc_token=oidc_token)
                self._send_bytes(200, render_dashboard_html(snapshot), 'text/html; charset=utf-8')
                return

            if path == '/api/status':
                snapshot = rt.collector.get_snapshot(force_refresh=force, oidc_token=oidc_token)
                self._send_bytes(
                    200,
                    json.dumps(snapshot, indent=2).encode('utf-8'),
                    'application/json; charset=utf-8',
                )
                return

            if path in {'/oauth', '/oauth/', '/oauth/login', '/oauth/callback'}:
                if not parse_bool_env('OAUTH_ENABLED', default=False):
                    self._send_html(
                        403,
                        'OAuth Flow Disabled',
                        '<p>Set <code>OAUTH_ENABLED=true</code> in Vercel environment variables '
                        'to run interactive Tesla OAuth setup. The public key endpoint remains active.</p>',
                    )
                    return
                if path in {'/oauth', '/oauth/'}:
                    self._handle_oauth_index(rt)
                elif path == '/oauth/login':
                    self._handle_oauth_login(rt.config)
                else:
                    self._handle_oauth_callback(rt, query)
                return

            self._send_bytes(404, b'Not Found\n', 'text/plain; charset=utf-8')

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)

            if not self._check_access_gate(path, query):
                return

            if not self._is_same_origin():
                self._send_bytes(403, b'Cross-origin POST rejected.\n', 'text/plain; charset=utf-8')
                return

            rt = self._runtime()
            oidc_token = self.headers.get('x-vercel-oidc-token')

            if path in {'/scheduler/pause', '/scheduler/resume'}:
                action = 'pause' if path == '/scheduler/pause' else 'resume'
                try:
                    rt.collector.toggle_scheduler(action, oidc_token=oidc_token)
                except Exception as err:
                    self._send_html(
                        502,
                        f'Failed to {action} schedule',
                        f'<pre>{escape(str(err))}</pre><p><a href="/">Back</a></p>',
                    )
                    return
                self._redirect('/')
                return

            if path == '/oauth/register':
                if not parse_bool_env('OAUTH_ENABLED', default=False):
                    self._send_html(
                        403,
                        'OAuth Flow Disabled',
                        '<p>Set <code>OAUTH_ENABLED=true</code> first.</p>',
                    )
                    return
                self._handle_oauth_register(rt.config)
                return

            self._send_bytes(404, b'Not Found\n', 'text/plain; charset=utf-8')

        def _require_tesla_credentials(self, cfg: AppConfig) -> bool:
            if cfg.tesla.client_id and cfg.tesla.client_secret:
                return True
            self._send_html(
                500,
                'Missing Configuration',
                '<p><code>TESLA_CLIENT_ID</code> and <code>TESLA_CLIENT_SECRET</code> must be set.</p>',
            )
            return False

        def _handle_oauth_index(self, rt: _Runtime) -> None:
            key_status = (
                '<span class="ok">Configured</span>'
                if public_key_loader()
                else '<span class="warn">Missing (set TESLA_PUBLIC_KEY_PEM)</span>'
            )
            creds_status = (
                '<span class="ok">Configured</span>'
                if rt.config.tesla.client_id and rt.config.tesla.client_secret
                else '<span class="warn">Missing TESLA_CLIENT_ID / TESLA_CLIENT_SECRET</span>'
            )
            body = (
                '<div class="box">'
                f'<p><strong>Partner Domain:</strong> <code>{escape(self._effective_partner_domain())}</code></p>'
                f'<p><strong>Redirect URI:</strong> <code>{escape(self._effective_redirect_uri())}</code></p>'
                f'<p><strong>Public Key URL:</strong> <a href="{WELL_KNOWN_KEY_PATH}"><code>{WELL_KNOWN_KEY_PATH}</code></a> ({key_status})</p>'
                f'<p><strong>Tesla Client Credentials:</strong> {creds_status}</p>'
                f'<p><strong>Storage Backend:</strong> <code>{escape(rt.store.backend_name)}</code></p>'
                '</div>'
                '<div class="box">'
                '<form method="post" action="/oauth/register" style="display:inline">'
                '<button class="btn secondary" type="submit">1. Register Partner Domain</button></form>'
                '<a class="btn" href="/oauth/login">2. Login with Tesla</a>'
                '</div>'
            )
            self._send_html(200, 'Tesla Fleet API OAuth Setup', body)

        def _handle_oauth_register(self, cfg: AppConfig) -> None:
            if not self._require_tesla_credentials(cfg):
                return

            domain = self._effective_partner_domain()
            try:
                with http_client_factory() as http:
                    result = register_partner_domain(
                        http=http,
                        client_id=cfg.tesla.client_id,
                        client_secret=cfg.tesla.client_secret,
                        domain=domain,
                        api_base_url=cfg.tesla.api_base_url,
                        scopes=os.getenv('TESLA_SCOPES', DEFAULT_SCOPES),
                    )
            except Exception as err:
                self._send_html(
                    502,
                    'Partner Registration Failed',
                    f'<p>Failed to register <code>{escape(domain)}</code>:</p><pre>{escape(str(err))}</pre>'
                    '<p><a href="/oauth">Back</a></p>',
                )
                return

            self._send_html(
                200,
                'Partner Domain Registered',
                f'<p class="ok">Registered <code>{escape(domain)}</code> with Tesla Fleet API.</p>'
                f'<pre>{escape(json.dumps(result, indent=2))}</pre>'
                '<p><a class="btn" href="/oauth/login">Continue to Tesla Login</a> <a href="/oauth">Back</a></p>',
            )

        def _handle_oauth_login(self, cfg: AppConfig) -> None:
            if not self._require_tesla_credentials(cfg):
                return

            state = secrets.token_urlsafe(24)
            auth_url = build_authorization_url(
                client_id=cfg.tesla.client_id,
                redirect_uri=self._effective_redirect_uri(),
                state=state,
                scopes=os.getenv('TESLA_SCOPES', DEFAULT_SCOPES),
            )
            self._redirect(
                auth_url,
                status=302,
                extra_headers={'Set-Cookie': f'{STATE_COOKIE_NAME}={state}; {self._cookie_flags(600)}'},
            )

        def _handle_oauth_callback(self, rt: _Runtime, query: Dict[str, Any]) -> None:
            if 'error' in query:
                err = query['error'][0]
                desc = (query.get('error_description') or [''])[0]
                self._send_html(
                    400,
                    'Tesla Authorization Error',
                    f'<p>Tesla returned an error: <code>{escape(err)}</code> {escape(desc)}</p>',
                )
                return

            code = (query.get('code') or [''])[0]
            returned_state = (query.get('state') or [''])[0]
            expected_state = self._get_cookie(STATE_COOKIE_NAME)

            if not code:
                self._send_html(
                    400,
                    'Missing Code',
                    '<p>Callback URL did not contain a <code>code</code> parameter.</p>',
                )
                return
            if not expected_state or not secrets.compare_digest(returned_state, expected_state):
                self._send_html(
                    400,
                    'Invalid State',
                    '<p>OAuth <code>state</code> cookie missing or mismatched. '
                    'Please <a href="/oauth/login">start the login flow again</a>.</p>',
                )
                return

            cfg = rt.config
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
                self._send_html(
                    502,
                    'Token Exchange Failed',
                    f'<p>Could not exchange authorization code with Tesla:</p><pre>{escape(str(err))}</pre>',
                )
                return

            try:
                rt.store.set_json(TOKEN_STATE_KEY, token_data)
                rt.cache.expire_tag([DASHBOARD_STATUS_TAG, TESLA_SITE_TAG])
            except Exception as err:
                self._send_html(
                    500,
                    'Failed to Persist Tokens',
                    f'<p>Tokens were received from Tesla, but could not be saved:</p><pre>{escape(str(err))}</pre>',
                )
                return

            site_html = (
                f'<p><strong>Discovered <code>ENERGY_SITE_ID</code>:</strong> <code>{escape(energy_site_id)}</code></p>'
                if energy_site_id
                else ''
            )
            self._send_html(
                200,
                'Tesla Authentication Complete',
                f'<p class="ok">OAuth tokens saved to <code>{escape(rt.store.backend_name)}</code>.</p>'
                f'{site_html}'
                '<p>Set <code>OAUTH_ENABLED=false</code> in Vercel project settings '
                'so only the dashboard and public key endpoint remain active.</p>'
                '<p><a class="btn" href="/">Open Dashboard</a></p>',
                extra_headers={'Set-Cookie': f'{STATE_COOKIE_NAME}=; {self._cookie_flags(0)}'},
            )

    return WebHandler


handler = make_web_handler()
