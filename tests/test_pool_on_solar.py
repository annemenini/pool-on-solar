"""Unit tests for Tesla Fleet API auth, Secret Manager token rotation, and automation logic."""

from __future__ import annotations

import asyncio
from http.client import HTTPConnection
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTOMATION_DIR = REPO_ROOT / 'automation'
AUTH_DIR = REPO_ROOT / 'tesla' / 'authentication'
ANALYSIS_DIR = REPO_ROOT / 'tesla' / 'analysis'


def _install_stub_httpx() -> None:
    """Provides a minimal httpx stub if httpx is not installed in the local test environment."""
    if 'httpx' in sys.modules:
        return
    try:
        import httpx  # noqa: F401
        return
    except ImportError:
        pass

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


class TeslaFleetModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stub_httpx()
        cls.tf = _load_module('tesla_fleet', AUTOMATION_DIR / 'tesla_fleet.py')
        cls.auth_mod = _load_module('tesla_auth_main', AUTH_DIR / 'main.py')

    def test_automation_and_analysis_fleet_modules_are_in_sync(self):
        auto_src = (AUTOMATION_DIR / 'tesla_fleet.py').read_text(encoding='utf-8')
        analysis_src = (ANALYSIS_DIR / 'tesla_fleet.py').read_text(encoding='utf-8')
        self.assertEqual(auto_src, analysis_src)

    def test_token_data_expiry_and_rotation(self):
        now = 1_700_000_000.0
        token = self.tf.TokenData(
            access_token='acc-1',
            refresh_token='ref-1',
            expires_at=now + 600,
        )
        self.assertFalse(token.is_expired(buffer_seconds=300, now=now))
        self.assertTrue(token.is_expired(buffer_seconds=300, now=now + 301))

        rotated = self.tf.TokenData.from_oauth_response(
            {'access_token': 'acc-2', 'refresh_token': 'ref-2', 'expires_in': 28800},
            fallback_refresh_token='ref-1',
            now=now,
        )
        self.assertEqual(rotated.access_token, 'acc-2')
        self.assertEqual(rotated.refresh_token, 'ref-2')
        self.assertEqual(rotated.expires_at, now + 28800)

        kept_fallback = self.tf.TokenData.from_oauth_response(
            {'access_token': 'acc-3', 'expires_in': 3600},
            fallback_refresh_token='ref-2',
            now=now,
        )
        self.assertEqual(kept_fallback.refresh_token, 'ref-2')

    def test_local_file_token_store_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'nested' / 'cache.json'
            store = self.tf.LocalFileTokenStore(str(path))
            token = self.tf.TokenData('acc-1', 'ref-1', 12345.0)
            store.save(token)
            loaded = store.load()
            self.assertEqual(loaded, token)
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_secret_manager_token_store_rotates_and_destroys_all_older_versions(self):
        sm_client = MagicMock()
        initial_payload = json.dumps({
            'access_token': 'old-acc',
            'refresh_token': 'old-ref',
            'expires_at': 100.0,
            'token_type': 'Bearer',
        }).encode('utf-8')
        sm_client.access_secret_version.return_value = types.SimpleNamespace(
            name='projects/my-proj/secrets/tesla-token/versions/2',
            payload=types.SimpleNamespace(data=initial_payload),
        )
        sm_client.add_secret_version.return_value = types.SimpleNamespace(
            name='projects/my-proj/secrets/tesla-token/versions/3',
        )
        sm_client.list_secret_versions.return_value = [
            types.SimpleNamespace(name='projects/my-proj/secrets/tesla-token/versions/1'),
            types.SimpleNamespace(name='projects/my-proj/secrets/tesla-token/versions/2'),
            types.SimpleNamespace(name='projects/my-proj/secrets/tesla-token/versions/3'),
        ]

        store = self.tf.SecretManagerTokenStore(
            'projects/my-proj/secrets/tesla-token',
            client=sm_client,
        )
        loaded = store.load()
        self.assertEqual(loaded.refresh_token, 'old-ref')

        new_token = self.tf.TokenData('new-acc', 'new-ref', 99999.0)
        store.save(new_token)

        sm_client.add_secret_version.assert_called_once()
        sm_client.list_secret_versions.assert_called_once_with(
            request={
                'parent': 'projects/my-proj/secrets/tesla-token',
                'filter': 'state:ENABLED OR state:DISABLED',
            }
        )
        destroyed = [
            call.kwargs['request']['name']
            for call in sm_client.destroy_secret_version.call_args_list
        ]
        self.assertEqual(
            destroyed,
            [
                'projects/my-proj/secrets/tesla-token/versions/1',
                'projects/my-proj/secrets/tesla-token/versions/2',
            ],
        )

    def test_secret_manager_destroy_failure_is_non_fatal(self):
        sm_client = MagicMock()
        sm_client.access_secret_version.return_value = types.SimpleNamespace(
            name='projects/my-proj/secrets/tesla-token/versions/1',
            payload=types.SimpleNamespace(
                data=json.dumps({'access_token': 'a', 'refresh_token': 'r', 'expires_at': 0}).encode()
            ),
        )
        sm_client.add_secret_version.return_value = types.SimpleNamespace(
            name='projects/my-proj/secrets/tesla-token/versions/2',
        )
        sm_client.list_secret_versions.side_effect = PermissionError('no list role')
        sm_client.destroy_secret_version.side_effect = PermissionError('no destroy role')

        store = self.tf.SecretManagerTokenStore(
            'projects/my-proj/secrets/tesla-token',
            client=sm_client,
        )
        store.load()
        # Should fall back to previous loaded version and not raise despite destroy failing
        store.save(self.tf.TokenData('a2', 'r2', 5000.0))
        sm_client.add_secret_version.assert_called_once()
        sm_client.destroy_secret_version.assert_called_once_with(
            request={'name': 'projects/my-proj/secrets/tesla-token/versions/1'}
        )

    def test_auth_cli_secret_manager_destroys_older_versions(self):
        sm_client = MagicMock()
        sm_client.add_secret_version.return_value = types.SimpleNamespace(
            name='projects/my-proj/secrets/tesla-token/versions/2',
        )
        sm_client.list_secret_versions.return_value = [
            types.SimpleNamespace(name='projects/my-proj/secrets/tesla-token/versions/1'),
            types.SimpleNamespace(name='projects/my-proj/secrets/tesla-token/versions/2'),
        ]
        saved_name = self.auth_mod.save_tokens_to_secret_manager(
            {'access_token': 'a', 'refresh_token': 'r', 'expires_at': 100.0},
            'projects/my-proj/secrets/tesla-token',
            client=sm_client,
        )
        self.assertEqual(saved_name, 'projects/my-proj/secrets/tesla-token/versions/2')
        sm_client.destroy_secret_version.assert_called_once_with(
            request={'name': 'projects/my-proj/secrets/tesla-token/versions/1'}
        )

    def test_normalize_secret_name(self):
        self.assertEqual(
            self.tf.normalize_secret_name('projects/p1/secrets/s1/versions/latest'),
            'projects/p1/secrets/s1',
        )
        with patch.dict(os.environ, {'GOOGLE_CLOUD_PROJECT': 'p2'}):
            self.assertEqual(
                self.tf.normalize_secret_name('my-secret'),
                'projects/p2/secrets/my-secret',
            )

    def test_fleet_client_refreshes_expired_token_and_discovers_site(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / 'cache.json'
            store = self.tf.LocalFileTokenStore(str(cache_path))
            store.save(self.tf.TokenData('expired-acc', 'ref-1', expires_at=1.0))

            fake_http = _FakeHttpClient([
                _FakeResponse(200, {
                    'access_token': 'fresh-acc',
                    'refresh_token': 'ref-2',
                    'expires_in': 28800,
                    'token_type': 'Bearer',
                }),
                _FakeResponse(200, {
                    'response': [
                        {'vin': '5YJ3E1EA000000'},
                        {'energy_site_id': 987654321, 'resource_type': 'battery'},
                    ]
                }),
                _FakeResponse(200, {
                    'response': {
                        'solar_power': 5200,
                        'load_power': 1800,
                        'percentage_charged': 82.5,
                    }
                }),
            ])

            with self.tf.TeslaFleetClient(
                client_id='client-123',
                token_store=store,
                http_client=fake_http,
            ) as client:
                site_id = client.get_energy_site_id()
                live = client.get_live_status(site_id)

            self.assertEqual(site_id, '987654321')
            self.assertEqual(live['solar_power'], 5200)

            persisted = store.load()
            self.assertEqual(persisted.access_token, 'fresh-acc')
            self.assertEqual(persisted.refresh_token, 'ref-2')
            self.assertEqual(fake_http.calls[0][0], 'POST')
            self.assertEqual(
                fake_http.calls[0][2]['data'],
                {
                    'grant_type': 'refresh_token',
                    'client_id': 'client-123',
                    'refresh_token': 'ref-1',
                },
            )

    def test_fleet_client_retries_once_on_401(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self.tf.LocalFileTokenStore(str(Path(tmpdir) / 'cache.json'))
            store.save(self.tf.TokenData('revoked-acc', 'ref-1', expires_at=time.time() + 10000))

            fake_http = _FakeHttpClient([
                _FakeResponse(401, {'error': 'unauthorized'}),
                _FakeResponse(200, {
                    'access_token': 'new-acc',
                    'refresh_token': 'ref-2',
                    'expires_in': 28800,
                }),
                _FakeResponse(200, {'response': {'solar_power': 3000, 'load_power': 500}}),
            ])

            client = self.tf.TeslaFleetClient('cid', store, http_client=fake_http)
            live = client.get_live_status('111')
            self.assertEqual(live['solar_power'], 3000)
            self.assertEqual(store.load().refresh_token, 'ref-2')

    def test_auth_cli_helpers(self):
        url = self.auth_mod.build_authorization_url(
            client_id='cid',
            redirect_uri='https://example.com/cb',
            scopes='openid offline_access energy_device_data',
            state='abc123',
        )
        self.assertIn('client_id=cid', url)
        self.assertIn('energy_device_data', url)
        self.assertIn('state=abc123', url)

        code = self.auth_mod.extract_code_from_input(
            'https://example.com/cb?code=NA_xyz&state=abc123',
            expected_state='abc123',
        )
        self.assertEqual(code, 'NA_xyz')
        self.assertEqual(self.auth_mod.extract_code_from_input('NA_bare_code'), 'NA_bare_code')

        with self.assertRaises(ValueError):
            self.auth_mod.extract_code_from_input(
                'https://example.com/cb?code=NA_xyz&state=wrong',
                expected_state='abc123',
            )

        fake_http = _FakeHttpClient([
            _FakeResponse(200, {'access_token': 'partner-tok'}),
            _FakeResponse(200, {'response': {'domain': 'example.com'}}),
        ])
        reg = self.auth_mod.register_partner_domain(
            http=fake_http,
            client_id='cid',
            client_secret='csec',
            domain='example.com',
            api_base_url='https://fleet-api.prd.na.vn.cloud.tesla.com',
            scopes='openid energy_device_data',
        )
        self.assertEqual(reg['response']['domain'], 'example.com')

        fake_http_exchange = _FakeHttpClient([
            _FakeResponse(200, {
                'access_token': 'user-acc',
                'refresh_token': 'user-ref',
                'expires_in': 28800,
                'token_type': 'Bearer',
            }),
            _FakeResponse(200, {
                'response': [{'energy_site_id': 555123, 'resource_type': 'solar'}]
            }),
        ])
        tokens = self.auth_mod.exchange_code_for_tokens(
            http=fake_http_exchange,
            client_id='cid',
            client_secret='csec',
            code='NA_xyz',
            redirect_uri='https://example.com/cb',
            api_base_url='https://fleet-api.prd.na.vn.cloud.tesla.com',
            scopes='openid offline_access energy_device_data',
        )
        self.assertEqual(tokens['access_token'], 'user-acc')
        self.assertEqual(tokens['refresh_token'], 'user-ref')
        site_id = self.auth_mod.discover_energy_site_id(
            http=fake_http_exchange,
            access_token=tokens['access_token'],
            api_base_url='https://fleet-api.prd.na.vn.cloud.tesla.com',
        )
        self.assertEqual(site_id, '555123')

    def test_proto_compiles_with_protoc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    'protoc',
                    f'-I{AUTOMATION_DIR}',
                    f'--python_out={tmpdir}',
                    str(AUTOMATION_DIR / 'config.proto'),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((Path(tmpdir) / 'config_pb2.py').exists())

    def test_automation_parse_bool_env_and_syntax(self):
        for rel in [
            'automation/tesla_fleet.py',
            'automation/main.py',
            'tesla/authentication/main.py',
            'tesla/authentication/server.py',
            'tesla/analysis/tesla_fleet.py',
            'tesla/analysis/main.py',
        ]:
            source = (REPO_ROOT / rel).read_text(encoding='utf-8')
            compile(source, rel, 'exec')

        # Load automation/main.py with lightweight stubs for optional third-party deps.
        google_mod = types.ModuleType('google')
        protobuf_mod = types.ModuleType('google.protobuf')
        text_format_mod = types.ModuleType('google.protobuf.text_format')
        text_format_mod.Parse = lambda raw, msg: msg
        protobuf_mod.text_format = text_format_mod
        google_mod.protobuf = protobuf_mod

        class _FakeConfig:
            def __init__(self):
                self.tesla = types.SimpleNamespace(
                    user_id='',
                    cache_file='/app/config/cache.json',
                    energy_site_id='',
                    api_base_url='https://fleet-api.prd.na.vn.cloud.tesla.com',
                    token_secret_name='',
                )
                self.iaqualink = types.SimpleNamespace(
                    user_id='',
                    password='',
                    pool=types.SimpleNamespace(
                        devices=['pool_pump', 'aux_1'],
                        activation_excess_power=2000,
                        deactivation_excess_power=500,
                        max_operating_window_start=8,
                        max_operating_window_end=18,
                        min_operating_window_start=-1,
                        min_operating_window_end=-1,
                        minimum_battery=50,
                    ),
                    light=types.SimpleNamespace(
                        devices=['aux_B1'],
                        minimum_battery=50,
                        max_operating_window_end=22,
                    ),
                )
                self.location = types.SimpleNamespace(
                    timezone='UTC',
                    latitude=37.3861,
                    longitude=-122.0839,
                )

        config_pb2_mod = types.ModuleType('config_pb2')
        config_pb2_mod.Config = _FakeConfig

        iaq_mod = types.ModuleType('iaqualink')
        iaq_client_mod = types.ModuleType('iaqualink.client')
        iaq_client_mod.AqualinkClient = MagicMock()
        iaq_dev_mod = types.ModuleType('iaqualink.device')
        iaq_dev_mod.AqualinkDevice = object
        pytz_mod = types.ModuleType('pytz')
        suntime_mod = types.ModuleType('suntime')
        suntime_mod.Sun = MagicMock()

        stub_modules = {
            'google': google_mod,
            'google.protobuf': protobuf_mod,
            'google.protobuf.text_format': text_format_mod,
            'config_pb2': config_pb2_mod,
            'iaqualink': iaq_mod,
            'iaqualink.client': iaq_client_mod,
            'iaqualink.device': iaq_dev_mod,
            'pytz': pytz_mod,
            'suntime': suntime_mod,
        }

        with patch.dict(sys.modules, stub_modules):
            auto_mod = _load_module('automation_main_tested', AUTOMATION_DIR / 'main.py')

            with patch.dict(os.environ, {'DRY_RUN': 'false'}):
                self.assertFalse(auto_mod.parse_bool_env('DRY_RUN'))
            with patch.dict(os.environ, {'DRY_RUN': '0'}):
                self.assertFalse(auto_mod.parse_bool_env('DRY_RUN'))
            with patch.dict(os.environ, {'DRY_RUN': 'true'}):
                self.assertTrue(auto_mod.parse_bool_env('DRY_RUN'))

            with tempfile.TemporaryDirectory() as tmpdir:
                cfg_path = Path(tmpdir) / 'config.pbtxt'
                cfg_path.write_text('', encoding='utf-8')

                # Optional None args should not raise protobuf TypeError
                controller = auto_mod.Controller(
                    config_file=str(cfg_path),
                    tesla_client_id='cid',
                    energy_site_id=None,
                    iaqualink_user_id='u@example.com',
                    iaqualink_password='pw',
                )
                self.assertEqual(controller._config.tesla.energy_site_id, '')

                # Verify pool turns on one device when excess power >= activation threshold
                pump = types.SimpleNamespace(
                    label='Pool Pump',
                    state='0',
                    is_on=False,
                    turn_on=AsyncMock(),
                    turn_off=AsyncMock(),
                )
                cleaner = types.SimpleNamespace(
                    label='Cleaner',
                    state='0',
                    is_on=False,
                    turn_on=AsyncMock(),
                    turn_off=AsyncMock(),
                )
                with patch.object(controller, 'get_forced_operation_mode', return_value='flexible'):
                    asyncio.run(
                        controller.update_pool(
                            {'pool_pump': pump, 'aux_1': cleaner},
                            excess_power=2500,
                            battery_percentage=80.0,
                        )
                    )
                pump.turn_on.assert_awaited_once()
                cleaner.turn_on.assert_not_awaited()


class TeslaOAuthServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_stub_httpx()
        sys.path.insert(0, str(AUTH_DIR))
        cls.server_mod = _load_module('tesla_auth_server', AUTH_DIR / 'server.py')

    def _start_server(self, config, http_client_factory, sm_client=None):
        server = self.server_mod.create_server(
            host='127.0.0.1',
            port=0,
            config=config,
            http_client_factory=http_client_factory,
            secret_manager_client=sm_client,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_public_key_register_login_and_callback_flow(self):
        sm_client = MagicMock()
        sm_client.add_secret_version.return_value = types.SimpleNamespace(
            name='projects/my-proj/secrets/tesla-token/versions/4'
        )
        sm_client.list_secret_versions.return_value = [
            types.SimpleNamespace(name='projects/my-proj/secrets/tesla-token/versions/3'),
            types.SimpleNamespace(name='projects/my-proj/secrets/tesla-token/versions/4'),
        ]

        responses = [
            # /register -> partner token + partner_accounts
            _FakeResponse(200, {'access_token': 'partner-tok'}),
            _FakeResponse(200, {'response': {'domain': 'oauth.example.com'}}),
            # /callback -> token exchange + products discovery
            _FakeResponse(200, {
                'access_token': 'super-secret-access',
                'refresh_token': 'super-secret-refresh',
                'expires_in': 28800,
                'token_type': 'Bearer',
            }),
            _FakeResponse(200, {
                'response': [{'energy_site_id': 777888, 'resource_type': 'battery'}]
            }),
        ]

        class _CtxFakeHttp(_FakeHttpClient):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        fake_http = _CtxFakeHttp(responses)
        pem = '-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEtest\n-----END PUBLIC KEY-----\n'
        cfg = self.server_mod.OAuthServerConfig(
            client_id='cid-1',
            client_secret='csec-1',
            token_secret_name='projects/my-proj/secrets/tesla-token',
            public_key_pem=pem,
            oauth_enabled=True,
        )
        server, _ = self._start_server(cfg, lambda: fake_http, sm_client=sm_client)
        port = server.server_address[1]
        try:
            conn = HTTPConnection('127.0.0.1', port)

            # 1. Public key endpoint
            conn.request('GET', '/.well-known/appspecific/com.tesla.3p.public-key.pem')
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read().decode('utf-8'), pem)

            # 2. Register partner domain (derived from X-Forwarded-Host)
            conn.request(
                'POST',
                '/register',
                headers={'X-Forwarded-Host': 'oauth.example.com', 'X-Forwarded-Proto': 'https'},
            )
            resp = conn.getresponse()
            body = resp.read().decode('utf-8')
            self.assertEqual(resp.status, 200)
            self.assertIn('oauth.example.com', body)

            # 3. Login redirect + state cookie
            conn.request(
                'GET',
                '/login',
                headers={'X-Forwarded-Host': 'oauth.example.com', 'X-Forwarded-Proto': 'https'},
            )
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 302)
            location = resp.getheader('Location')
            set_cookie = resp.getheader('Set-Cookie')
            self.assertIn('https://auth.tesla.com/oauth2/v3/authorize', location)
            self.assertIn('redirect_uri=https%3A%2F%2Foauth.example.com%2Fcallback', location)
            state = parse_qs(urlparse(location).query)['state'][0]
            self.assertIn(f'tesla_oauth_state={state}', set_cookie)

            # 4. Callback with mismatched state fails with 400
            conn.request('GET', '/callback?code=NA_123&state=wrong', headers={'Cookie': f'tesla_oauth_state={state}'})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 400)

            # 5. Callback with matching state succeeds, saves to Secret Manager, destroys old version, hides secrets
            conn.request(
                'GET',
                f'/callback?code=NA_123&state={state}',
                headers={
                    'Cookie': f'tesla_oauth_state={state}',
                    'X-Forwarded-Host': 'oauth.example.com',
                    'X-Forwarded-Proto': 'https',
                },
            )
            resp = conn.getresponse()
            body = resp.read().decode('utf-8')
            self.assertEqual(resp.status, 200)
            self.assertIn('777888', body)
            self.assertIn('versions/4', body)
            self.assertNotIn('super-secret-access', body)
            self.assertNotIn('super-secret-refresh', body)
            sm_client.destroy_secret_version.assert_called_once_with(
                request={'name': 'projects/my-proj/secrets/tesla-token/versions/3'}
            )
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_oauth_disabled_keeps_public_key_serving(self):
        pem = '-----BEGIN PUBLIC KEY-----\nTEST\n-----END PUBLIC KEY-----\n'
        cfg = self.server_mod.OAuthServerConfig(
            client_id='cid',
            client_secret='csec',
            public_key_pem=pem,
            oauth_enabled=False,
        )
        server, _ = self._start_server(cfg, lambda: _FakeHttpClient([]))
        port = server.server_address[1]
        try:
            conn = HTTPConnection('127.0.0.1', port)
            conn.request('GET', '/.well-known/appspecific/com.tesla.3p.public-key.pem')
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read().decode('utf-8'), pem)

            conn.request('GET', '/login')
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 403)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
