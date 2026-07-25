"""Tests for wildcard subdomain routing in Kubernetes ingress mode."""
# pylint: disable=protected-access,invalid-name
import json
import re
from unittest import mock

import pytest

from sky import skypilot_config
from sky.provision.kubernetes import network
from sky.provision.kubernetes import network_utils
from sky.serve import serve_authz
from sky.serve.server import server as serve_server
from sky.server import daemons
from sky.skylet import constants

# A single DNS label: <= 63 chars, [a-z0-9-], no leading/trailing hyphen.
_LABEL_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')

_DOMAIN = 'skyapps.io'


def _label_of(host: str) -> str:
    return host[:-(len(_DOMAIN) + 1)]


class TestGenerateWildcardHost:
    """`generate_wildcard_host` -- §5.1 hostname scheme."""

    def test_basic_shape(self):
        host = network_utils.generate_wildcard_host(
            identity='default|my-service|8080',
            name='my-service-8080',
            wildcard_domain=_DOMAIN)
        assert host.endswith(f'.{_DOMAIN}')
        label = _label_of(host)
        assert _LABEL_RE.fullmatch(label), label
        assert re.fullmatch(r'my-service-8080--[0-9a-f]{8}', label), label

    def test_is_a_single_dns_label(self):
        """A `.` in the name must not create a deeper subdomain level.

        A one-level `*.<domain>` certificate and DNS record only cover a
        single label, so a name that escapes into a second label would produce
        a hostname with no valid certificate -- or, worse, one that shadows an
        existing name under the domain.
        """
        host = network_utils.generate_wildcard_host(identity='ns|evil|1',
                                                    name='api.internal.corp',
                                                    wildcard_domain=_DOMAIN)
        assert host.count('.') == _DOMAIN.count('.') + 1
        assert _LABEL_RE.fullmatch(_label_of(host))

    @pytest.mark.parametrize('name', [
        'UPPER-Case',
        'has spaces',
        'under_scores',
        'sym!@#$%^&*()bols',
        '---leading-and-trailing---',
        '',
        '.',
        '-',
        'a' * 300,
        'ünïcödé',
    ])
    def test_sanitization_always_yields_a_valid_label(self, name):
        host = network_utils.generate_wildcard_host(identity=f'ns|{name}|80',
                                                    name=name,
                                                    wildcard_domain=_DOMAIN)
        label = _label_of(host)
        assert _LABEL_RE.fullmatch(label), label
        assert len(label) <= 63

    @pytest.mark.parametrize('name', ['xn--fiqs8s', 'ab--cd', 'xn--'])
    def test_punycode_lookalikes_are_defused(self, name):
        """Labels with `--` in position 3-4 are reserved by RFC 5891."""
        label = _label_of(
            network_utils.generate_wildcard_host(identity='ns|x|80',
                                                 name=name,
                                                 wildcard_domain=_DOMAIN))
        assert not label.startswith('xn--')
        assert label[2:4] != '--'

    @pytest.mark.parametrize('name', sorted(network_utils.RESERVED_HOST_LABELS))
    def test_reserved_names_cannot_be_shadowed(self, name):
        host = network_utils.generate_wildcard_host(identity=f'ns|{name}|80',
                                                    name=name,
                                                    wildcard_domain=_DOMAIN)
        label = _label_of(host)
        assert label not in network_utils.RESERVED_HOST_LABELS
        assert host != f'{name}.{_DOMAIN}'

    def test_idempotent(self):
        args = dict(identity='ns|c|80', name='c-80', wildcard_domain=_DOMAIN)
        assert (network_utils.generate_wildcard_host(
            **args) == network_utils.generate_wildcard_host(**args))

    def test_truncation_cannot_collide_distinct_identities(self):
        """The hash is taken over the untruncated identity.

        Two clusters whose names share a long prefix truncate to the same
        readable portion; if the hash were taken over the truncated name they
        would land on the same hostname, and one service would answer for the
        other.
        """
        shared = 'a' * 60
        hosts = {
            network_utils.generate_wildcard_host(
                identity=f'default|{shared}{suffix}|8080',
                name=f'{shared}{suffix}-8080',
                wildcard_domain=_DOMAIN) for suffix in ('-one', '-two')
        }
        assert len(hosts) == 2

    def test_distinct_identities_do_not_collide(self):
        """Property check over a large sweep of realistic identities."""
        hosts = set()
        identities = set()
        for namespace in ('default', 'team-a', 'team-b'):
            for cluster in ('svc', 'svc-2', 'x' * 70, 'x' * 70 + 'y'):
                for port in (80, 8080, 30001, 30002):
                    identity = f'{namespace}|{cluster}|{port}'
                    identities.add(identity)
                    hosts.add(
                        network_utils.generate_wildcard_host(
                            identity=identity,
                            name=f'{cluster}-{port}',
                            wildcard_domain=_DOMAIN))
        assert len(hosts) == len(identities)


