"""Vercel Schedules code-declared jobs using the official `vercel-schedules` Python SDK."""

from __future__ import annotations

from datetime import timedelta
import json
import os
from typing import Optional

from vercel.schedules import ScheduleMetadata, schedule

from pool_on_solar.analysis import run_daily_analysis
from pool_on_solar.automation import run_scheduled_automation
from pool_on_solar.cache import create_app_cache
from pool_on_solar.config import load_config
from pool_on_solar.storage import create_state_store


def _resolve_oidc_token() -> Optional[str]:
    try:
        from vercel.oidc import get_vercel_oidc_token

        token = get_vercel_oidc_token()
        if token:
            return token
    except Exception:
        pass
    return os.getenv('VERCEL_OIDC_TOKEN')


@schedule(
    cron='*/15 * * * *',
    name='automation',
    timezone='America/Los_Angeles',
    max_duration=timedelta(seconds=60),
)
async def automation_job(metadata: ScheduleMetadata) -> None:
    """Runs pool & lights solar-surplus automation every 15 minutes."""
    print(
        f'Starting scheduled {metadata.name} run '
        f'(executionId={metadata.execution_id}, scheduledAt={metadata.scheduled_at.isoformat()})'
    )
    config = load_config()
    state_store = create_state_store()
    cache = create_app_cache()
    result = await run_scheduled_automation(
        config=config,
        state_store=state_store,
        execution_id=metadata.execution_id,
        scheduled_at=metadata.scheduled_at.isoformat(),
        cache=cache,
    )
    print(json.dumps(result))


@schedule(
    cron='0 4 * * *',
    name='analysis',
    timezone='America/Los_Angeles',
    max_duration=timedelta(seconds=120),
)
def analysis_job(metadata: ScheduleMetadata) -> None:
    """Fetches yesterday's Tesla Energy power & energy history and inserts rows into BigQuery."""
    print(
        f'Starting scheduled {metadata.name} BigQuery sync '
        f'(executionId={metadata.execution_id}, scheduledAt={metadata.scheduled_at.isoformat()})'
    )
    config = load_config()
    state_store = create_state_store()
    cache = create_app_cache()
    result = run_daily_analysis(
        config=config,
        state_store=state_store,
        execution_id=metadata.execution_id,
        oidc_token=_resolve_oidc_token(),
        cache=cache,
    )
    print(json.dumps(result))
