"""Kubernetes network provisioning utils."""
import dataclasses
import hashlib
import json
import os
import re
import time
import typing
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib.parse

from sky import exceptions
from sky import sky_logging
from sky import skypilot_config
from sky.adaptors import common as adaptors_common
from sky.adaptors import kubernetes
from sky.provision.kubernetes import utils as kubernetes_utils
from sky.utils import directory_utils
from sky.utils import kubernetes_enums
from sky.utils import ux_utils
from sky.utils import yaml_utils

if typing.TYPE_CHECKING:
    import jinja2
else:
    jinja2 = adaptors_common.LazyImport('jinja2')

logger = sky_logging.init_logger(__name__)

_INGRESS_TEMPLATE_NAME = 'kubernetes-ingress.yml.j2'
_WILDCARD_INGRESS_TEMPLATE_NAME = 'kubernetes-wildcard-ingress.yml.j2'
_LOADBALANCER_TEMPLATE_NAME = 'kubernetes-loadbalancer.yml.j2'

# ---------------------------------------------------------------------------
# Wildcard subdomain routing (opt-in).
#
# When `kubernetes.ingress.wildcard_domain` is set, SkyPilot emits an
# *additional* Ingress whose rules are keyed on a per-(cluster, port) hostname
# under the admin-configured wildcard domain, serving the backend at the root
# path. The existing sub-path Ingress is always emitted as well, so this is
# purely additive and the default (unset) behavior is unchanged.
# ---------------------------------------------------------------------------

# Annotation holding the {service name: hostname} map on the SkyServe Ingress,
# as JSON. A single annotation rather than one key per service, because service
# names are user-chosen and annotation *keys* are charset- and length-limited
# while values are not.
SERVE_ENDPOINT_HOSTS_ANNOTATION = 'skypilot.co/serve-endpoint-hosts'

# Hash of the desired spec, used to skip writes that would not change anything.
# Every write to an Ingress triggers a configuration reload of the shared
# ingress controller, so a reconcile loop that rewrote the object every tick
# would produce reload storms proportional to the number of API servers.
SPEC_HASH_ANNOTATION = 'skypilot.co/spec-hash'

# Forward-auth endpoint on the API server. Every request to every service
# hostname is authorized here before it reaches the service.
SERVE_AUTHZ_PATH = '/serve/authz'

# Identity the authz endpoint returns, forwarded to the service so it never
# has to authenticate anyone itself.
SERVE_AUTH_RESPONSE_HEADERS = ('X-Skypilot-User,X-Skypilot-User-Id,'
                               'X-Skypilot-Workspace')

# A DNS label is capped at 63 bytes. The generated label is
# `<name>--<8 hex chars>`, so the name portion is capped well below that to
# leave headroom.
_MAX_DNS_LABEL_LENGTH = 63
_HOST_HASH_LENGTH = 8
_MAX_HOST_NAME_LENGTH = 48

# Hostnames that must never be produced by name generation, because operators
# commonly point infrastructure at them under the same domain. Every generated
# label carries a `--<hash>` suffix, so a collision is already impossible; this
# is belt-and-braces against future changes to the scheme.
RESERVED_HOST_LABELS = frozenset({
    'acme-challenge',
    'admin',
    'api',
    'auth',
    'dashboard',
    'grafana',
    'localhost',
    'login',
    'oauth',
    'oauth2',
    'skypilot',
    'www',
})

# Suffixes that are effectively public suffixes with more than one label. Used
# to approximate the registrable domain when comparing the wildcard domain
# against the API server's own host. This is intentionally a small, conservative
# list: an unknown multi-label suffix makes the check *stricter* (it compares
# fewer labels), never looser.
_MULTI_LABEL_PUBLIC_SUFFIXES = frozenset({
    'ac.uk',
    'co.at',
    'co.il',
    'co.in',
    'co.jp',
    'co.kr',
    'co.nz',
    'co.uk',
    'co.za',
    'com.ar',
    'com.au',
    'com.br',
    'com.cn',
    'com.mx',
    'com.sg',
    'com.tr',
    'edu.au',
    'gov.uk',
    'net.au',
    'net.cn',
    'org.au',
    'org.uk',
})