class TestRegistrableDomain:
    """`get_registrable_domain` -- the cookie-scope comparison in §4.1."""

    @pytest.mark.parametrize('host,expected', [
        ('sky.example.com', 'example.com'),
        ('example.com', 'example.com'),
        ('a.b.c.example.com', 'example.com'),
        ('sky.example.co.uk', 'example.co.uk'),
        ('example.co.uk', 'example.co.uk'),
        ('skyapps.io', 'skyapps.io'),
    ])
    def test_registrable_domain(self, host, expected):
        assert network_utils.get_registrable_domain(host) == expected


_API_SERVER = 'https://skypilot.example.com'
_DOMAIN_UNDER_API = 'skypilot.example.com'


def _config(_api_server_endpoint=_API_SERVER, **ingress):
    """Patches the loaded SkyPilot config with a `kubernetes.ingress` block."""

    def fake_get_nested(keys, default_value=None, **kwargs):
        del kwargs
        if tuple(keys) == ('kubernetes', 'ingress'):
            return ingress
        if tuple(keys) == ('api_server', 'endpoint'):
            return _api_server_endpoint or default_value
        return default_value

    return mock.patch('sky.skypilot_config.get_nested',
                      side_effect=fake_get_nested)


class TestWildcardIngressConfig:
    """`get_wildcard_ingress_config` -- validation of the admin config."""

    def test_disabled_by_default(self):
        with mock.patch('sky.skypilot_config.get_nested', return_value=None):
            assert network_utils.get_wildcard_ingress_config() is None

    def test_absent_wildcard_domain_is_disabled(self):
        with _config():
            assert network_utils.get_wildcard_ingress_config() is None

    def test_minimal_enabled_config(self):
        with _config(wildcard_domain=_DOMAIN_UNDER_API):
            config = network_utils.get_wildcard_ingress_config()
        assert config is not None
        assert config.wildcard_domain == _DOMAIN_UNDER_API
        assert config.tls_mode == network_utils.WildcardIngressTLSMode.NONE

    @pytest.mark.parametrize('domain', [
        'not a domain',
        'nodots',
        '-leading.example.com',
        'trailing-.example.com',
        'sky@apps.io',
    ])
    def test_malformed_domain_rejected(self, domain):
        with _config(wildcard_domain=domain):
            with pytest.raises(ValueError, match='Invalid'):
                network_utils.get_wildcard_ingress_config()

    def test_empty_domain_is_disabled(self):
        with _config(wildcard_domain=''):
            assert network_utils.get_wildcard_ingress_config() is None

    @pytest.mark.parametrize(
        'domain', ['SkyPilot.Example.COM', ' skypilot.example.com. '])
    def test_domain_is_normalized(self, domain):
        with _config(wildcard_domain=domain):
            config = network_utils.get_wildcard_ingress_config()
        assert config is not None
        assert config.wildcard_domain == _DOMAIN_UNDER_API

    def test_domain_must_share_parent_with_api_server(self):
        """The session cookie has to reach service hostnames.

        SkyPilot authorizes requests using the caller's API server session. A
        service on an unrelated registrable domain would never receive that
        cookie, so every request would bounce to a login it cannot complete.
        """
        with _config(wildcard_domain='skyapps.io'):
            with pytest.raises(ValueError,
                               match='does not share a registrable domain'):
                network_utils.get_wildcard_ingress_config()

    def test_subdomain_of_api_server_accepted(self):
        with _config(wildcard_domain='apps.skypilot.example.com'):
            assert network_utils.get_wildcard_ingress_config() is not None

    def test_sibling_under_same_parent_accepted(self):
        with _config(wildcard_domain='serve.example.com',
                     _api_server_endpoint='https://sky.example.com'):
            assert network_utils.get_wildcard_ingress_config() is not None

    @pytest.mark.parametrize(
        'endpoint', [None, '', 'http://127.0.0.1:46580', 'https://10.0.0.7'])
    def test_api_server_must_be_a_hostname(self, endpoint):
        """An IP or unset endpoint has no cookie scope to share."""
        with _config(wildcard_domain=_DOMAIN_UNDER_API,
                     _api_server_endpoint=endpoint):
            with pytest.raises(ValueError, match='api_server.endpoint'):
                network_utils.get_wildcard_ingress_config()

    def test_auth_endpoints_are_derived_from_the_api_server(self):
        """Never configured by an admin: a typo would expose every service."""
        with _config(wildcard_domain=_DOMAIN_UNDER_API,
                     tls={'mode': 'external'}):
            config = network_utils.get_wildcard_ingress_config()
        assert config is not None
        assert config.auth_url == (
            f'https://{_DOMAIN_UNDER_API}{network_utils.SERVE_AUTHZ_PATH}')
        assert config.auth_signin_url.startswith(
            f'https://{_DOMAIN_UNDER_API}/oauth2/start?rd=')

    def test_auth_endpoint_scheme_follows_tls_mode(self):
        with _config(wildcard_domain=_DOMAIN_UNDER_API):
            config = network_utils.get_wildcard_ingress_config()
        assert config is not None
        assert config.auth_url.startswith('http://')

    def test_secret_tls_mode_requires_key_replication_ack(self):
        """The wildcard key would land in user-workload namespaces."""
        with _config(wildcard_domain=_DOMAIN_UNDER_API,
                     tls={
                         'mode': 'secret',
                         'secret_name': 'skypilot-wildcard-tls',
                     }):
            with pytest.raises(ValueError,
                               match='i_understand_key_replication'):
                network_utils.get_wildcard_ingress_config()

    def test_secret_tls_mode_with_ack(self):
        with _config(wildcard_domain=_DOMAIN_UNDER_API,
                     tls={
                         'mode': 'secret',
                         'secret_name': 'skypilot-wildcard-tls',
                         'i_understand_key_replication': True,
                     }):
            config = network_utils.get_wildcard_ingress_config()
        assert config is not None
        assert config.tls_secret_name == 'skypilot-wildcard-tls'
        assert config.scheme == 'https'

    def test_secret_tls_mode_requires_secret_name(self):
        with _config(wildcard_domain=_DOMAIN_UNDER_API,
                     tls={
                         'mode': 'secret',
                         'i_understand_key_replication': True,
                     }):
            with pytest.raises(ValueError, match='secret_name'):
                network_utils.get_wildcard_ingress_config()

    def test_external_tls_mode_emits_no_secret(self):
        with _config(wildcard_domain=_DOMAIN_UNDER_API,
                     tls={'mode': 'external'}):
            config = network_utils.get_wildcard_ingress_config()
        assert config is not None
        assert config.tls_secret_name is None
        assert config.scheme == 'https'

    def test_invalid_tls_mode_rejected(self):
        with _config(wildcard_domain=_DOMAIN_UNDER_API, tls={'mode': 'acm'}):
            with pytest.raises(ValueError, match='tls.mode'):
                network_utils.get_wildcard_ingress_config()


