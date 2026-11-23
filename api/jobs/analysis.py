"""Private Vercel Schedule function (schedule/v1beta) archiving daily Tesla Energy history."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import json
from typing import Any, Dict

from pool_on_solar.analysis import run_daily_analysis
from pool_on_solar.cache import create_app_cache
from pool_on_solar.config import load_config
from pool_on_solar.storage import create_state_store


def _read_schedule_metadata(handler_obj: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler_obj.headers.get('Content-Length') or '0')
    if length <= 0:
        return {}
    raw = handler_obj.rfile.read(length).decode('utf-8')
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        metadata = _read_schedule_metadata(self)
        execution_id = metadata.get('executionId')
        oidc_token = self.headers.get('x-vercel-oidc-token')
        print(f'Starting daily Tesla energy history BigQuery sync (executionId={execution_id})')
        try:
            config = load_config()
            state_store = create_state_store()
            cache = create_app_cache()
            result = run_daily_analysis(
                config=config,
                state_store=state_store,
                execution_id=execution_id,
                oidc_token=oidc_token,
                cache=cache,
            )
            self._send_json(200, result)
        except Exception as err:
            print(json.dumps({'message': f'Daily analysis failed: {err}', 'severity': 'ERROR'}))
            self._send_json(500, {'status': 'failed', 'error': str(err)})