_VALID_DOMAIN_RE = re.compile(
    r'^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$')


class WildcardIngressTLSMode:
    """Where the TLS certificate for the wildcard domain lives."""
    # No `spec.tls` on the Ingress; endpoints are advertised over http://.
    NONE = 'none'
    # `spec.tls` referencing a Secret that must exist in *every* namespace
    # where SkyPilot creates Ingresses (i.e. where user workloads run). This
    # replicates the wildcard private key into untrusted namespaces and is
    # gated behind an explicit acknowledgement.
    SECRET = 'secret'
    # TLS is terminated upstream of the Ingress (cloud LB with an ACM/GCP
    # managed cert, a Gateway, a service mesh). No key material in-cluster;
    # endpoints are advertised over https://.
    EXTERNAL = 'external'

    ALL = (NONE, SECRET, EXTERNAL)


@dataclasses.dataclass
class WildcardIngressConfig:
    """Validated `kubernetes.ingress` configuration."""
    wildcard_domain: str
    tls_mode: str = WildcardIngressTLSMode.NONE
    tls_secret_name: Optional[str] = None
    auth_url: Optional[str] = None
    auth_signin_url: Optional[str] = None

    @property
    def scheme(self) -> str:
        if self.tls_mode == WildcardIngressTLSMode.NONE:
            return 'http'
        return 'https'


def _sanitize_host_label(name: str) -> str:
    """Reduces an arbitrary name to a safe leading portion of a DNS label."""
    label = re.sub(r'[^a-z0-9-]', '-', name.lower())
    label = re.sub(r'-{2,}', '-', label).strip('-')
    if not label:
        label = 'sky'
    label = label[:_MAX_HOST_NAME_LENGTH].rstrip('-')
    if not label:
        label = 'sky'
    if label in RESERVED_HOST_LABELS:
        label = f's-{label}'
    return label


def generate_wildcard_host(identity: str, name: str,
                           wildcard_domain: str) -> str:
    """Builds the single-DNS-label hostname for one exposed port.

    The label is ``<sanitized-name>--<8 hex>`` so that a one-level
    ``*.<wildcard_domain>`` certificate and DNS record are sufficient; a name
    containing a `.` can never produce a deeper label.

    Args:
        identity: The full, *untruncated* identity of the endpoint (namespace,
            cluster and port). The hash is taken over this, not over the
            truncated display name, so truncation can never collide two
            distinct endpoints onto one hostname.
        name: The human-readable portion of the hostname. Sanitized and
            truncated; only affects readability, never uniqueness.
        wildcard_domain: The admin-configured domain, e.g. ``skyapps.io``.

    Returns:
        A fully qualified hostname, e.g.
        ``my-cluster-8080--a1b2c3d4.skyapps.io``.
    """
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    suffix = digest[:_HOST_HASH_LENGTH]
    label = f'{_sanitize_host_label(name)}--{suffix}'
    # RFC 5891 reserves labels with '--' in the third and fourth position;
    # that is what makes `xn--` punycode work, and a browser or CA may treat
    # such a label as an A-label and reject or re-interpret it. Checked on the
    # composed label, not on the name alone: a two-character name lands the
    # '--' separator itself in that position.
    if label[2:4] == '--':
        label = f's{label}'
    # Defensive: the suffix makes both of these unreachable today, but the
    # cost of checking is nil and the cost of being wrong is a route that
    # shadows infrastructure.
    assert len(label) <= _MAX_DNS_LABEL_LENGTH, label
    assert label not in RESERVED_HOST_LABELS, label
    return f'{label}.{wildcard_domain}'