class TestAdminOnlyEnforcement:
    """§4.5: `kubernetes.ingress` must not be settable by a client."""

    def test_client_override_is_dropped(self):
        """A client's `~/.sky/config.yaml` cannot set the wildcard domain.

        A client that could set it would choose the public hostname of its own
        service, and could point a hostname operators trust at its own pod.
        """
        client_config = {
            'kubernetes': {
                'ports': 'ingress',
                'ingress': {
                    'wildcard_domain': 'attacker.example.com',
                    'allow_unauthenticated': True,
                },
            },
        }
        with skypilot_config.override_skypilot_config(client_config):
            assert skypilot_config.get_nested(
                ('kubernetes', 'ingress'), default_value=None) is None
            assert network_utils.get_wildcard_ingress_config() is None
            # Keys that are not admin-only still come through, so the drop is
            # targeted rather than the whole block being ignored.
            assert skypilot_config.get_nested(('kubernetes', 'ports'),
                                              default_value=None) == 'ingress'

    def test_ingress_is_listed_as_admin_only(self):
        assert ('kubernetes',
                'ingress') in (constants.SKIPPED_CLIENT_OVERRIDE_KEYS)


class TestWildcardIngressTemplate:
    """Rendering of the wildcard Ingress object."""

    def _render(self, **overrides):
        config = network_utils.WildcardIngressConfig(wildcard_domain=_DOMAIN,
                                                     **overrides)
        with mock.patch('sky.skypilot_config.get_effective_region_config',
                        return_value={}), mock.patch(
                            'sky.provision.kubernetes.utils'
                            '.get_cleaned_context_and_cloud_str',
                            return_value=('ctx', 'kubernetes')):
            return network_utils.fill_wildcard_ingress_template(
                namespace='default',
                context='ctx',
                service_details=[
                    ('c0--skypilot-svc--8080', 8080,
                     f'c0-8080--abcd1234.{_DOMAIN}'),
                ],
                ingress_name='c0-skypilot-wildcard-ingress',
                wildcard_config=config,
            )

    def test_root_path_host_rule(self):
        spec = self._render()
        rule = spec['spec']['rules'][0]
        assert rule['host'] == f'c0-8080--abcd1234.{_DOMAIN}'
        path = rule['http']['paths'][0]
        assert path['path'] == '/'
        assert path['pathType'] == 'Prefix'
        assert path['backend']['service']['name'] == 'c0--skypilot-svc--8080'
        # The sub-path ingress' rewrite annotations must not appear here, or
        # they would strip the request path from these root-path rules.
        assert 'nginx.ingress.kubernetes.io/rewrite-target' not in (
            spec['metadata']['annotations'])

    def test_no_tls_block_without_a_secret(self):
        assert 'tls' not in self._render()['spec']

    def test_tls_block_with_a_secret(self):
        spec = self._render(tls_mode='secret', tls_secret_name='wildcard-tls')
        assert spec['spec']['tls'] == [{
            'hosts': [f'c0-8080--abcd1234.{_DOMAIN}'],
            'secretName': 'wildcard-tls',
        }]

    def test_auth_annotations(self):
        spec = self._render(auth_url='https://auth/oauth2/auth',
                            auth_signin_url='https://auth/oauth2/start')
        annotations = spec['metadata']['annotations']
        assert annotations['nginx.ingress.kubernetes.io/auth-url'] == (
            'https://auth/oauth2/auth')
        assert annotations['nginx.ingress.kubernetes.io/auth-signin'] == (
            'https://auth/oauth2/start')


