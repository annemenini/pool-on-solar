"""Unit and HTTP integration tests for the Vercel deployment of Pool on Solar."""

from __future__ import annotations

import asyncio
import datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_optional_stubs() -> None:
    """Provides lightweight stubs when third-party deps aren't installed locally."""
    if 'httpx' not in sys.modules:
        try:
            import httpx  # noqa: F401
        except ImportError:
            stub = types.ModuleType('httpx')

            class _DummyClient:
                def __init__(self, *args, **kwargs):
                    pass

                def close(self):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.close()

            stub.Client = _DummyClient
            sys.modules['httpx'] = stub

    if 'iaqualink' not in sys.modules:
        try:
            import iaqualink  # noqa: F401
        except ImportError:
            iaq_mod = types.ModuleType('iaqualink')
            iaq_client_mod = types.ModuleType('iaqualink.client')
            iaq_client_mod.AqualinkClient = MagicMock()
            iaq_dev_mod = types.ModuleType('iaqualink.device')
            iaq_dev_mod.AqualinkDevice = object
            sys.modules['iaqualink'] = iaq_mod
            sys.modules['iaqualink.client'] = iaq_client_mod
            sys.modules['iaqualink.device'] = iaq_dev_mod

    if 'suntime' not in sys.modules:
        try:
            import suntime  # noqa: F401
        except ImportError:
            suntime_mod = types.ModuleType('suntime')
            suntime_mod.Sun = MagicMock()
            sys.modules['suntime'] = suntime_mod

    if 'vercel.schedules' not in sys.modules:
        try:
            import vercel.schedules  # noqa: F401
        except ImportError:
            vercel_pkg = sys.modules.get('vercel') or types.ModuleType('vercel')
            vercel_pkg.__path__ = getattr(vercel_pkg, '__path__', [])
            sys.modules['vercel'] = vercel_pkg

            sched_mod = types.ModuleType('vercel.schedules')
            registered: list = []

            class _ScheduleMetadata:
                def __init__(
                    self,
                    schedule_id: str,
                    execution_id: str,
                    name: str,
                    namespace: str,
                    scheduled_at: datetime.datetime,
                    source: str = 'static',
                    expression: str = '*/15 * * * *',
                ) -> None:
                    self.schedule_id = schedule_id
                    self.execution_id = execution_id
                    self.name = name
                    self.namespace = namespace
                    self.scheduled_at = scheduled_at
                    self.source = source
                    self.expression = expression

            def _schedule_decorator(**kwargs):
                def _wrap(fn):
                    registered.append((kwargs, fn))
                    return fn
                return _wrap

            sched_mod.ScheduleMetadata = _ScheduleMetadata
            sched_mod.schedule = _schedule_decorator
            sched_mod._registered = registered
            sys.modules['vercel.schedules'] = sched_mod


_install_optional_stubs()

from pool_on_solar.analysis import (  # noqa: E402
    exchange_vercel_oidc_for_gcp_access_token,
    run_daily_analysis,
)
from pool_on_solar.automation import (  # noqa: E402
    Controller,
    run_scheduled_automation,
)
from pool_on_solar.cache import (  # noqa: E402
    DASHBOARD_SNAPSHOT_CACHE_KEY,
    MemoryCache,
    VercelRuntimeCache,
    create_app_cache,
)
from pool_on_solar.config import AppConfig, load_config, parse_bool_env  # noqa: E402
from pool_on_solar.dashboard import (  # noqa: E402
    StatusCollector,
    render_dashboard_html,
)
from pool_on_solar.oauth import (  # noqa: E402
    build_authorization_url,
    discover_energy_site_id,
    exchange_code_for_tokens,
    register_partner_domain,
)
from pool_on_solar.storage import (  # noqa: E402
    LocalFileStateStore,
    VercelBlobStateStore,
    create_state_store,
)
from pool_on_solar.tesla_fleet import (  # noqa: E402
    TOKEN_STATE_KEY,
    TeslaFleetClient,
    TokenData,
)
from pool_on_solar.vercel_schedules import VercelSchedulesClient  # noqa: E402


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self, handlers):
        self._handlers = list(handlers)
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def post(self, url, data=None, json=None, headers=None):
        self.calls.append(('POST', url, {'data': data, 'json': json, 'headers': headers}))
        return self._handlers.pop(0)

    def get(self, url, params=None, headers=None):
        self.calls.append(('GET', url, {'params': params, 'headers': headers}))
        return self._handlers.pop(0)

    def request(self, method, url, params=None, json=None, headers=None):
        self.calls.append((method, url, {'params': params, 'json': json, 'headers': headers}))
        return self._handlers.pop(0)

    def close(self):
        self.closed = True