def generate_serve_endpoint_host(namespace: str, service_name: str,
                                 wildcard_domain: str) -> str:
    """Builds the hostname for a SkyServe service.

    Unlike `generate_wildcard_host`, the identity here is the *service*, not
    the load balancer port it happens to occupy. SkyServe hands out ports with
    `find_free_port(30001)`, so a torn-down service frees its port for the next
    service to take; keying the hostname on the port would hand the new service
    the old one's address. Keying on the service name instead means the
    hostname belongs to the service for as long as the service exists, and
    disappears with it.
    """
    return generate_wildcard_host(identity='|'.join(
        ['serve', namespace, service_name]),
                                  name=service_name,
                                  wildcard_domain=wildcard_domain)


def get_registrable_domain(host: str) -> str:
    """Approximates the registrable ('cookie-settable') domain of a host."""
    labels = host.lower().strip('.').split('.')
    if len(labels) >= 3 and '.'.join(
            labels[-2:]) in _MULTI_LABEL_PUBLIC_SUFFIXES:
        return '.'.join(labels[-3:])
    return '.'.join(labels[-2:])


def _get_api_server_host() -> Optional[str]:
    """Returns the hostname of the API server, if it is known and not an IP."""
    endpoint = skypilot_config.get_nested(('api_server', 'endpoint'),
                                          default_value=None)
    if not endpoint:
        return None
    host = urllib.parse.urlparse(endpoint).hostname
    if not host or re.fullmatch(r'[0-9.]+', host) or ':' in host:
        # An IP literal has no cookie domain to share.
        return None
    return host


def _validate_wildcard_domain(wildcard_domain: str) -> str:
    """Validates the wildcard domain and returns the API server host.

    SkyPilot authorizes every request to a service itself, using the caller's
    API server session or service account token. For a browser session to be
    present on a service hostname at all, services must sit under the same
    registrable domain as the API server -- otherwise the session cookie can
    never reach them and every request would bounce to a login that cannot
    complete.

    So this design *requires* the shared parent, e.g. API server on
    ``skypilot.example.com`` and services on ``*.skypilot.example.com``.

    Raises:
        ValueError: if the domain is malformed, the API server host is unknown,
            or the two do not share a registrable domain.
    """
    if not _VALID_DOMAIN_RE.fullmatch(wildcard_domain):
        with ux_utils.print_exception_no_traceback():
            raise ValueError(
                f'Invalid kubernetes.ingress.wildcard_domain '
                f'{wildcard_domain!r}: expected a domain with at least two '
                'labels, e.g. "skypilot.example.com".')

    api_server_host = _get_api_server_host()
    if api_server_host is None:
        with ux_utils.print_exception_no_traceback():
            raise ValueError(
                'kubernetes.ingress.wildcard_domain requires the API server '
                'to be reachable at a hostname, so services can share its '
                'login session. Set api_server.endpoint to the external '
                'hostname of the API server; an IP address is not enough.')

    if (get_registrable_domain(wildcard_domain) !=
            get_registrable_domain(api_server_host)):
        with ux_utils.print_exception_no_traceback():
            raise ValueError(
                f'kubernetes.ingress.wildcard_domain {wildcard_domain!r} does '
                'not share a registrable domain with the API server host '
                f'{api_server_host!r}. SkyPilot authorizes service requests '
                "using the caller's API server session, which a browser only "
                'sends to hosts under the same registrable domain. Use, for '
                'example, an API server on "skypilot.example.com" and '
                'services on "*.skypilot.example.com".')
    return api_server_host


def is_ingress_config_present() -> bool:
    """Returns whether an admin has configured `kubernetes.ingress` at all.

    Distinguishes "never enabled" (the default for every deployment) from
    "explicitly turned off", so the default path does not pay for cleanup of
    objects that cannot exist.
    """
    return bool(
        skypilot_config.get_nested(('kubernetes', 'ingress'),
                                   default_value=None))