class TestServeEndpointHostNaming:
    """Service-keyed hostnames -- identity is the service, not the port."""

    def test_hostname_is_independent_of_the_port(self):
        """The whole point: a service keeps its name across port changes.

        SkyServe assigns load balancer ports with `find_free_port(30001)`, so
        a service that is torn down and brought back may land on a different
        port. Its hostname must not move with it.
        """
        a = network_utils.generate_serve_endpoint_host('default', 'my-llm',
                                                       _DOMAIN)
        b = network_utils.generate_serve_endpoint_host('default', 'my-llm',
                                                       _DOMAIN)
        assert a == b
        assert a.startswith('my-llm--')

    def test_distinct_services_get_distinct_hostnames(self):
        hosts = {
            network_utils.generate_serve_endpoint_host('default', name, _DOMAIN)
            for name in ('my-llm', 'my-llm-2', 'other')
        }
        assert len(hosts) == 3

    def test_namespace_is_part_of_the_identity(self):
        a = network_utils.generate_serve_endpoint_host('team-a', 'svc', _DOMAIN)
        b = network_utils.generate_serve_endpoint_host('team-b', 'svc', _DOMAIN)
        assert a != b


class TestServeIngressReconcile:
    """Full re-render from the live service table."""

    def _reconcile(self, service_to_port, live_hash=None):
        config = network_utils.WildcardIngressConfig(wildcard_domain=_DOMAIN)
        with mock.patch(
                'sky.provision.kubernetes.network_utils'
                '._read_ingress_annotations',
                return_value=({
                    network_utils.SPEC_HASH_ANNOTATION: live_hash
                } if live_hash else {})), mock.patch(
                    'sky.provision.kubernetes.network_utils'
                    '.create_or_replace_namespaced_ingress') as mock_create, (
                        mock.patch(
                            'sky.provision.kubernetes.network_utils'
                            '.delete_namespaced_ingress')) as mock_delete, (
                                mock.patch(
                                    'sky.skypilot_config'
                                    '.get_effective_region_config',
                                    return_value={})), mock.patch(
                                        'sky.provision.kubernetes.utils'
                                        '.get_cleaned_context_and_cloud_str',
                                        return_value=('ctx', 'kubernetes')):
            hosts = network_utils.reconcile_serve_ingress(
                namespace='default',
                context='ctx',
                ingress_name='ctrl-skypilot-serve-ingress',
                controller_cluster_name_on_cloud='ctrl',
                service_to_port=service_to_port,
                wildcard_config=config)
        return hosts, mock_create, mock_delete

    def test_renders_a_rule_per_service(self):
        hosts, mock_create, _ = self._reconcile({'a': 30001, 'b': 30002})
        assert set(hosts) == {'a', 'b'}
        spec = mock_create.call_args.kwargs['ingress_spec']
        rules = spec['spec']['rules']
        assert [r['host'] for r in rules] == [hosts['a'], hosts['b']]
        # Each rule must point at the ClusterIP service for that service's
        # current port.
        backends = {
            r['host']: r['http']['paths'][0]['backend']['service']
            for r in rules
        }
        assert backends[hosts['a']]['name'] == 'ctrl--skypilot-svc--30001'
        assert backends[hosts['a']]['port']['number'] == 30001
        assert backends[hosts['b']]['name'] == 'ctrl--skypilot-svc--30002'

    def test_publishes_the_service_to_host_map(self):
        hosts, mock_create, _ = self._reconcile({'a': 30001})
        spec = mock_create.call_args.kwargs['ingress_spec']
        published = spec['metadata']['annotations'][
            network_utils.SERVE_ENDPOINT_HOSTS_ANNOTATION]
        assert json.loads(published) == hosts

    def test_removed_service_loses_its_rule(self):
        """A torn-down service vanishes from the next render.

        This is what makes an orphaned route impossible: there is no
        incremental delete path that could be missed.
        """
        _, mock_create, _ = self._reconcile({'a': 30001})
        spec = mock_create.call_args.kwargs['ingress_spec']
        assert len(spec['spec']['rules']) == 1
        published = json.loads(spec['metadata']['annotations'][
            network_utils.SERVE_ENDPOINT_HOSTS_ANNOTATION])
        assert 'b' not in published

    def test_port_reassignment_repoints_the_same_hostname(self):
        """A service that moves ports keeps its hostname, gains a new backend.

        The inverse of the hazard: the hostname follows the service, and the
        rule is what changes.
        """
        hosts_before, create_before, _ = self._reconcile({'a': 30001})
        hosts_after, create_after, _ = self._reconcile({'a': 30005})
        assert hosts_before['a'] == hosts_after['a']

        def backend_of(mock_create):
            spec = mock_create.call_args.kwargs['ingress_spec']
            return spec['spec']['rules'][0]['http']['paths'][0]['backend'][
                'service']['port']['number']

        assert backend_of(create_before) == 30001
        assert backend_of(create_after) == 30005

    def test_no_services_deletes_the_ingress(self):
        hosts, mock_create, mock_delete = self._reconcile({})
        assert hosts == {}
        mock_create.assert_not_called()
        mock_delete.assert_called_once()
        assert mock_delete.call_args.kwargs['missing_ok'] is True

    def test_unchanged_spec_skips_the_write(self):
        """Avoids an ingress controller reload on every reconcile tick."""
        _, mock_create, _ = self._reconcile({'a': 30001})
        spec = mock_create.call_args.kwargs['ingress_spec']
        live_hash = spec['metadata']['annotations'][
            network_utils.SPEC_HASH_ANNOTATION]

        hosts, mock_create_2, _ = self._reconcile({'a': 30001},
                                                  live_hash=live_hash)
        mock_create_2.assert_not_called()
        # The caller still learns the live hostnames.
        assert set(hosts) == {'a'}

    def test_changed_spec_does_write(self):
        _, mock_create, _ = self._reconcile({'a': 30001})
        stale_hash = mock_create.call_args.kwargs['ingress_spec']['metadata'][
            'annotations'][network_utils.SPEC_HASH_ANNOTATION]
        _, mock_create_2, _ = self._reconcile({'a': 30002},
                                              live_hash=stale_hash)
        mock_create_2.assert_called_once()