class VercelConfigAndStorageTests(unittest.TestCase):
    def test_vercel_json_schedules_and_python_version(self):
        self.assertEqual((REPO_ROOT / '.python-version').read_text(encoding='utf-8').strip(), '3.14')
        pyproject = (REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('>=3.14', pyproject)
        self.assertIn('vercel-schedules', pyproject)

        vcfg = json.loads((REPO_ROOT / 'vercel.json').read_text(encoding='utf-8'))
        self.assertTrue(vcfg['fluid'])
        self.assertIn('schedules', vcfg)
        self.assertNotIn('crons', vcfg)
        self.assertEqual(
            vcfg['schedules']['pool-on-solar'],
            {'root': '.', 'entrypoint': 'jobs'},
        )
        self.assertEqual(
            vcfg['rewrites'],
            [{'source': '/(.*)', 'destination': '/api/web'}],
        )

    def test_all_python_files_compile_without_syntax_errors(self):
        for path in sorted(REPO_ROOT.rglob('*.py')):
            if '.git' in path.parts:
                continue
            source = path.read_text(encoding='utf-8')
            compile(source, str(path.relative_to(REPO_ROOT)), 'exec')

    def test_config_loading_and_bool_env(self):
        with patch.dict(os.environ, {'DRY_RUN': 'false'}):
            self.assertFalse(parse_bool_env('DRY_RUN'))
        with patch.dict(os.environ, {'DRY_RUN': 'true'}):
            self.assertTrue(parse_bool_env('DRY_RUN'))

        with patch.dict(
            os.environ,
            {
                'TESLA_CLIENT_ID': 'cid-test',
                'ENERGY_SITE_ID': '2252243631420713',
                'IAQUALINK_USER_ID': 'u@example.com',
                'IAQUALINK_PASSWORD': 'secret',
            },
            clear=False,
        ):
            cfg = load_config()
            self.assertEqual(cfg.tesla.client_id, 'cid-test')
            self.assertEqual(cfg.tesla.energy_site_id, '2252243631420713')
            self.assertEqual(cfg.iaqualink.pool.devices, ['pool_pump', 'aux_1'])
            self.assertEqual(cfg.location.timezone, 'America/Los_Angeles')

    def test_local_state_store_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalFileStateStore(tmpdir)
            self.assertIsNone(store.get_json('missing/key.json'))
            store.set_json('tesla/token.json', {'access_token': 'a1', 'refresh_token': 'r1'})
            self.assertEqual(
                store.get_json('tesla/token.json'),
                {'access_token': 'a1', 'refresh_token': 'r1'},
            )
            mode = os.stat(Path(tmpdir) / 'tesla' / 'token.json').st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_vercel_blob_state_store_private_overwrite(self):
        stored = {}

        class _FakeBlobModule:
            @staticmethod
            def put_blob(pathname, body, **kwargs):
                assert kwargs['access'] == 'private'
                assert kwargs['allow_overwrite'] is True
                assert kwargs['add_random_suffix'] is False
                stored[pathname] = body

            @staticmethod
            def get_blob(pathname, **kwargs):
                assert kwargs['access'] == 'private'
                if pathname not in stored:
                    raise RuntimeError('BlobNotFoundError: not found')
                return types.SimpleNamespace(body=stored[pathname])

        store = VercelBlobStateStore(blob_module=_FakeBlobModule)
        self.assertIsNone(store.get_json('tesla/token.json'))
        store.set_json('tesla/token.json', {'access_token': 'blob-acc'})
        self.assertEqual(store.get_json('tesla/token.json'), {'access_token': 'blob-acc'})

    def test_create_state_store_precedence(self):
        with patch.dict(os.environ, {'BLOB_READ_WRITE_TOKEN': 'blob-tok'}, clear=True):
            self.assertEqual(create_state_store().backend_name, 'vercel-blob')

        with patch.dict(os.environ, {'BLOB_STORE_ID': 'store_123'}, clear=True):
            self.assertEqual(create_state_store().backend_name, 'vercel-blob')

        with patch.dict(os.environ, {'VERCEL': '1'}, clear=True):
            with self.assertRaises(RuntimeError):
                create_state_store()

    def test_vercel_runtime_cache_wrapper_and_fail_open_fallback(self):
        backed = {}
        backed_tags = {}

        class _FakeRuntimeCache:
            def get(self, key):
                return backed.get(key)

            def set(self, key, value, options=None):
                backed[key] = value
                backed_tags[key] = set((options or {}).get('tags') or ())

            def delete(self, key):
                backed.pop(key, None)
                backed_tags.pop(key, None)

            def expire_tag(self, tag):
                tags = {tag} if isinstance(tag, str) else set(tag)
                for k in list(backed.keys()):
                    if backed_tags.get(k, set()) & tags:
                        backed.pop(k, None)
                        backed_tags.pop(k, None)

        cache = VercelRuntimeCache(runtime_cache=_FakeRuntimeCache())
        cache.set('k1', {'v': 1}, ttl=60, tags=['dashboard-status'], name='test-entry')
        self.assertEqual(cache.get('k1'), {'v': 1})
        cache.expire_tag('dashboard-status')
        self.assertIsNone(cache.get('k1'))

        # If Runtime Cache raises, VercelRuntimeCache fails open to local MemoryCache
        class _BrokenRuntimeCache:
            def get(self, _key):
                raise RuntimeError('sidecar unavailable')

            def set(self, _key, _value, _options=None):
                raise RuntimeError('sidecar unavailable')

            def delete(self, _key):
                raise RuntimeError('sidecar unavailable')

            def expire_tag(self, _tag):
                raise RuntimeError('sidecar unavailable')

        broken = VercelRuntimeCache(runtime_cache=_BrokenRuntimeCache())
        broken.set('fallback-key', 'ok', ttl=60, tags=['t1'])
        self.assertEqual(broken.get('fallback-key'), 'ok')
        broken.expire_tag('t1')
        self.assertIsNone(broken.get('fallback-key'))

        with patch.dict(os.environ, {'VERCEL': '1'}, clear=True):
            self.assertEqual(create_app_cache().backend_name, 'vercel-runtime-cache')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(create_app_cache().backend_name, 'memory')


class TeslaFleetAndAutomationTests(unittest.TestCase):
    def test_token_refresh_rotation_and_401_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalFileStateStore(tmpdir)
            store.set_json(
                TOKEN_STATE_KEY,
                TokenData('revoked-acc', 'ref-1', expires_at=time.time() + 10000).to_dict(),
            )
            fake_http = _FakeHttpClient([
                _FakeResponse(401, {'error': 'unauthorized'}),
                _FakeResponse(200, {
                    'access_token': 'new-acc',
                    'refresh_token': 'ref-2',
                    'expires_in': 28800,
                }),
                _FakeResponse(200, {'response': {'solar_power': 3500, 'load_power': 900}}),
            ])
            with TeslaFleetClient('cid', store, http_client=fake_http) as client:
                live = client.get_live_status('12345')
            self.assertEqual(live['solar_power'], 3500)
            self.assertEqual(store.get_json(TOKEN_STATE_KEY)['refresh_token'], 'ref-2')

    def test_energy_site_id_cached_across_clients(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalFileStateStore(tmpdir)
            store.set_json(
                TOKEN_STATE_KEY,
                TokenData('acc-1', 'ref-1', expires_at=time.time() + 10000).to_dict(),
            )
            cache = MemoryCache()
            fake_http = _FakeHttpClient([
                _FakeResponse(200, {'response': [{'energy_site_id': 2252243631420713, 'resource_type': 'battery'}]}),
            ])
            with TeslaFleetClient('cid', store, http_client=fake_http, cache=cache) as c1:
                self.assertEqual(c1.get_energy_site_id(), '2252243631420713')
            # Second client hits cache without making another HTTP call
            with TeslaFleetClient('cid', store, http_client=_FakeHttpClient([]), cache=cache) as c2:
                self.assertEqual(c2.get_energy_site_id(), '2252243631420713')
            self.assertEqual(len(fake_http.calls), 1)

    def test_oauth_helpers(self):
        url = build_authorization_url(
            client_id='cid',
            redirect_uri='https://hilo.steren.fr/oauth/callback',
            state='s123',
        )
        self.assertIn('energy_device_data', url)
        fake_http = _FakeHttpClient([
            _FakeResponse(200, {'access_token': 'p-tok'}),
            _FakeResponse(200, {'response': {'domain': 'hilo.steren.fr'}}),
            _FakeResponse(200, {'access_token': 'u-acc', 'refresh_token': 'u-ref', 'expires_in': 28800}),
            _FakeResponse(200, {'response': [{'energy_site_id': 999111, 'resource_type': 'battery'}]}),
        ])
        reg = register_partner_domain(
            fake_http, 'cid', 'csec', 'hilo.steren.fr', 'https://fleet-api.prd.na.vn.cloud.tesla.com'
        )
        self.assertEqual(reg['response']['domain'], 'hilo.steren.fr')
        toks = exchange_code_for_tokens(
            fake_http,
            'cid',
            'csec',
            'NA_1',
            'https://hilo.steren.fr/oauth/callback',
            'https://fleet-api.prd.na.vn.cloud.tesla.com',
        )
        self.assertEqual(toks['refresh_token'], 'u-ref')
        self.assertEqual(
            discover_energy_site_id(fake_http, toks['access_token'], 'https://fleet-api.prd.na.vn.cloud.tesla.com'),
            '999111',
        )

    def test_scheduled_automation_pool_control_and_stale_skip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalFileStateStore(tmpdir)
            cfg = AppConfig()
            controller = Controller(cfg, store)

            pump = types.SimpleNamespace(
                label='Pool Pump', state='0', is_on=False, turn_on=AsyncMock(), turn_off=AsyncMock()
            )
            cleaner = types.SimpleNamespace(
                label='Cleaner', state='0', is_on=False, turn_on=AsyncMock(), turn_off=AsyncMock()
            )
            with patch.object(controller, 'get_forced_operation_mode', return_value='flexible'):
                asyncio.run(
                    controller.update_pool({'pool_pump': pump, 'aux_1': cleaner}, 2600, 85.0)
                )
            pump.turn_on.assert_awaited_once()
            cleaner.turn_on.assert_not_awaited()

            cache = MemoryCache()
            cache.set(DASHBOARD_SNAPSHOT_CACHE_KEY, {'snapshot': {}, 'cached_at': time.time()}, ttl=60, tags=['dashboard-status'])

            mock_ctrl = MagicMock()
            mock_ctrl.control_home = AsyncMock(
                return_value={'excess_power': 2400, 'battery_percentage': 90.0, 'actions': ['Turned Pool Pump ON']}
            )
            res = asyncio.run(
                run_scheduled_automation(cfg, store, execution_id='exec-2', controller=mock_ctrl, cache=cache)
            )
            self.assertEqual(res['status'], 'succeeded')
            self.assertEqual(res['execution_id'], 'exec-2')
            # Switching a device invalidates the cached dashboard snapshot
            self.assertIsNone(cache.get(DASHBOARD_SNAPSHOT_CACHE_KEY))

            # Redelivered executionId is skipped idempotently via cache
            dup = asyncio.run(
                run_scheduled_automation(cfg, store, execution_id='exec-2', controller=mock_ctrl, cache=cache)
            )
            self.assertEqual(dup['status'], 'skipped')
            self.assertEqual(dup['reason'], 'duplicate_execution')
            mock_ctrl.control_home.assert_awaited_once()

            # Stale occurrence (> 15 min old) is skipped
            stale_ts = (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)
            ).isoformat()
            stale = asyncio.run(
                run_scheduled_automation(
                    cfg,
                    store,
                    execution_id='exec-3',
                    scheduled_at=stale_ts,
                    controller=mock_ctrl,
                    cache=cache,
                )
            )
            self.assertEqual(stale['reason'], 'stale_occurrence')

    def test_vercel_schedules_client_uses_official_sdk(self):
        calls = []
        session_options_seen = []

        class _FakeSession:
            def __init__(self, *, service_options):
                session_options_seen.append(service_options)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        class _FakeOptions:
            def __init__(self, *, credentials_factory):
                self.credentials_factory = credentials_factory

        class _FakeSchedulesSdk:
            SchedulesServiceOptions = _FakeOptions

            @staticmethod
            def static_credentials_factory(token):
                return lambda: token

            @staticmethod
            def get_schedule(name, **kwargs):
                calls.append(('get', name, kwargs))
                return types.SimpleNamespace(
                    name=name,
                    is_active=True,
                    expression='*/15 * * * *',
                    timezone='America/Los_Angeles',
                )

            @staticmethod
            def disable_schedule(name, **kwargs):
                calls.append(('disable', name, kwargs))
                return types.SimpleNamespace(
                    name=name,
                    is_active=False,
                    expression='*/15 * * * *',
                    timezone='America/Los_Angeles',
                )

            @staticmethod
            def enable_schedule(name, **kwargs):
                calls.append(('enable', name, kwargs))
                return types.SimpleNamespace(
                    name=name,
                    is_active=True,
                    expression='*/15 * * * *',
                    timezone='America/Los_Angeles',
                )

        client = VercelSchedulesClient(
            schedule_name='pool-on-solar.automation',
            oidc_token='oidc-jwt-123',
            schedules_sdk=_FakeSchedulesSdk,
            session_factory=_FakeSession,
        )
        health = client.get_health()
        self.assertEqual(health['scheduler']['state'], 'ENABLED')
        self.assertEqual(health['scheduler']['schedule'], '*/15 * * * *')

        disabled = client.disable()
        self.assertEqual(disabled['state'], 'PAUSED')
        enabled = client.enable()
        self.assertEqual(enabled['state'], 'ENABLED')

        self.assertEqual([c[0] for c in calls], ['get', 'disable', 'enable'])
        self.assertEqual(len(session_options_seen), 3)
        self.assertEqual(session_options_seen[0][0].credentials_factory(), 'oidc-jwt-123')

    def test_daily_analysis_inserts_into_bigquery_and_wif_exchange(self):
        fake_sts_http = _FakeHttpClient([
            _FakeResponse(200, {'access_token': 'fed-tok'}),
            _FakeResponse(200, {'accessToken': 'gcp-bq-tok'}),
        ])
        bq_token = exchange_vercel_oidc_for_gcp_access_token(
            oidc_token='vercel-jwt',
            project_number='794799107782',
            pool_id='vercel-pool',
            provider_id='vercel-provider',
            service_account_email='bq-writer@pool-on-solar.iam.gserviceaccount.com',
            http_client=fake_sts_http,
        )
        self.assertEqual(bq_token, 'gcp-bq-tok')

        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalFileStateStore(tmpdir)
            store.set_json(
                TOKEN_STATE_KEY,
                TokenData('acc', 'ref', expires_at=time.time() + 10000).to_dict(),
            )
            cfg = AppConfig()
            cfg.tesla.client_id = 'cid'
            cfg.tesla.energy_site_id = '777'

            fake_http = _FakeHttpClient([
                _FakeResponse(200, {'response': {'time_series': [{'solar_power': 1000}]}}),
                _FakeResponse(200, {'response': {'time_series': [{'solar_energy_exported': 5000}]}}),
            ])
            fake_bq = MagicMock()
            fake_bq.insert_rows_json.return_value = []

            cache = MemoryCache()
            with (
                patch('pool_on_solar.tesla_fleet.build_httpx_client', return_value=fake_http),
                patch.dict(
                    os.environ,
                    {
                        'POWER_TABLE_ID': 'pool-on-solar.hilo_tesla.power',
                        'ENERGY_TABLE_ID': 'pool-on-solar.hilo_tesla.energy',
                    },
                ),
            ):
                res = run_daily_analysis(cfg, store, execution_id='daily-1', bq_client=fake_bq, cache=cache)
                dup = run_daily_analysis(cfg, store, execution_id='daily-1', bq_client=fake_bq, cache=cache)

            self.assertEqual(res['status'], 'succeeded')
            self.assertEqual(res['power_points'], 1)
            self.assertEqual(res['energy_points'], 1)
            self.assertEqual(dup['status'], 'skipped')
            self.assertEqual(dup['reason'], 'duplicate_execution')
            self.assertEqual(fake_bq.insert_rows_json.call_count, 2)
            fake_bq.insert_rows_json.assert_any_call(
                'pool-on-solar.hilo_tesla.power',
                [{'solar_power': 1000}],
                ignore_unknown_values=True,
            )
            fake_bq.insert_rows_json.assert_any_call(
                'pool-on-solar.hilo_tesla.energy',
                [{'solar_energy_exported': 5000}],
                ignore_unknown_values=True,
            )


class VercelWebAndJobFunctionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web_mod = _load_module('vercel_api_web', REPO_ROOT / 'api' / 'web.py')
        cls.jobs_mod = _load_module('vercel_jobs', REPO_ROOT / 'jobs.py')

    def test_web_function_dashboard_schedule_toggle_public_key_and_oauth(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalFileStateStore(tmpdir)
            cfg = AppConfig()
            cfg.tesla.client_id = 'cid-1'
            cfg.tesla.client_secret = 'csec-1'

            def fake_tesla(_cfg, _store):
                return (
                    {
                        'status': 'ok',
                        'solar_power': 300,
                        'load_power': 4200,
                        'excess_power': -3900,
                        'battery_power': 0,
                        'grid_power': 3900,
                        'grid_status': 'Active',
                        'percentage_charged': 97.0,
                        'battery_count': 2,
                    },
                    {'status': 'ok', 'expires_in_seconds': 7200},
                )

            def fake_iaq(_cfg):
                return {
                    'status': 'ok',
                    'system_name': 'Pool',
                    'pool': [{'key': 'pool_pump', 'label': 'Filter Pump', 'found': True, 'is_on': True}],
                    'lights': [{'key': 'aux_B1', 'label': 'Light Back', 'found': True, 'is_on': False}],
                    'temperatures': {'pool_temp': '78'},
                }

            sched_state = {'state': 'ENABLED'}
            toggled = []

            def fake_job_health(_cfg, _oidc):
                return {
                    'status': 'ok',
                    'job': {'name': 'pool-on-solar', 'status': 'succeeded', 'completed_at': '2026-11-23T05:00:00Z'},
                    'scheduler': {'name': 'pool-on-solar', 'state': sched_state['state'], 'schedule': '*/15 * * * *'},
                }

            def fake_toggle(_cfg, action, oidc):
                toggled.append((action, oidc))
                sched_state['state'] = 'PAUSED' if action == 'pause' else 'ENABLED'
                return {'status': 'disabled' if action == 'pause' else 'active'}

            shared_cache = MemoryCache()
            collector = StatusCollector(
                cfg,
                store,
                cache=shared_cache,
                tesla_fetcher=fake_tesla,
                iaqualink_fetcher=fake_iaq,
                job_health_fetcher=fake_job_health,
                scheduler_toggler=fake_toggle,
            )
            fake_http = _FakeHttpClient([
                _FakeResponse(200, {'access_token': 'partner-tok'}),
                _FakeResponse(200, {'response': {'domain': 'hilo.steren.fr'}}),
                _FakeResponse(200, {
                    'access_token': 'secret-acc',
                    'refresh_token': 'secret-ref',
                    'expires_in': 28800,
                    'token_type': 'Bearer',
                }),
                _FakeResponse(200, {'response': [{'energy_site_id': 2252243631420713, 'resource_type': 'battery'}]}),
            ])
            pem = '-----BEGIN PUBLIC KEY-----\nTESTKEY\n-----END PUBLIC KEY-----\n'

            handler_cls = self.web_mod.make_web_handler(
                config=cfg,
                state_store=store,
                collector=collector,
                cache=shared_cache,
                http_client_factory=lambda: fake_http,
                public_key_loader=lambda: pem,
            )
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                conn = HTTPConnection('127.0.0.1', port)

                # Public key stays accessible even with DASHBOARD_ACCESS_TOKEN set
                with patch.dict(os.environ, {'DASHBOARD_ACCESS_TOKEN': 'my-gate', 'OAUTH_ENABLED': 'true'}):
                    conn.request('GET', '/.well-known/appspecific/com.tesla.3p.public-key.pem')
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.read().decode('utf-8'), pem)

                    # Unauthenticated dashboard returns 401
                    conn.request('GET', '/')
                    resp = conn.getresponse()
                    resp.read()
                    self.assertEqual(resp.status, 401)

                    # ?token=... sets session cookie and redirects
                    conn.request('GET', '/?token=my-gate')
                    resp = conn.getresponse()
                    resp.read()
                    self.assertEqual(resp.status, 303)
                    self.assertIn('pool_dashboard_session=my-gate', resp.getheader('Set-Cookie'))

                    cookie_hdr = {'Cookie': 'pool_dashboard_session=my-gate'}

                    # Dashboard renders Tesla Energy layout & plain-English schedule
                    conn.request('GET', '/', headers=cookie_hdr)
                    resp = conn.getresponse()
                    html = resp.read().decode('utf-8')
                    self.assertEqual(resp.status, 200)
                    self.assertIn('SOLAR', html)
                    self.assertIn('0.3 kW', html)
                    self.assertIn('HOME', html)
                    self.assertIn('4.2 kW', html)
                    self.assertIn('POWERWALL · 2x', html)
                    self.assertIn('0 kW · 97%', html)
                    self.assertIn('GRID', html)
                    self.assertIn('3.9 kW', html)
                    self.assertIn('Runs every 15 minutes', html)
                    self.assertIn('/scheduler/pause', html)

                    # Pause schedule invokes Vercel Schedules toggle with forwarded x-vercel-oidc-token
                    conn.request(
                        'POST',
                        '/scheduler/pause',
                        headers={
                            **cookie_hdr,
                            'Origin': f'http://127.0.0.1:{port}',
                            'x-vercel-oidc-token': 'req-oidc-tok',
                        },
                    )
                    resp = conn.getresponse()
                    resp.read()
                    self.assertEqual(resp.status, 303)
                    self.assertEqual(toggled, [('pause', 'req-oidc-tok')])

                    # OAuth partner registration + login + callback saves tokens into StateStore
                    conn.request(
                        'POST',
                        '/oauth/register',
                        headers={
                            **cookie_hdr,
                            'X-Forwarded-Host': 'hilo.steren.fr',
                            'X-Forwarded-Proto': 'https',
                            'Origin': 'https://hilo.steren.fr',
                        },
                    )
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    self.assertIn('hilo.steren.fr', resp.read().decode('utf-8'))

                    conn.request(
                        'GET',
                        '/oauth/login',
                        headers={
                            **cookie_hdr,
                            'X-Forwarded-Host': 'hilo.steren.fr',
                            'X-Forwarded-Proto': 'https',
                        },
                    )
                    resp = conn.getresponse()
                    resp.read()
                    self.assertEqual(resp.status, 302)
                    state = parse_qs(urlparse(resp.getheader('Location')).query)['state'][0]

                    conn.request(
                        'GET',
                        f'/oauth/callback?code=NA_ok&state={state}',
                        headers={
                            'Cookie': f'pool_dashboard_session=my-gate; tesla_oauth_state={state}',
                            'X-Forwarded-Host': 'hilo.steren.fr',
                            'X-Forwarded-Proto': 'https',
                        },
                    )
                    resp = conn.getresponse()
                    body = resp.read().decode('utf-8')
                    self.assertEqual(resp.status, 200)
                    self.assertIn('2252243631420713', body)
                    self.assertNotIn('secret-acc', body)
                    self.assertEqual(store.get_json(TOKEN_STATE_KEY)['refresh_token'], 'secret-ref')

                conn.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_code_declared_schedule_jobs(self):
        import vercel.schedules as vs

        registered = getattr(vs, '_registered', None)
        if registered is not None:
            names = [meta['name'] for meta, _ in registered]
            self.assertIn('automation', names)
            self.assertIn('analysis', names)

        meta = vs.ScheduleMetadata(
            schedule_id='sched-1',
            execution_id='exec-1',
            name='pool-on-solar.automation',
            namespace='default',
            scheduled_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30),
            source='static',
            expression='*/15 * * * *',
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalFileStateStore(tmpdir)
            cache = MemoryCache()
            with (
                patch.object(self.jobs_mod, 'create_state_store', return_value=store),
                patch.object(self.jobs_mod, 'create_app_cache', return_value=cache),
                patch.object(self.jobs_mod, 'load_config', return_value=AppConfig()),
                patch.object(
                    self.jobs_mod,
                    'run_scheduled_automation',
                    new=AsyncMock(return_value={'status': 'skipped', 'reason': 'stale_occurrence'}),
                ) as mock_run,
            ):
                asyncio.run(self.jobs_mod.automation_job(meta))
                mock_run.assert_awaited_once()
                self.assertEqual(mock_run.call_args.kwargs['execution_id'], 'exec-1')


if __name__ == '__main__':
    unittest.main()
