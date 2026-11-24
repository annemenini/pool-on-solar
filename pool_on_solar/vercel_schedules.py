"""Schedule management adapter backed by the official `vercel-schedules` Python SDK."""

from __future__ import annotations

from contextlib import nullcontext
import os
from typing import Any, Callable, Dict, Optional

DEFAULT_AUTOMATION_SCHEDULE_NAME = 'pool-on-solar.automation'


class VercelSchedulesError(RuntimeError):
    """Raised when a Vercel Schedules SDK call fails."""


def _schedule_to_dict(schedule: Any, fallback_cron: str) -> Dict[str, Any]:
    is_active = getattr(schedule, 'is_active', None)
    if isinstance(is_active, bool):
        state = 'ENABLED' if is_active else 'PAUSED'
    else:
        raw_state = str(getattr(schedule, 'state', '') or '').strip().lower()
        if raw_state in {'active', 'enabled'}:
            state = 'ENABLED'
        elif raw_state in {'inactive', 'disabled', 'paused'}:
            state = 'PAUSED'
        else:
            state = 'UNKNOWN'

    expression = getattr(schedule, 'expression', None)
    cron_str = str(expression) if expression is not None else fallback_cron

    return {
        'name': getattr(schedule, 'name', None) or DEFAULT_AUTOMATION_SCHEDULE_NAME,
        'state': state,
        'schedule': cron_str,
        'timezone': getattr(schedule, 'timezone', None),
    }


class VercelSchedulesClient:
    """Thin adapter over `vercel.schedules.sync` for reading and toggling schedules."""

    def __init__(
        self,
        schedule_name: str = DEFAULT_AUTOMATION_SCHEDULE_NAME,
        namespace: Optional[str] = None,
        oidc_token: Optional[str] = None,
        schedules_sdk: Optional[Any] = None,
        session_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._schedule_name = schedule_name
        self._namespace = namespace
        self._oidc_token = oidc_token or os.getenv('VERCEL_OIDC_TOKEN')
        self._schedules_sdk = schedules_sdk
        self._session_factory = session_factory

    def _sdk(self) -> Any:
        if self._schedules_sdk is None:
            from vercel.schedules import sync as schedules_sync

            self._schedules_sdk = schedules_sync
        return self._schedules_sdk

    def _session_ctx(self) -> Any:
        if not self._oidc_token:
            return nullcontext()

        sdk = self._sdk()
        options_cls = getattr(sdk, 'SchedulesServiceOptions', None)
        static_creds = getattr(sdk, 'static_credentials_factory', None)
        if options_cls is None or static_creds is None:
            return nullcontext()

        factory = self._session_factory
        if factory is None:
            try:
                from vercel.api import session as vercel_session

                factory = vercel_session
            except ImportError:
                return nullcontext()

        options = options_cls(credentials_factory=static_creds(self._oidc_token))
        return factory(service_options=[options])

    def _kwargs(self) -> Dict[str, Any]:
        return {'namespace': self._namespace} if self._namespace else {}

    def get_schedule(self, fallback_cron: str = '*/15 * * * *') -> Dict[str, Any]:
        sdk = self._sdk()
        try:
            with self._session_ctx():
                schedule = sdk.get_schedule(self._schedule_name, **self._kwargs())
        except Exception as err:
            raise VercelSchedulesError(
                f'Failed to fetch schedule "{self._schedule_name}": {err}'
            ) from err
        return _schedule_to_dict(schedule, fallback_cron)

    def enable(self, fallback_cron: str = '*/15 * * * *') -> Dict[str, Any]:
        sdk = self._sdk()
        try:
            with self._session_ctx():
                schedule = sdk.enable_schedule(self._schedule_name, **self._kwargs())
        except Exception as err:
            raise VercelSchedulesError(
                f'Failed to enable schedule "{self._schedule_name}": {err}'
            ) from err
        return _schedule_to_dict(schedule, fallback_cron)

    def disable(self, fallback_cron: str = '*/15 * * * *') -> Dict[str, Any]:
        sdk = self._sdk()
        try:
            with self._session_ctx():
                schedule = sdk.disable_schedule(self._schedule_name, **self._kwargs())
        except Exception as err:
            raise VercelSchedulesError(
                f'Failed to disable schedule "{self._schedule_name}": {err}'
            ) from err
        return _schedule_to_dict(schedule, fallback_cron)

    def get_health(self, fallback_cron: str = '*/15 * * * *') -> Dict[str, Any]:
        scheduler = self.get_schedule(fallback_cron=fallback_cron)
        return {
            'status': 'ok',
            'job': {'name': self._schedule_name},
            'scheduler': scheduler,
        }
