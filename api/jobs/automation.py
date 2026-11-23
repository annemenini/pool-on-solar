"""Private Vercel Schedule function (schedule/v1beta) running pool & lights automation."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler
import json
from typing import Any, Dict

from pool_on_solar.automation import run_scheduled_automation
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
        scheduled_at = metadata.get('scheduledAt')
        print(
            f'Starting scheduled pool-on-solar run '
            f'(executionId={execution_id}, scheduledAt={scheduled_at})'
        )
        try:
            config = load_config()
            state_store = create_state_store()
            cache = create_app_cache()
            result = asyncio.run(
                run_scheduled_automation(
                    config=config,
                    state_store=state_store,
                    execution_id=execution_id,
                    scheduled_at=scheduled_at,
                    cache=cache,
                )
            )
            self._send_json(200, result)
        except Exception as err:
            print(json.dumps({'message': f'Automation run failed: {err}', 'severity': 'ERROR'}))
            self._send_json(500, {'status': 'failed', 'error': str(err)})
