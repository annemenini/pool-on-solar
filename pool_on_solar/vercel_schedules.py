"""Schedule management adapter backed by the official `vercel-schedules` Python SDK."""

from __future__ import annotations

from contextlib import nullcontext
import os
from typing import Any, Callable, Dict, Optional

DEFAULT_AUTOMATION_SCHEDULE_NAME = 'pool-on-solar.automation'


class VercelSchedulesError(RuntimeError):
    """Raised when a Vercel Schedules SDK call fails."""


def _schedule_to_dict(schedule: Any, fallback_cron: str) -> Dict[str, Any]:
    state = 'ENABLED' if getattr(schedule, 'is_active', False) else 'PAUSED'
    expression = getattr(schedule, 'expression', None)
    return {
        'name': getattr(schedule, 'name', None) or DEFAULT_AUTOMATION_SCHEDULE_NAME,
        'state': state,
        'schedule': str(expression) if expression is not None else fallback_cron,
        'timezone': getattr(schedule, 'timezone', None),
    }


class VercelSchedulesClient:
    """Thin adapter over `vercel.schedules.sync` for reading and toggling schedules."""

    def __init__(
        self,
        schedule_name: Optional[str] = None,
        namespace: Optional[str] = None,
        oidc_token: Optional[str] = None,
        schedules_sdk: Optional[Any] = None,
        session_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._schedule_name = schedule_name or os.getenv(
            'VERCEL_SCHEDULE_NAME', DEFAULT_AUTOMATION_SCHEDULE_NAME
        )
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
        factory = self._session_factory
        if factory is None:
            try:
                from vercel.api import session as factory
            except ImportError:
                return nullcontext()
        options = sdk.SchedulesServiceOptions(
            credentials_factory=sdk.static_credentials_factory(self._oidc_token)
        )
        return factory(service_options=[options])

    def _call(self, method_name: str, fallback_cron: str) -> Dict[str, Any]:
        sdk = self._sdk()
        kwargs = {'namespace': self._namespace} if self._namespace else {}
        try:
            with self._session_ctx():
                schedule = getattr(sdk, method_name)(self._schedule_name, **kwargs)
        except Exception as err:
            raise VercelSchedulesError(
                f'{method_name}("{self._schedule_name}") failed: {err}'
            ) from err
        return _schedule_to_dict(schedule, fallback_cron)

    def get_schedule(self, fallback_cron: str = '*/15 * * * *') -> Dict[str, Any]:
        return self._call('get_schedule', fallback_cron)

    def enable(self, fallback_cron: str = '*/15 * * * *') -> Dict[str, Any]:
        return self._call('enable_schedule', fallback_cron)

    def disable(self, fallback_cron: str = '*/15 * * * *') -> Dict[str, Any]:
        return self._call('disable_schedule', fallback_cron)

    def get_health(self, fallback_cron: str = '*/15 * * * *') -> Dict[str, Any]:
        return {
            'status': 'ok',
            'job': {'name': self._schedule_name},
            'scheduler': self.get_schedule(fallback_cron=fallback_cron),
        }