def get_wildcard_ingress_config() -> Optional[WildcardIngressConfig]:
    """Reads and validates `kubernetes.ingress` from the API server config.

    Returns None when wildcard subdomain routing is not configured, which is
    the default. These keys are honored exclusively from the API server's own
    config: they are listed in `SKIPPED_CLIENT_OVERRIDE_KEYS`, so a value in a
    client's `~/.sky/config.yaml` is dropped with a warning before it ever
    reaches here.

    Raises:
        ValueError: if the configuration is present but invalid.
    """
    ingress_config = skypilot_config.get_nested(('kubernetes', 'ingress'),
                                                default_value=None)
    if not ingress_config:
        return None
    wildcard_domain = ingress_config.get('wildcard_domain')
    if not wildcard_domain:
        return None
    # DNS is case-insensitive; normalize so the emitted hostnames, the
    # `spec.tls` hosts and the recorded annotation all agree.
    wildcard_domain = wildcard_domain.strip().strip('.').lower()

    api_server_host = _validate_wildcard_domain(wildcard_domain)

    tls_config = ingress_config.get('tls') or {}
    tls_mode = tls_config.get('mode', WildcardIngressTLSMode.NONE)
    if tls_mode not in WildcardIngressTLSMode.ALL:
        with ux_utils.print_exception_no_traceback():
            raise ValueError(
                f'Invalid kubernetes.ingress.tls.mode {tls_mode!r}. Expected '
                f'one of {list(WildcardIngressTLSMode.ALL)}.')
    tls_secret_name = tls_config.get('secret_name')
    if tls_mode == WildcardIngressTLSMode.SECRET:
        if not tls_secret_name:
            with ux_utils.print_exception_no_traceback():
                raise ValueError(
                    'kubernetes.ingress.tls.secret_name is required when '
                    'tls.mode is "secret".')
        if not tls_config.get('i_understand_key_replication', False):
            with ux_utils.print_exception_no_traceback():
                raise ValueError(
                    'kubernetes.ingress.tls.mode "secret" requires the '
                    'wildcard TLS Secret to exist in every namespace where '
                    'SkyPilot creates Ingresses, i.e. in namespaces where '
                    'user workloads run. A key that can impersonate every '
                    'service under the wildcard domain would be readable by '
                    'those workloads. Prefer terminating TLS upstream '
                    '(tls.mode: external) so the key never enters a task '
                    'namespace. To proceed anyway, set '
                    'kubernetes.ingress.tls.i_understand_key_replication: '
                    'true.')
    else:
        tls_secret_name = None

    # Auth endpoints are derived, not configured: SkyPilot knows where its own
    # API server is, and a mistyped auth URL would silently expose every
    # service.
    scheme = 'https' if tls_mode != WildcardIngressTLSMode.NONE else 'http'
    return WildcardIngressConfig(
        wildcard_domain=wildcard_domain,
        tls_mode=tls_mode,
        tls_secret_name=tls_secret_name,
        auth_url=f'{scheme}://{api_server_host}{SERVE_AUTHZ_PATH}',
        auth_signin_url=(f'{scheme}://{api_server_host}/oauth2/start'
                         '?rd=$escaped_request_uri'),
    )


def get_port_mode(
        mode_str: Optional[str],
        context: Optional[str]) -> kubernetes_enums.KubernetesPortMode:
    """Get the port mode from the provider config."""

    curr_kube_config = kubernetes_utils.get_current_kube_config_context_name()
    running_kind = curr_kube_config == kubernetes_utils.KIND_CONTEXT_NAME

    if running_kind:
        # If running in kind (`sky local up`), use ingress mode
        return kubernetes_enums.KubernetesPortMode.INGRESS

    mode_str = mode_str or skypilot_config.get_effective_region_config(
        cloud='kubernetes',
        region=context,
        keys=('ports',),
        default_value=kubernetes_enums.KubernetesPortMode.LOADBALANCER.value)
    try:
        port_mode = kubernetes_enums.KubernetesPortMode(mode_str)
    except ValueError as e:
        with ux_utils.print_exception_no_traceback():
            raise ValueError(str(e)
                + ' Cluster was setup with invalid port mode.'
                + 'Please check the port_mode in provider config.') \
                from None

    return port_mode


