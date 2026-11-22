"""Cloud Run monitoring dashboard server for Pool on Solar (intended behind IAP)."""

from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from dashboard import (
    DashboardConfig,
    StatusCollector,
    render_dashboard_html,
)


def make_handler(collector: StatusCollector):
    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            print(f'[dashboard] {self.address_string()} - {format % args}')

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

        def _request_host(self) -> str:
            forwarded = self.headers.get('X-Forwarded-Host')
            if forwarded:
                return forwarded.split(',')[0].strip().lower()
            return (self.headers.get('Host') or '').strip().lower()

        def _is_same_origin(self) -> bool:
            host = self._request_host()
            origin = self.headers.get('Origin')
            if origin:
                parsed = urlparse(origin)
                origin_host = (parsed.netloc or '').lower()
                return bool(origin_host and origin_host == host)
            referer = self.headers.get('Referer')
            if referer:
                parsed = urlparse(referer)
                ref_host = (parsed.netloc or '').lower()
                return bool(ref_host and ref_host == host)
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == '/healthz':
                self._send_bytes(200, b'ok\n', 'text/plain; charset=utf-8')
                return

            force = (query.get('refresh') or [''])[0].lower() in {'1', 'true', 'yes'}

            if path == '/api/status':
                snapshot = collector.get_snapshot(force_refresh=force)
                body = json.dumps(snapshot, indent=2).encode('utf-8')
                self._send_bytes(200, body, 'application/json; charset=utf-8')
                return

            if path == '/':
                snapshot = collector.get_snapshot(force_refresh=force)
                body = render_dashboard_html(snapshot)
                self._send_bytes(200, body, 'text/html; charset=utf-8')
                return

            self._send_bytes(404, b'Not Found\n', 'text/plain; charset=utf-8')

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            if path not in {'/scheduler/pause', '/scheduler/resume'}:
                self._send_bytes(404, b'Not Found\n', 'text/plain; charset=utf-8')
                return

            if not self._is_same_origin():
                self._send_bytes(403, b'Cross-origin POST rejected.\n', 'text/plain; charset=utf-8')
                return

            action = 'pause' if path == '/scheduler/pause' else 'resume'
            try:
                collector.toggle_scheduler(action)
            except Exception as err:
                body = (
                    f'<!doctype html><html><body><h1>Failed to {escape(action)} schedule</h1>'
                    f'<pre>{escape(str(err))}</pre><p><a href="/">Back</a></p></body></html>'
                ).encode('utf-8')
                self._send_bytes(502, body, 'text/html; charset=utf-8')
                return

            self._send_bytes(
                303,
                b'',
                'text/plain; charset=utf-8',
                extra_headers={'Location': '/'},
            )

    return DashboardHandler


def create_server(
    host: str = '0.0.0.0',
    port: Optional[int] = None,
    collector: Optional[StatusCollector] = None,
) -> ThreadingHTTPServer:
    resolved_port = int(os.getenv('PORT', '8080')) if port is None else port
    resolved_collector = collector or StatusCollector(DashboardConfig.from_env())
    return ThreadingHTTPServer((host, resolved_port), make_handler(resolved_collector))


if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    server = create_server(port=port)
    print(f'Pool on Solar dashboard listening on 0.0.0.0:{port}')
    server.serve_forever()