class TestServeEndpointHostsReadback:
    """Readers resolve hostnames from the published map, never recompute."""

    def _read(self, annotations):
        with mock.patch(
                'sky.provision.kubernetes.network_utils'
                '._read_ingress_annotations',
                return_value=annotations):
            return network_utils.get_serve_endpoint_hosts_from_ingress(
                namespace='default', context='ctx', ingress_name='ing')

    def test_reads_published_map(self):
        hosts = {'a': f'a--deadbeef.{_DOMAIN}'}
        assert self._read({
            network_utils.SERVE_ENDPOINT_HOSTS_ANNOTATION: json.dumps(hosts)
        }) == hosts

    def test_absent_annotation(self):
        assert self._read({}) == {}

    def test_malformed_annotation_is_ignored(self):
        assert self._read(
            {network_utils.SERVE_ENDPOINT_HOSTS_ANNOTATION: 'not json'}) == {}

    def test_non_dict_annotation_is_ignored(self):
        assert self._read(
            {network_utils.SERVE_ENDPOINT_HOSTS_ANNOTATION: '["a"]'}) == {}


class TestQueryServeEndpoints:
    """`query_serve_endpoints` -- what `sky serve status` consumes."""

    def _query(self, hosts, config):
        with mock.patch(
                'sky.provision.kubernetes.network.network_utils'
                '.get_wildcard_ingress_config',
                return_value=config), mock.patch(
                    'sky.provision.kubernetes.network.network_utils'
                    '.get_serve_endpoint_hosts_from_ingress',
                    return_value=hosts), mock.patch(
                        'sky.provision.kubernetes.network.kubernetes_utils'
                        '.get_context_from_config',
                        return_value='ctx'), mock.patch(
                            'sky.provision.kubernetes.network.kubernetes_utils'
                            '.get_namespace_from_config',
                            return_value='default'):
            return network.query_serve_endpoints(
                cluster_name_on_cloud='ctrl',
                provider_config={'context': 'ctx'})

    def test_disabled_returns_empty(self):
        assert self._query({'a': 'h'}, None) == {}

    def test_returns_urls_with_the_configured_scheme(self):
        config = network_utils.WildcardIngressConfig(wildcard_domain=_DOMAIN,
                                                     tls_mode='external')
        assert self._query({'a': f'a--dead.{_DOMAIN}'}, config) == {
            'a': f'https://a--dead.{_DOMAIN}'
        }

    def test_http_scheme_without_tls(self):
        config = network_utils.WildcardIngressConfig(wildcard_domain=_DOMAIN)
        assert self._query({'a': f'a--dead.{_DOMAIN}'}, config) == {
            'a': f'http://a--dead.{_DOMAIN}'
        }

    def test_errors_never_propagate(self):
        """`sky serve status` must not break on an ingress read failure."""
        with mock.patch(
                'sky.provision.kubernetes.network.network_utils'
                '.get_wildcard_ingress_config',
                side_effect=RuntimeError('boom')):
            assert network.query_serve_endpoints(cluster_name_on_cloud='ctrl',
                                                 provider_config={}) == {}