def fill_loadbalancer_template(
        namespace: str,
        context: Optional[str],
        service_name: str,
        ports: List[int],
        selector_key: str,
        selector_value: str,
        cluster_config_overrides: Optional[Dict[str, Any]] = None) -> Dict:
    template_path = os.path.join(directory_utils.get_sky_dir(), 'templates',
                                 _LOADBALANCER_TEMPLATE_NAME)
    if not os.path.exists(template_path):
        raise FileNotFoundError(
            f'Template "{_LOADBALANCER_TEMPLATE_NAME}" does not exist.')

    with open(template_path, 'r', encoding='utf-8') as fin:
        template = fin.read()
    context, cloud_str = kubernetes_utils.get_cleaned_context_and_cloud_str(
        context)
    annotations = skypilot_config.get_effective_region_config(
        cloud=cloud_str,
        region=context,
        keys=('custom_metadata', 'annotations'),
        default_value={},
        override_configs=cluster_config_overrides)
    labels = skypilot_config.get_effective_region_config(
        cloud=cloud_str,
        region=context,
        keys=('custom_metadata', 'labels'),
        default_value={},
        override_configs=cluster_config_overrides)
    j2_template = jinja2.Template(template)
    cont = j2_template.render(
        namespace=namespace,
        service_name=service_name,
        ports=ports,
        selector_key=selector_key,
        selector_value=selector_value,
        annotations=annotations,
        labels=labels,
    )
    content = yaml_utils.safe_load(cont)
    return content


def fill_ingress_template(
        namespace: str,
        context: Optional[str],
        service_details: List[Tuple[str, int, str]],
        ingress_name: str,
        selector_key: str,
        selector_value: str,
        cluster_config_overrides: Optional[Dict[str, Any]] = None) -> Dict:
    template_path = os.path.join(directory_utils.get_sky_dir(), 'templates',
                                 _INGRESS_TEMPLATE_NAME)
    if not os.path.exists(template_path):
        raise FileNotFoundError(
            f'Template "{_INGRESS_TEMPLATE_NAME}" does not exist.')
    with open(template_path, 'r', encoding='utf-8') as fin:
        template = fin.read()
    context, cloud_str = kubernetes_utils.get_cleaned_context_and_cloud_str(
        context)
    annotations = skypilot_config.get_effective_region_config(
        cloud=cloud_str,
        region=context,
        keys=('custom_metadata', 'annotations'),
        default_value={},
        override_configs=cluster_config_overrides)
    labels = skypilot_config.get_effective_region_config(
        cloud=cloud_str,
        region=context,
        keys=('custom_metadata', 'labels'),
        default_value={},
        override_configs=cluster_config_overrides)
    j2_template = jinja2.Template(template)
    cont = j2_template.render(
        namespace=namespace,
        service_names_and_ports=[{
            'service_name': name,
            'service_port': port,
            'path_prefix': path_prefix
        } for name, port, path_prefix in service_details],
        ingress_name=ingress_name,
        selector_key=selector_key,
        selector_value=selector_value,
        annotations=annotations,
        labels=labels,
    )
    content = yaml_utils.safe_load(cont)

    # Return a dictionary containing both specs
    return {
        'ingress_spec': content['ingress_spec'],
        'services_spec': content['services_spec']
    }


