"""Lightweight Cloud Run HTTP server for Tesla Fleet API public-key hosting and OAuth2 callback."""

from __future__ import annotations

from html import escape
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import secrets
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from main import (
    DEFAULT_API_BASE_URL,
    DEFAULT_SCOPES,
    build_authorization_url,
    build_httpx_client,
    discover_energy_site_id,
    exchange_code_for_tokens,
    register_partner_domain,
    save_tokens_to_file,
    save_tokens_to_secret_manager,
)

STATE_COOKIE_NAME = 'tesla_oauth_state'
WELL_KNOWN_KEY_PATH = '/.well-known/appspecific/com.tesla.3p.public-key.pem'


def parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def load_public_key_pem() -> Optional[str]:
    """Loads the PEM public key from TESLA_PUBLIC_KEY_PEM env var or file."""
    env_pem = os.getenv('TESLA_PUBLIC_KEY_PEM')
    if env_pem:
        return env_pem.replace('\\n', '\n').strip() + '\n'

    candidates = []
    if os.getenv('TESLA_PUBLIC_KEY_FILE'):
        candidates.append(Path(os.environ['TESLA_PUBLIC_KEY_FILE']))
    candidates.extend([
        Path('/app/public-key.pem'),
        Path(__file__).resolve().parent / 'public-key.pem',
        Path(__file__).resolve().parent / '.well-known' / 'appspecific' / 'com.tesla.3p.public-key.pem',
    ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding='utf-8').strip() + '\n'
    return None


def _render_html(title: str, body_html: str) -> bytes:
    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 680px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; color: #111; }}
    code, pre {{ background: #f4f4f5; padding: 0.15rem 0.35rem; border-radius: 4px; }}
    pre {{ padding: 0.75rem; overflow-x: auto; }}
    .btn {{ display: inline-block; padding: 0.6rem 1rem; background: #111; color: #fff; text-decoration: none; border-radius: 6px; border: 0; cursor: pointer; font-size: 1rem; margin-right: 0.5rem; }}
    .btn.secondary {{ background: #4b5563; }}
    .box {{ border: 1px solid #e4e4e7; border-radius: 8px; padding: 1rem 1.25rem; margin: 1rem 0; }}
    .ok {{ color: #15803d; font-weight: 600; }}
    .warn {{ color: #b45309; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>{escape(title)}</h1>
  {body_html}
</body>
</html>
"""
    return doc.encode('utf-8')


class OAuthServerConfig:
    """Runtime configuration read from environment variables."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        partner_domain: Optional[str] = None,
        api_base_url: Optional[str] = None,
        scopes: Optional[str] = None,
        token_secret_name: Optional[str] = None,
        token_file: Optional[str] = None,
        public_key_pem: Optional[str] = None,
        oauth_enabled: Optional[bool] = None,
        setup_token: Optional[str] = None,
    ) -> None:
        self.client_id = client_id if client_id is not None else os.getenv('TESLA_CLIENT_ID', '')
        self.client_secret = (
            client_secret if client_secret is not None else os.getenv('TESLA_CLIENT_SECRET', '')
        )
        self.redirect_uri = (
            redirect_uri if redirect_uri is not None else os.getenv('TESLA_REDIRECT_URI', '')
        )
        self.partner_domain = (
            partner_domain if partner_domain is not None else os.getenv('TESLA_PARTNER_DOMAIN', '')
        )
        self.api_base_url = (
            api_base_url
            if api_base_url is not None
            else os.getenv('TESLA_API_BASE_URL', DEFAULT_API_BASE_URL)
        )
        self.scopes = scopes if scopes is not None else os.getenv('TESLA_SCOPES', DEFAULT_SCOPES)
        self.token_secret_name = (
            token_secret_name
            if token_secret_name is not None
            else os.getenv('TESLA_TOKEN_SECRET_NAME', '')
        )
        self.token_file = token_file if token_file is not None else os.getenv('TESLA_TOKEN_FILE', '')
        self.public_key_pem = (
            public_key_pem if public_key_pem is not None else load_public_key_pem()
        )
        self.oauth_enabled = (
            oauth_enabled
            if oauth_enabled is not None
            else parse_bool_env('OAUTH_ENABLED', default=True)
        )
        self.setup_token = (
            setup_token if setup_token is not None else os.getenv('SETUP_TOKEN', '')
        )


def make_handler(
    config: OAuthServerConfig,
    http_client_factory: Callable[[], Any] = build_httpx_client,
    secret_manager_client: Optional[Any] = None,
):
    """Creates a BaseHTTPRequestHandler bound to config and injectable dependencies."""

    class TeslaOAuthHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            print(f'[oauth-server] {self.address_string()} - {format % args}')

        def _request_host(self) -> str:
            forwarded = self.headers.get('X-Forwarded-Host')
            if forwarded:
                return forwarded.split(',')[0].strip()
            return (self.headers.get('Host') or 'localhost:8080').strip()

        def _request_scheme(self) -> str:
            forwarded = self.headers.get('X-Forwarded-Proto')
            if forwarded:
                return forwarded.split(',')[0].strip()
            host = self._request_host()
            if host.startswith('localhost') or host.startswith('127.0.0.1'):
                return 'http'
            return 'https'

        def _effective_redirect_uri(self) -> str:
            if config.redirect_uri:
                return config.redirect_uri
            return f'{self._request_scheme()}://{self._request_host()}/callback'

        def _effective_partner_domain(self) -> str:
            if config.partner_domain:
                return config.partner_domain.strip().lower()
            redirect = self._effective_redirect_uri()
            parsed = urlparse(redirect)
            return (parsed.hostname or '').lower()

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

        def _send_html(
            self,
            status: int,
            title: str,
            body_html: str,
            extra_headers: Optional[Dict[str, str]] = None,
        ) -> None:
            self._send_bytes(
                status,
                _render_html(title, body_html),
                'text/html; charset=utf-8',
                extra_headers=extra_headers,
            )

        def _get_cookie(self, name: str) -> Optional[str]:
            raw = self.headers.get('Cookie')
            if not raw:
                return None
            jar = cookies.SimpleCookie()
            jar.load(raw)
            morsel = jar.get(name)
            return morsel.value if morsel else None

        def _check_setup_token(self, query: Dict[str, Any], form: Optional[Dict[str, Any]] = None) -> bool:
            if not config.setup_token:
                return True
            provided = (query.get('token') or [''])[0]
            if not provided and form:
                provided = (form.get('token') or [''])[0]
            return secrets.compare_digest(provided, config.setup_token)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == WELL_KNOWN_KEY_PATH:
                if not config.public_key_pem:
                    self._send_bytes(
                        404,
                        b'Public key not configured (set TESLA_PUBLIC_KEY_PEM or TESLA_PUBLIC_KEY_FILE).\n',
                        'text/plain; charset=utf-8',
                    )
                    return
                self._send_bytes(
                    200,
                    config.public_key_pem.encode('utf-8'),
                    'application/x-pem-file',
                )
                return

            if path == '/healthz':
                self._send_bytes(200, b'ok\n', 'text/plain; charset=utf-8')
                return

            if path == '/':
                self._handle_index()
                return

            if not config.oauth_enabled:
                self._send_html(
                    403,
                    'OAuth Flow Disabled',
                    '<p>Set <code>OAUTH_ENABLED=true</code> to re-enable interactive Tesla OAuth login. '
                    'The public key endpoint remains active.</p>',
                )
                return

            if path == '/login':
                self._handle_login(query)
                return

            if path == '/callback':
                self._handle_callback(query)
                return

            self._send_html(404, 'Not Found', '<p>Unknown route.</p>')

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != '/register':
                self._send_html(404, 'Not Found', '<p>Unknown route.</p>')
                return

            if not config.oauth_enabled:
                self._send_html(403, 'OAuth Flow Disabled', '<p>Set <code>OAUTH_ENABLED=true</code> first.</p>')
                return

            length = int(self.headers.get('Content-Length') or '0')
            body = self.rfile.read(length).decode('utf-8') if length > 0 else ''
            form = parse_qs(body)
            query = parse_qs(parsed.query)
            if not self._check_setup_token(query, form):
                self._send_html(403, 'Forbidden', '<p>Invalid or missing setup token.</p>')
                return

            if not config.client_id or not config.client_secret:
                self._send_html(
                    500,
                    'Missing Configuration',
                    '<p><code>TESLA_CLIENT_ID</code> and <code>TESLA_CLIENT_SECRET</code> must be set.</p>',
                )
                return

            domain = self._effective_partner_domain()
            try:
                with http_client_factory() as http:
                    result = register_partner_domain(
                        http=http,
                        client_id=config.client_id,
                        client_secret=config.client_secret,
                        domain=domain,
                        api_base_url=config.api_base_url,
                        scopes=config.scopes,
                    )
            except Exception as err:
                self._send_html(
                    502,
                    'Partner Registration Failed',
                    f'<p>Failed to register <code>{escape(domain)}</code>:</p><pre>{escape(str(err))}</pre>'
                    '<p><a href="/">Back</a></p>',
                )
                return

            self._send_html(
                200,
                'Partner Domain Registered',
                f'<p class="ok">Registered <code>{escape(domain)}</code> with Tesla Fleet API.</p>'
                f'<pre>{escape(str(result))}</pre>'
                '<p><a class="btn" href="/login">Continue to Tesla Login</a> <a href="/">Back</a></p>',
            )

        def _handle_index(self) -> None:
            redirect_uri = self._effective_redirect_uri()
            domain = self._effective_partner_domain()
            key_status = '<span class="ok">Configured</span>' if config.public_key_pem else '<span class="warn">Missing (set TESLA_PUBLIC_KEY_PEM)</span>'
            creds_status = (
                '<span class="ok">Configured</span>'
                if config.client_id and config.client_secret
                else '<span class="warn">Missing TESLA_CLIENT_ID / TESLA_CLIENT_SECRET</span>'
            )
            storage_status = (
                f'Secret Manager: <code>{escape(config.token_secret_name)}</code>'
                if config.token_secret_name
                else f'Local file: <code>{escape(config.token_file or "./cache.json")}</code>'
            )
            token_input = (
                '<p><label>Setup token: <input type="password" name="token" required></label></p>'
                if config.setup_token
                else ''
            )
            login_action = (
                '<form method="get" action="/login" style="display:inline">'
                f'{token_input}<button class="btn" type="submit">2. Login with Tesla</button></form>'
                if config.setup_token
                else '<a class="btn" href="/login">2. Login with Tesla</a>'
            )
            actions_html = (
                '<div class="box">'
                '<form method="post" action="/register" style="display:inline">'
                f'{token_input}<button class="btn secondary" type="submit">1. Register Partner Domain</button></form>'
                f'{login_action}'
                '</div>'
                if config.oauth_enabled
                else '<div class="box"><p>OAuth flow is disabled (<code>OAUTH_ENABLED=false</code>). '
                'Public key hosting remains active.</p></div>'
            )
            body = (
                '<div class="box">'
                f'<p><strong>Partner Domain:</strong> <code>{escape(domain)}</code></p>'
                f'<p><strong>Redirect URI:</strong> <code>{escape(redirect_uri)}</code></p>'
                f'<p><strong>Public Key URL:</strong> <a href="{WELL_KNOWN_KEY_PATH}"><code>{WELL_KNOWN_KEY_PATH}</code></a> ({key_status})</p>'
                f'<p><strong>Tesla Client Credentials:</strong> {creds_status}</p>'
                f'<p><strong>Token Destination:</strong> {storage_status}</p>'
                '</div>'
                f'{actions_html}'
            )
            self._send_html(200, 'Tesla Fleet API OAuth Setup', body)

        def _handle_login(self, query: Dict[str, Any]) -> None:
            if not self._check_setup_token(query):
                self._send_html(403, 'Forbidden', '<p>Invalid or missing setup token.</p>')
                return
            if not config.client_id or not config.client_secret:
                self._send_html(
                    500,
                    'Missing Configuration',
                    '<p><code>TESLA_CLIENT_ID</code> and <code>TESLA_CLIENT_SECRET</code> must be set.</p>',
                )
                return

            state = secrets.token_urlsafe(24)
            auth_url = build_authorization_url(
                client_id=config.client_id,
                redirect_uri=self._effective_redirect_uri(),
                scopes=config.scopes,
                state=state,
            )
            cookie_flags = 'HttpOnly; SameSite=Lax; Path=/; Max-Age=600'
            if self._request_scheme() == 'https':
                cookie_flags += '; Secure'

            self.send_response(302)
            self.send_header('Location', auth_url)
            self.send_header('Set-Cookie', f'{STATE_COOKIE_NAME}={state}; {cookie_flags}')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()

        def _handle_callback(self, query: Dict[str, Any]) -> None:
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
                self._send_html(400, 'Missing Code', '<p>Callback URL did not contain a <code>code</code> parameter.</p>')
                return
            if not expected_state or not secrets.compare_digest(returned_state, expected_state):
                self._send_html(
                    400,
                    'Invalid State',
                    '<p>OAuth <code>state</code> cookie missing or mismatched. '
                    'Please <a href="/login">start the login flow again</a>.</p>',
                )
                return

            try:
                with http_client_factory() as http:
                    token_data = exchange_code_for_tokens(
                        http=http,
                        client_id=config.client_id,
                        client_secret=config.client_secret,
                        code=code,
                        redirect_uri=self._effective_redirect_uri(),
                        api_base_url=config.api_base_url,
                        scopes=config.scopes,
                    )
                    energy_site_id = discover_energy_site_id(
                        http=http,
                        access_token=token_data['access_token'],
                        api_base_url=config.api_base_url,
                    )
            except Exception as err:
                self._send_html(
                    502,
                    'Token Exchange Failed',
                    f'<p>Could not exchange authorization code with Tesla:</p><pre>{escape(str(err))}</pre>',
                )
                return

            try:
                if config.token_secret_name:
                    saved_location = save_tokens_to_secret_manager(
                        token_data,
                        config.token_secret_name,
                        client=secret_manager_client,
                    )
                else:
                    output_path = config.token_file or './cache.json'
                    save_tokens_to_file(token_data, output_path)
                    saved_location = output_path
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
                else '<p>Could not auto-discover <code>ENERGY_SITE_ID</code>.</p>'
            )
            self._send_html(
                200,
                'Tesla Authentication Complete',
                '<p class="ok">OAuth tokens were saved successfully (older Secret Manager versions destroyed).</p>'
                f'<p><strong>Saved to:</strong> <code>{escape(saved_location)}</code></p>'
                f'{site_html}'
                '<p>You can now set <code>OAUTH_ENABLED=false</code> on this Cloud Run service '
                'so only the public key endpoint remains active.</p>',
                extra_headers={
                    'Set-Cookie': f'{STATE_COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'
                },
            )

    return TeslaOAuthHandler


def create_server(
    host: str = '0.0.0.0',
    port: Optional[int] = None,
    config: Optional[OAuthServerConfig] = None,
    http_client_factory: Callable[[], Any] = build_httpx_client,
    secret_manager_client: Optional[Any] = None,
) -> ThreadingHTTPServer:
    resolved_port = int(os.getenv('PORT', '8080')) if port is None else port
    resolved_config = config or OAuthServerConfig()
    handler_cls = make_handler(
        config=resolved_config,
        http_client_factory=http_client_factory,
        secret_manager_client=secret_manager_client,
    )
    return ThreadingHTTPServer((host, resolved_port), handler_cls)


if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    server = create_server(port=port)
    print(f'Tesla OAuth server listening on 0.0.0.0:{port}')
    server.serve_forever()