class TestServeEndpointReconcileDaemon:
    """The daemon is skipped entirely unless the feature is configured."""

    def test_skipped_when_wildcard_routing_is_off(self):
        with mock.patch(
                'sky.provision.kubernetes.network_utils'
                '.get_wildcard_ingress_config',
                return_value=None):
            assert daemons.should_skip_serve_endpoint_reconcile() is True

    def test_skipped_when_config_is_invalid(self):
        with mock.patch(
                'sky.provision.kubernetes.network_utils'
                '.get_wildcard_ingress_config',
                side_effect=ValueError('bad domain')):
            assert daemons.should_skip_serve_endpoint_reconcile() is True

    def test_runs_when_configured(self):
        with mock.patch(
                'sky.provision.kubernetes.network_utils'
                '.get_wildcard_ingress_config',
                return_value=network_utils.WildcardIngressConfig(
                    wildcard_domain=_DOMAIN)):
            assert daemons.should_skip_serve_endpoint_reconcile() is False

    def test_registered_as_a_daemon(self):
        ids = {d.id for d in daemons.INTERNAL_REQUEST_DAEMONS}
        assert 'serve-endpoint-reconcile-daemon' in ids

    def test_no_controller_is_not_an_error(self):
        """A missing or non-Kubernetes controller must be a quiet no-op."""
        with mock.patch(
                'sky.server.daemons._serve_controller_kubernetes_target',
                return_value=None), mock.patch(
                    'sky.server.daemons.time.sleep') as mock_sleep:
            daemons.serve_endpoint_reconcile_event()
        mock_sleep.assert_called_once()

    def test_reconcile_failure_does_not_raise(self):
        """A transient Kubernetes error must not kill the daemon."""
        with mock.patch(
                'sky.server.daemons._serve_controller_kubernetes_target',
                side_effect=RuntimeError('api down')), mock.patch(
                    'sky.server.daemons.time.sleep') as mock_sleep:
            daemons.serve_endpoint_reconcile_event()
        mock_sleep.assert_called_once()