def fill_wildcard_ingress_template(
        namespace: str,
        context: Optional[str],
        service_details: List[Tuple[str, int, str]],
        ingress_name: str,
        wildcard_config: WildcardIngressConfig,
        cluster_config_overrides: Optional[Dict[str, Any]] = None,
        extra_annotations: Optional[Dict[str, str]] = None) -> Dict:
    """Renders the wildcard-subdomain Ingress.

    Args:
        service_details: (service name, port, hostname) for each exposed port.
        extra_annotations: SkyPilot-owned annotations to record on the object,
            e.g. the SkyServe service-to-hostname map.
    """
    template_path = os.path.join(directory_utils.get_sky_dir(), 'templates',
                                 _WILDCARD_INGRESS_TEMPLATE_NAME)
    if not os.path.exists(template_path):
        raise FileNotFoundError(
            f'Template "{_WILDCARD_INGRESS_TEMPLATE_NAME}" does not exist.')
    with open(template_path, 'r', encoding='utf-8') as fin:
        template = fin.read()
    context, cloud_str = kubernetes_utils.get_cleaned_context_and_cloud_str(
        context)
    labels = skypilot_config.get_effective_region_config(
        cloud=cloud_str,
        region=context,
        keys=('custom_metadata', 'labels'),
        default_value={},
        override_configs=cluster_config_overrides)
    # Composed here rather than in the template so that the rendered value is
    # always a mapping, never null.
    annotations: Dict[str, str] = {}
    if wildcard_config.auth_url is not None:
        annotations['nginx.ingress.kubernetes.io/auth-url'] = (
            wildcard_config.auth_url)
    if wildcard_config.auth_signin_url is not None:
        annotations['nginx.ingress.kubernetes.io/auth-signin'] = (
            wildcard_config.auth_signin_url)
    if wildcard_config.auth_url is not None:
        annotations['nginx.ingress.kubernetes.io/auth-response-headers'] = (
            SERVE_AUTH_RESPONSE_HEADERS)
    annotations.update(extra_annotations or {})

    j2_template = jinja2.Template(template)
    cont = j2_template.render(
        namespace=namespace,
        ingress_name=ingress_name,
        services_and_hosts=[{
            'service_name': name,
            'service_port': port,
            'host': host,
        } for name, port, host in service_details],
        tls_secret_name=wildcard_config.tls_secret_name,
        annotations=annotations,
        labels=labels,
    )
    return yaml_utils.safe_load(cont)['ingress_spec']


def reconcile_serve_ingress(
        namespace: str, context: Optional[str], ingress_name: str,
        controller_cluster_name_on_cloud: str, service_to_port: Dict[str, int],
        wildcard_config: WildcardIngressConfig) -> Dict[str, str]:
    """Renders the SkyServe Ingress from the current service table.

    This is a full re-render rather than an incremental create/delete: the
    desired object is derived from `service_to_port` every time, so a service
    that has gone away is simply absent from the next render. There is no
    delete path that can be missed, and therefore no way to leave a hostname
    routing to a port that has since been handed to a different service.

    Writes are skipped when the desired spec is unchanged, since every write
    reloads the shared ingress controller's configuration.

    Returns:
        The {service name: hostname} map that is now live.
    """
    hosts = {
        service_name:
        generate_serve_endpoint_host(namespace, service_name,
                                     wildcard_config.wildcard_domain)
        for service_name in service_to_port
    }
    if not hosts:
        # No services: drop the object entirely rather than leave an Ingress
        # with an empty rule list.
        delete_namespaced_ingress(namespace=namespace,
                                  context=context,
                                  ingress_name=ingress_name,
                                  missing_ok=True)
        return {}

    service_details = [
        (f'{controller_cluster_name_on_cloud}--skypilot-svc--{port}', port,
         hosts[service_name])
        for service_name, port in sorted(service_to_port.items())
    ]
    ingress_spec = fill_wildcard_ingress_template(
        namespace=namespace,
        context=context,
        service_details=service_details,
        ingress_name=ingress_name,
        wildcard_config=wildcard_config,
        extra_annotations={
            SERVE_ENDPOINT_HOSTS_ANNOTATION: json.dumps(hosts, sort_keys=True),
        },
    )
    spec_hash = hashlib.sha256(
        json.dumps(ingress_spec, sort_keys=True).encode('utf-8')).hexdigest()
    ingress_spec['metadata']['annotations'][SPEC_HASH_ANNOTATION] = spec_hash

    if _get_ingress_spec_hash(namespace, context, ingress_name) == spec_hash:
        logger.debug(f'SkyServe ingress {ingress_name!r} is already up to '
                     'date; skipping write to avoid an ingress controller '
                     'reload.')
        return hosts

    create_or_replace_namespaced_ingress(namespace=namespace,
                                         context=context,
                                         ingress_name=ingress_name,
                                         ingress_spec=ingress_spec)
    return hosts


