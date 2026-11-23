"""Client for the Vercel Schedules HTTP API (https://vercel-schedules.com)."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx

DEFAULT_SCHEDULES_BASE_URL = 'https://vercel-schedules.com'


class VercelSchedulesError(RuntimeError):
    """Raised when a Vercel Schedules API request fails."""


def _normalize_schedule_state(schedule: Dict[str, Any]) -> str:
    """Maps Vercel Schedules status/enabled fields to ENABLED / PAUSED."""
    if isinstance(schedule.get('enabled'), bool):
        return 'ENABLED' if schedule['enabled'] else 'PAUSED'
    status = str(schedule.get('status') or '').strip().lower()
    if status in {'active', 'enabled'}:
        return 'ENABLED'
    if status in {'disabled', 'paused', 'inactive'}:
        return 'PAUSED'
    return status.upper() if status else 'UNKNOWN'


def _extract_cron(schedule: Dict[str, Any], fallback_cron: str) -> str:
    expr = schedule.get('expression')
    if isinstance(expr, dict) and expr.get('cron'):
        return str(expr['cron'])
    if schedule.get('schedule'):
        return str(schedule['schedule'])
    if schedule.get('cron'):
        return str(schedule['cron'])
    return fallback_cron


def _normalize_execution_status(execution: Dict[str, Any]) -> Optional[str]:
    raw = str(execution.get('status') or execution.get('state') or '').strip().lower()
    if raw in {'succeeded', 'success', 'completed', 'ok'}:
        return 'succeeded'
    if raw in {'failed', 'error', 'errored', 'timed_out', 'timeout'}:
        return 'failed'
    if raw in {'running', 'in_progress'}:
        return 'running'
    if raw in {'pending', 'queued', 'scheduled'}:
        return 'pending'
    status_code = execution.get('statusCode') or execution.get('responseStatus')
    if isinstance(status_code, int):
        return 'succeeded' if 200 <= status_code < 300 else 'failed'
    return raw or None


class VercelSchedulesClient:
    """Manages native Vercel Schedules status, execution history, and enable/disable."""

    def __init__(
        self,
        schedule_name: str = 'pool-on-solar',
        namespace: str = 'default',
        oidc_token: Optional[str] = None,
        api_token: Optional[str] = None,
        project_id: Optional[str] = None,
        team_id: Optional[str] = None,
        base_url: str = DEFAULT_SCHEDULES_BASE_URL,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._schedule_name = schedule_name
        self._namespace = namespace
        self._oidc_token = oidc_token or os.getenv('VERCEL_OIDC_TOKEN')
        self._api_token = api_token or os.getenv('VERCEL_TOKEN')
        self._project_id = project_id or os.getenv('VERCEL_PROJECT_ID')
        self._team_id = team_id or os.getenv('VERCEL_TEAM_ID')
        self._base_url = base_url.rstrip('/')
        self._http = http_client

    def _auth_token(self) -> tuple[str, bool]:
        """Returns (bearer_token, is_oidc)."""
        if self._oidc_token:
            return self._oidc_token, True
        if self._api_token:
            return self._api_token, False
        raise VercelSchedulesError(
            'Vercel Schedules API authentication unavailable (missing VERCEL_OIDC_TOKEN / VERCEL_TOKEN).'
        )

    def _params(self, is_oidc: bool, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {'namespace': self._namespace}
        if not is_oidc:
            if self._project_id:
                params['projectId'] = self._project_id
            if self._team_id:
                params['teamId'] = self._team_id
        if extra:
            params.update(extra)
        return params

    def _request(
        self,
        method: str,
        path: str,
        extra_params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        token, is_oidc = self._auth_token()
        url = f'{self._base_url}{path}'
        headers = {'Authorization': f'Bearer {token}'}
        if json_body is not None:
            headers['Content-Type'] = 'application/json'
        params = self._params(is_oidc, extra=extra_params)

        if self._http is not None:
            return self._http.request(method, url, params=params, json=json_body, headers=headers)
        with httpx.Client(timeout=15.0) as client:
            return client.request(method, url, params=params, json=json_body, headers=headers)

    def get_schedule(self) -> Dict[str, Any]:
        resp = self._request('GET', f'/v1/schedules/{self._schedule_name}')
        if resp.status_code >= 400:
            raise VercelSchedulesError(
                f'Failed to fetch schedule "{self._schedule_name}" ({resp.status_code}): {resp.text}'
            )
        payload = resp.json()
        if isinstance(payload, dict) and isinstance(payload.get('schedule'), dict):
            return payload['schedule']
        return payload if isinstance(payload, dict) else {}

    def get_latest_execution(self) -> Optional[Dict[str, Any]]:
        resp = self._request(
            'GET',
            f'/v1/schedules/{self._schedule_name}/executions',
            extra_params={'limit': '1'},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise VercelSchedulesError(
                f'Failed to fetch executions for "{self._schedule_name}" ({resp.status_code}): {resp.text}'
            )
        payload = resp.json()
        items = None
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get('executions') or payload.get('data') or payload.get('items')
        if isinstance(items, list) and items:
            return items[0] if isinstance(items[0], dict) else None
        return None

    def enable(self) -> Dict[str, Any]:
        resp = self._request('POST', f'/v1/schedules/{self._schedule_name}/enable', json_body={})
        if resp.status_code >= 400:
            raise VercelSchedulesError(
                f'Failed to enable schedule "{self._schedule_name}" ({resp.status_code}): {resp.text}'
            )
        return resp.json() if resp.text else {'status': 'active'}

    def disable(self) -> Dict[str, Any]:
        resp = self._request('POST', f'/v1/schedules/{self._schedule_name}/disable')
        if resp.status_code >= 400:
            raise VercelSchedulesError(
                f'Failed to disable schedule "{self._schedule_name}" ({resp.status_code}): {resp.text}'
            )
        return resp.json() if resp.text else {'status': 'disabled'}

    def get_health(self, fallback_cron: str = '*/15 * * * *') -> Dict[str, Any]:
        schedule = self.get_schedule()
        latest_exec: Optional[Dict[str, Any]] = None
        try:
            latest_exec = self.get_latest_execution()
        except Exception:
            latest_exec = schedule.get('lastExecution') if isinstance(schedule.get('lastExecution'), dict) else None

        job_info: Dict[str, Any] = {'name': self._schedule_name}
        if latest_exec:
            job_info['status'] = _normalize_execution_status(latest_exec)
            job_info['started_at'] = latest_exec.get('startedAt') or latest_exec.get('scheduledAt')
            job_info['completed_at'] = latest_exec.get('completedAt') or latest_exec.get('finishedAt')
            job_info['error'] = latest_exec.get('error')
        elif schedule.get('lastRunAt'):
            job_info['completed_at'] = schedule.get('lastRunAt')

        return {
            'status': 'ok',
            'job': job_info,
            'scheduler': {
                'name': self._schedule_name,
                'state': _normalize_schedule_state(schedule),
                'schedule': _extract_cron(schedule, fallback_cron),
            },
        }