class TestServeAuthzState:
    """`serve_authz` -- the state the authorization endpoint reads."""

    def setup_method(self):
        self._store = {}

        def fake_get(key):
            return self._store.get(key)

        def fake_set(key, value):
            self._store[key] = value

        self._patches = [
            mock.patch('sky.global_user_state.get_system_config',
                       side_effect=fake_get),
            mock.patch('sky.global_user_state.set_system_config',
                       side_effect=fake_set),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()

    def test_record_and_read_workspace(self):
        serve_authz.record_service_workspace('my-llm', 'team-a')
        assert serve_authz.get_service_workspaces() == {'my-llm': 'team-a'}

    def test_forget_workspace(self):
        serve_authz.record_service_workspace('my-llm', 'team-a')
        serve_authz.forget_service_workspace('my-llm')
        assert serve_authz.get_service_workspaces() == {}

    def test_forget_unknown_service_is_a_noop(self):
        serve_authz.forget_service_workspace('never-existed')
        assert serve_authz.get_service_workspaces() == {}

    def test_resolve_published_host(self):
        serve_authz.publish_endpoint_hosts(
            {'my-llm--abcd.skypilot.example.com': ('my-llm', 'team-a')})
        assert serve_authz.resolve_host(
            'my-llm--abcd.skypilot.example.com') == ('my-llm', 'team-a')

    def test_resolve_strips_port_and_normalizes(self):
        serve_authz.publish_endpoint_hosts(
            {'my-llm--abcd.skypilot.example.com': ('my-llm', 'team-a')})
        assert serve_authz.resolve_host(
            'My-LLM--ABCD.skypilot.example.com:443') == ('my-llm', 'team-a')

    def test_unknown_host_resolves_to_none(self):
        """An unclaimed hostname must not authorize anything."""
        serve_authz.publish_endpoint_hosts(
            {'my-llm--abcd.skypilot.example.com': ('my-llm', 'team-a')})
        assert serve_authz.resolve_host('evil.skypilot.example.com') is None
        assert serve_authz.resolve_host('') is None

    def test_publish_replaces_rather_than_merges(self):
        """A torn-down service loses its hostname on the next publish."""
        serve_authz.publish_endpoint_hosts({
            'a.skypilot.example.com': ('a', 'w'),
            'b.skypilot.example.com': ('b', 'w'),
        })
        serve_authz.publish_endpoint_hosts(
            {'a.skypilot.example.com': ('a', 'w')})
        assert serve_authz.resolve_host('b.skypilot.example.com') is None

    def test_corrupt_state_is_treated_as_empty(self):
        self._store['serve_endpoint_host_map'] = 'not json'
        assert serve_authz.resolve_host('a.skypilot.example.com') is None


class TestServeAuthzEndpoint:
    """`/serve/authz` -- SkyPilot, not the service, decides who gets in."""

    @staticmethod
    def _auth_user(user_id='u1', email='a@b.com'):
        # `Mock(name=...)` names the mock rather than setting `.name`.
        user = mock.Mock(id=user_id)
        user.name = email
        return user

    @classmethod
    def _request(cls, headers, auth_user='default'):
        if auth_user == 'default':
            auth_user = cls._auth_user()
        request = mock.Mock()
        request.headers = headers
        request.state.auth_user = auth_user
        request.state.anonymous_user = False
        return request

    @staticmethod
    async def _call(request, resolved, allowed=True):
        with mock.patch('sky.serve.serve_authz.resolve_host',
                        return_value=resolved), mock.patch(
                            'sky.users.permission.permission_service'
                            '.check_workspace_permission',
                            return_value=allowed) as mock_check:
            response = await serve_server.authz(request)
        return response, mock_check

    @pytest.mark.asyncio
    async def test_allows_workspace_member(self):
        request = self._request(
            {'X-Forwarded-Host': 'my-llm--abcd.skypilot.example.com'})
        response, mock_check = await self._call(request, ('my-llm', 'team-a'))
        assert response.status_code == 200
        mock_check.assert_called_once_with('u1', 'team-a')
        # Identity is handed to the service so it never authenticates anyone.
        assert response.headers['X-Skypilot-User'] == 'a@b.com'
        assert response.headers['X-Skypilot-Workspace'] == 'team-a'

    @pytest.mark.asyncio
    async def test_denies_non_member(self):
        request = self._request(
            {'X-Forwarded-Host': 'my-llm--abcd.skypilot.example.com'})
        response, _ = await self._call(request, ('my-llm', 'team-a'),
                                       allowed=False)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_denies_unknown_hostname(self):
        """A hostname no service claims must not be authorized."""
        request = self._request({'X-Forwarded-Host': 'evil.example.com'})
        response, mock_check = await self._call(request, None)
        assert response.status_code == 403
        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_denies_when_no_host_is_forwarded(self):
        """Without a host we cannot tell which service to authorize."""
        request = self._request({})
        response, mock_check = await self._call(request, ('a', 'w'))
        assert response.status_code == 403
        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_reads_host_from_original_url(self):
        """ingress-nginx forwards the full original URL, not a host header."""
        request = self._request(
            {'X-Original-URL': 'https://my-llm--abcd.skypilot.example.com/v1'})
        response, _ = await self._call(request, ('my-llm', 'team-a'))
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_fails_closed_without_an_identity(self):
        request = self._request({'X-Forwarded-Host': 'a.skypilot.example.com'},
                                auth_user=None)
        response, mock_check = await self._call(request, ('a', 'w'))
        assert response.status_code == 401
        mock_check.assert_not_called()