def _get_ingress_spec_hash(namespace: str, context: Optional[str],
                           ingress_name: str) -> Optional[str]:
    """Returns the recorded spec hash of an Ingress, or None if absent."""
    annotations = _read_ingress_annotations(namespace, context, ingress_name)
    return annotations.get(SPEC_HASH_ANNOTATION)


def get_serve_endpoint_hosts_from_ingress(namespace: str,
                                          context: Optional[str],
                                          ingress_name: str) -> Dict[str, str]:
    """Reads the {service name: hostname} map from the SkyServe Ingress.

    As with per-port hostnames, the map written at reconcile time is the single
    source of truth; readers never recompute a hostname.
    """
    annotations = _read_ingress_annotations(namespace, context, ingress_name)
    raw = annotations.get(SERVE_ENDPOINT_HOSTS_ANNOTATION)
    if not raw:
        return {}
    try:
        hosts = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug(f'Ignoring malformed {SERVE_ENDPOINT_HOSTS_ANNOTATION} '
                     f'annotation on ingress {ingress_name!r}.')
        return {}
    if not isinstance(hosts, dict):
        return {}
    return {str(k): str(v) for k, v in hosts.items()}


def _read_ingress_annotations(namespace: str, context: Optional[str],
                              ingress_name: str) -> Dict[str, str]:
    """Returns an Ingress' annotations, or {} if it does not exist."""
    networking_api = kubernetes.networking_api(context)
    try:
        ingress = networking_api.read_namespaced_ingress(
            ingress_name, namespace, _request_timeout=kubernetes.API_TIMEOUT)
    except kubernetes.kubernetes.client.ApiException as e:
        if e.status == 404:
            return {}
        raise
    if ingress.metadata is None:
        return {}
    return ingress.metadata.annotations or {}


def create_or_replace_namespaced_ingress(
        namespace: str, context: Optional[str], ingress_name: str,
        ingress_spec: Dict[str, Union[str, int]]) -> None:
    """Creates an ingress resource for the specified service."""
    networking_api = kubernetes.networking_api(context)

    try:
        networking_api.read_namespaced_ingress(
            ingress_name, namespace, _request_timeout=kubernetes.API_TIMEOUT)
    except kubernetes.kubernetes.client.ApiException as e:
        if e.status == 404:
            networking_api.create_namespaced_ingress(
                namespace,
                ingress_spec,
                _request_timeout=kubernetes.API_TIMEOUT)
            return
        raise e

    networking_api.replace_namespaced_ingress(
        ingress_name,
        namespace,
        ingress_spec,
        _request_timeout=kubernetes.API_TIMEOUT)


def delete_namespaced_ingress(namespace: str,
                              context: Optional[str],
                              ingress_name: str,
                              missing_ok: bool = False) -> None:
    """Deletes an ingress resource."""
    networking_api = kubernetes.networking_api(context)
    try:
        networking_api.delete_namespaced_ingress(
            ingress_name, namespace, _request_timeout=kubernetes.API_TIMEOUT)
    except kubernetes.kubernetes.client.ApiException as e:
        if e.status == 404:
            if missing_ok:
                return
            raise exceptions.PortDoesNotExistError(
                f'Port {ingress_name.split("--")[-1]} does not exist.')
        raise e


def create_or_replace_namespaced_service(
        namespace: str, context: Optional[str], service_name: str,
        service_spec: Dict[str, Union[str, int]]) -> None:
    """Creates a service resource for the specified service."""
    core_api = kubernetes.core_api(context)

    try:
        core_api.read_namespaced_service(
            service_name, namespace, _request_timeout=kubernetes.API_TIMEOUT)
    except kubernetes.kubernetes.client.ApiException as e:
        if e.status == 404:
            core_api.create_namespaced_service(
                namespace,
                service_spec,
                _request_timeout=kubernetes.API_TIMEOUT)
            return
        raise e

    core_api.replace_namespaced_service(service_name,
                                        namespace,
                                        service_spec,
                                        _request_timeout=kubernetes.API_TIMEOUT)


