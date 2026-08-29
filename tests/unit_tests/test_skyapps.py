"""Tests for the SkyApps API server plugin."""
from unittest import mock

import fastapi
from fastapi.testclient import TestClient
import pytest

from skyapps.plugin import SkyAppsPlugin


def _plugin(**kwargs):
    params = dict(base_domain='apps.example.com', scheme='https')
    params.update(kwargs)
    return SkyAppsPlugin(**params)


def _record(name='dashboard', workspace='eng', port=30003, endpoint=None):
    rec = {
        'name': name,
        'workspace': workspace,
        'load_balancer_port': port,
    }
    if endpoint is not None:
        rec['endpoint'] = endpoint
    return rec


class TestHostname:

    def test_name_and_workspace(self):
        assert _plugin().hostname(_record()) == 'dashboard-eng.apps.example.com'

    def test_missing_workspace_defaults(self):
        rec = _record()
        del rec['workspace']
        assert _plugin().hostname(rec) == 'dashboard-default.apps.example.com'

    def test_rejects_invalid_label(self):
        with pytest.raises(ValueError, match='DNS-1123'):
            _plugin().hostname(_record(name='My_App'))


class TestBackendUrl:

    def test_uses_backend_host_when_set(self):
        p = _plugin(backend_host='controller.svc', backend_scheme='http')
        assert p.backend_url(_record()) == 'http://controller.svc:30003'

    def test_parses_host_from_endpoint(self):
        p = _plugin()
        rec = _record(endpoint='http://10.0.3.4:30003')
        assert p.backend_url(rec) == 'http://10.0.3.4:30003'

    def test_skips_when_no_port(self):
        rec = _record()
        rec['load_balancer_port'] = None
        assert _plugin().backend_url(rec) is None


class TestRender:

    def test_router_and_service(self):
        p = _plugin(backend_host='controller.svc')
        cfg = p.render([_record()])
        assert cfg['http']['routers']['dashboard-eng']['rule'] == (
            'Host(`dashboard-eng.apps.example.com`)')
        servers = cfg['http']['services']['dashboard-eng']['loadBalancer'][
            'servers']
        assert servers[0]['url'] == 'http://controller.svc:30003'

    def test_skips_invalid_and_keeps_valid(self):
        p = _plugin(backend_host='controller.svc')
        cfg = p.render([_record(name='My_App'), _record()])
        assert 'dashboard-eng' in cfg['http']['routers']
        assert len(cfg['http']['routers']) == 1


class TestTraefikRoute:

    def _client(self, plugin):
        app = fastapi.FastAPI()
        app.include_router(plugin.build_router())
        return TestClient(app)

    def test_serves_config(self):
        p = _plugin(backend_host='controller.svc')
        p._service_status_records = lambda: [_record()]
        resp = self._client(p).get('/plugins/skyapps/traefik')
        assert resp.status_code == 200
        assert resp.json()['http']['routers']

    def test_empty_without_last_good_is_503(self):
        p = _plugin(backend_host='controller.svc')
        p._service_status_records = lambda: []
        resp = self._client(p).get('/plugins/skyapps/traefik')
        assert resp.status_code == 503

    def test_failed_read_returns_last_good(self):
        p = _plugin(backend_host='controller.svc')
        p._service_status_records = lambda: [_record()]
        client = self._client(p)
        assert client.get('/plugins/skyapps/traefik').status_code == 200

        def boom():
            raise RuntimeError('controller down')

        p._service_status_records = boom
        resp = client.get('/plugins/skyapps/traefik')
        assert resp.status_code == 200
        assert 'dashboard-eng' in resp.json()['http']['routers']

    def test_empty_after_good_keeps_last_good(self):
        p = _plugin(backend_host='controller.svc')
        p._service_status_records = lambda: [_record()]
        client = self._client(p)
        client.get('/plugins/skyapps/traefik')
        p._service_status_records = lambda: []
        resp = client.get('/plugins/skyapps/traefik')
        assert resp.status_code == 200
        assert resp.json()['http']['routers']


class TestEndpointPatch:

    def test_rewrites_endpoint(self):
        p = _plugin()
        records = [_record()]
        original = mock.Mock(return_value=records)
        with mock.patch('sky.serve.server.impl.status', original):
            import sky.serve.server.impl as impl
            p._patch_endpoint_reporting()
            out = impl.status()
        assert out[0]['endpoint'] == 'https://dashboard-eng.apps.example.com'

    def test_fail_open_on_bad_name(self):
        p = _plugin()
        records = [_record(name='My_App')]
        original = mock.Mock(return_value=records)
        with mock.patch('sky.serve.server.impl.status', original):
            import sky.serve.server.impl as impl
            p._patch_endpoint_reporting()
            out = impl.status()
        assert out[0].get('endpoint') is None