def delete_namespaced_service(context: Optional[str], namespace: str,
                              service_name: str) -> None:
    """Deletes a service resource."""
    core_api = kubernetes.core_api(context)

    try:
        core_api.delete_namespaced_service(
            service_name, namespace, _request_timeout=kubernetes.API_TIMEOUT)
    except kubernetes.kubernetes.client.ApiException as e:
        if e.status == 404:
            raise exceptions.PortDoesNotExistError(
                f'Port {service_name.split("--")[-1]} does not exist.')
        raise e


def ingress_controller_exists(context: Optional[str],
                              ingress_class_name: str = 'nginx') -> bool:
    """Checks if an ingress controller exists in the cluster."""
    networking_api = kubernetes.networking_api(context)
    ingress_classes = networking_api.list_ingress_class(
        _request_timeout=kubernetes.API_TIMEOUT).items
    return any(
        map(lambda item: item.metadata.name == ingress_class_name,
            ingress_classes))


def get_ingress_external_ip_and_ports(
    context: Optional[str],
    namespace: str = 'ingress-nginx'
) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    """Returns external ip and ports for the ingress controller."""
    core_api = kubernetes.core_api(context)
    ingress_services = [
        item for item in core_api.list_namespaced_service(
            namespace, _request_timeout=kubernetes.API_TIMEOUT).items
        if item.metadata.name == 'ingress-nginx-controller'
    ]
    if not ingress_services:
        return (None, None)

    ingress_service = ingress_services[0]
    if ingress_service.status.load_balancer.ingress is None:
        # We try to get an IP/host for the service in the following order:
        # 1. Try to use assigned external IP if it exists
        # 2. Use the skypilot.co/external-ip annotation in the service
        # 3. Otherwise return 'localhost'
        ip = None
        if ingress_service.spec.external_i_ps is not None:
            ip = ingress_service.spec.external_i_ps[0]
        elif ingress_service.metadata.annotations is not None:
            ip = ingress_service.metadata.annotations.get(
                'skypilot.co/external-ip', None)
        if ip is None:
            ip = 'localhost'
        ports = ingress_service.spec.ports
        http_port = [port for port in ports if port.name == 'http'][0].node_port
        https_port = [port for port in ports if port.name == 'https'
                     ][0].node_port
        return ip, (int(http_port), int(https_port))

    external_ip = ingress_service.status.load_balancer.ingress[
        0].ip or ingress_service.status.load_balancer.ingress[0].hostname
    return external_ip, None


def get_loadbalancer_ip(context: Optional[str],
                        namespace: str,
                        service_name: str,
                        timeout: int = 0) -> Optional[str]:
    """Returns the IP address of the load balancer."""
    core_api = kubernetes.core_api(context)

    ip = None

    start_time = time.time()
    retry_cnt = 0
    while ip is None and (retry_cnt == 0 or time.time() - start_time < timeout):
        service = core_api.read_namespaced_service(
            service_name, namespace, _request_timeout=kubernetes.API_TIMEOUT)
        if service.status.load_balancer.ingress is not None:
            ip = (service.status.load_balancer.ingress[0].ip or
                  service.status.load_balancer.ingress[0].hostname)
        if ip is None:
            retry_cnt += 1
            if retry_cnt % 5 == 0:
                logger.debug('Waiting for load balancer IP to be assigned'
                             '...')
            time.sleep(1)
    return ip


def get_pod_ip(context: Optional[str], namespace: str,
               pod_name: str) -> Optional[str]:
    """Returns the IP address of the pod."""
    core_api = kubernetes.core_api(context)
    pod = core_api.read_namespaced_pod(pod_name,
                                       namespace,
                                       _request_timeout=kubernetes.API_TIMEOUT)

    return pod.status.pod_ip if pod.status.pod_ip is not None else None
