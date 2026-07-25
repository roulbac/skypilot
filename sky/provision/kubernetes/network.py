"""Kubernetes network provisioning."""
from typing import Any, Dict, List, Optional, Tuple

from sky import sky_logging
from sky.adaptors import kubernetes
from sky.provision import common
from sky.provision import constants as provision_constants
from sky.provision.kubernetes import network_utils
from sky.provision.kubernetes import utils as kubernetes_utils
from sky.utils import kubernetes_enums
from sky.utils.resources_utils import port_ranges_to_set

logger = sky_logging.init_logger(__name__)

_PATH_PREFIX = '/skypilot/{namespace}/{cluster_name_on_cloud}/{port}'
_LOADBALANCER_SERVICE_NAME = '{cluster_name_on_cloud}--skypilot-lb'
_INGRESS_NAME = '{cluster_name_on_cloud}-skypilot-ingress'
# Owned by the serve endpoint reconcile daemon, never by the provisioner, so
# that each object has exactly one writer.
_SERVE_INGRESS_NAME = '{cluster_name_on_cloud}-skypilot-serve-ingress'


def _context_and_namespace(
        provider_config: Dict[str, Any]) -> Tuple[Optional[str], str]:
    context = kubernetes_utils.get_context_from_config(provider_config)
    namespace = kubernetes_utils.get_namespace_from_config(provider_config)
    return context, namespace


def reconcile_serve_endpoints(
    cluster_name_on_cloud: str,
    service_to_port: Dict[str, int],
    provider_config: Dict[str, Any],
) -> Dict[str, str]:
    """Renders the SkyServe Ingress from the current service table.

    Args:
        cluster_name_on_cloud: The serve controller's name on the cloud.
        service_to_port: {service name: load balancer port} for every service
            that currently exists. Services absent from this map lose their
            hostname on the next render.

    Returns:
        The {service name: hostname} map that is now live, or {} when wildcard
        routing is not configured.
    """
    wildcard_config = network_utils.get_wildcard_ingress_config()
    context, namespace = _context_and_namespace(provider_config)
    ingress_name = _SERVE_INGRESS_NAME.format(
        cluster_name_on_cloud=cluster_name_on_cloud)
    if wildcard_config is None:
        if network_utils.is_ingress_config_present():
            network_utils.delete_namespaced_ingress(namespace=namespace,
                                                    context=context,
                                                    ingress_name=ingress_name,
                                                    missing_ok=True)
        return {}
    return network_utils.reconcile_serve_ingress(
        namespace=namespace,
        context=context,
        ingress_name=ingress_name,
        controller_cluster_name_on_cloud=cluster_name_on_cloud,
        service_to_port=service_to_port,
        wildcard_config=wildcard_config,
    )


def query_serve_endpoints(
    cluster_name_on_cloud: str,
    provider_config: Dict[str, Any],
) -> Dict[str, str]:
    """Returns the live {service name: endpoint URL} map, or {} if none.

    Never raises on a misconfigured or absent wildcard domain: SkyServe falls
    back to the port-based endpoint, which remains valid.
    """
    try:
        wildcard_config = network_utils.get_wildcard_ingress_config()
        if wildcard_config is None:
            return {}
        context, namespace = _context_and_namespace(provider_config)
        hosts = network_utils.get_serve_endpoint_hosts_from_ingress(
            namespace=namespace,
            context=context,
            ingress_name=_SERVE_INGRESS_NAME.format(
                cluster_name_on_cloud=cluster_name_on_cloud))
        return {
            service_name: f'{wildcard_config.scheme}://{host}'
            for service_name, host in hosts.items()
        }
    except Exception as e:  # pylint: disable=broad-except
        logger.debug(f'Could not read SkyServe endpoint hostnames: {e}')
        return {}


def open_ports(
    cluster_name_on_cloud: str,
    ports: List[str],
    provider_config: Optional[Dict[str, Any]] = None,
) -> None:
    """See sky/provision/__init__.py"""
    assert provider_config is not None, 'provider_config is required'
    context = kubernetes_utils.get_context_from_config(provider_config)
    port_mode = network_utils.get_port_mode(
        provider_config.get('port_mode', None), context)
    ports = list(port_ranges_to_set(ports))
    if port_mode == kubernetes_enums.KubernetesPortMode.LOADBALANCER:
        _open_ports_using_loadbalancer(
            cluster_name_on_cloud=cluster_name_on_cloud,
            ports=ports,
            provider_config=provider_config)
    elif port_mode == kubernetes_enums.KubernetesPortMode.INGRESS:
        _open_ports_using_ingress(cluster_name_on_cloud=cluster_name_on_cloud,
                                  ports=ports,
                                  provider_config=provider_config)
    elif port_mode == kubernetes_enums.KubernetesPortMode.PODIP:
        # Do nothing, as PodIP mode does not require opening ports
        pass


def _open_ports_using_loadbalancer(
    cluster_name_on_cloud: str,
    ports: List[int],
    provider_config: Dict[str, Any],
) -> None:
    service_name = _LOADBALANCER_SERVICE_NAME.format(
        cluster_name_on_cloud=cluster_name_on_cloud)
    context = kubernetes_utils.get_context_from_config(provider_config)
    namespace = kubernetes_utils.get_namespace_from_config(provider_config)
    overrides = provider_config.get('cluster_config_overrides')

    content = network_utils.fill_loadbalancer_template(
        namespace=namespace,
        context=context,
        service_name=service_name,
        ports=ports,
        selector_key=provision_constants.TAG_SKYPILOT_CLUSTER_NAME,
        selector_value=cluster_name_on_cloud,
        cluster_config_overrides=overrides,
    )

    # Update metadata from config
    kubernetes_utils.merge_custom_metadata(content['service_spec']['metadata'],
                                           context=context,
                                           cluster_config_overrides=overrides)

    network_utils.create_or_replace_namespaced_service(
        namespace=kubernetes_utils.get_namespace_from_config(provider_config),
        context=context,
        service_name=service_name,
        service_spec=content['service_spec'])


def _open_ports_using_ingress(
    cluster_name_on_cloud: str,
    ports: List[int],
    provider_config: Dict[str, Any],
) -> None:
    context = kubernetes_utils.get_context_from_config(provider_config)
    namespace = kubernetes_utils.get_namespace_from_config(provider_config)
    overrides = provider_config.get('cluster_config_overrides')
    # Check if an ingress controller exists
    if not network_utils.ingress_controller_exists(context):
        raise Exception(
            'Ingress controller not found. '
            'Install Nginx ingress controller first: '
            'https://github.com/kubernetes/ingress-nginx/blob/main/docs/deploy/index.md.'  # pylint: disable=line-too-long
        )

    # URL path namespace must match the Service's namespace (resolved above
    # from `provider_config`); per-workspace overrides can make these differ.
    service_details = [
        (f'{cluster_name_on_cloud}--skypilot-svc--{port}', port,
         _PATH_PREFIX.format(cluster_name_on_cloud=cluster_name_on_cloud,
                             port=port,
                             namespace=namespace).rstrip('/').lstrip('/'))
        for port in ports
    ]

    # Generate ingress and services specs
    # We batch ingress rule creation because each rule triggers a hot reload of
    # the nginx controller. If the ingress rules are created sequentially,
    # it could lead to multiple reloads of the Nginx-Ingress-Controller within
    # a brief period. Consequently, the Nginx-Controller pod might spawn an
    # excessive number of sub-processes. This surge triggers Kubernetes to kill
    # and restart the Nginx due to the podPidsLimit parameter, which is
    # typically set to a default value like 1024.
    # To avoid this, we change ingress creation into one object containing
    # multiple rules.
    content = network_utils.fill_ingress_template(
        namespace=namespace,
        context=context,
        service_details=service_details,
        ingress_name=_INGRESS_NAME.format(
            cluster_name_on_cloud=cluster_name_on_cloud),
        selector_key=provision_constants.TAG_SKYPILOT_CLUSTER_NAME,
        selector_value=cluster_name_on_cloud,
        cluster_config_overrides=overrides,
    )

    # Create or update services based on the generated specs
    for service_name, service_spec in content['services_spec'].items():
        # Update metadata from config
        kubernetes_utils.merge_custom_metadata(
            service_spec['metadata'],
            context=context,
            cluster_config_overrides=overrides)
        network_utils.create_or_replace_namespaced_service(
            namespace=namespace,
            context=context,
            service_name=service_name,
            service_spec=service_spec,
        )

    kubernetes_utils.merge_custom_metadata(content['ingress_spec']['metadata'],
                                           context=context,
                                           cluster_config_overrides=overrides)
    # Create or update the single ingress for all services
    network_utils.create_or_replace_namespaced_ingress(
        namespace=namespace,
        context=context,
        ingress_name=_INGRESS_NAME.format(
            cluster_name_on_cloud=cluster_name_on_cloud),
        ingress_spec=content['ingress_spec'],
    )


def cleanup_ports(
    cluster_name_on_cloud: str,
    ports: List[str],
    provider_config: Optional[Dict[str, Any]] = None,
) -> None:
    """See sky/provision/__init__.py"""
    assert provider_config is not None, 'provider_config is required'
    context = kubernetes_utils.get_context_from_config(provider_config)
    port_mode = network_utils.get_port_mode(
        provider_config.get('port_mode', None), context)
    ports = list(port_ranges_to_set(ports))
    if port_mode == kubernetes_enums.KubernetesPortMode.LOADBALANCER:
        _cleanup_ports_for_loadbalancer(
            cluster_name_on_cloud=cluster_name_on_cloud,
            provider_config=provider_config)
    elif port_mode == kubernetes_enums.KubernetesPortMode.INGRESS:
        _cleanup_ports_for_ingress(cluster_name_on_cloud=cluster_name_on_cloud,
                                   ports=ports,
                                   provider_config=provider_config)
    elif port_mode == kubernetes_enums.KubernetesPortMode.PODIP:
        # Do nothing, as PodIP mode does not require opening ports
        pass


def _cleanup_ports_for_loadbalancer(
    cluster_name_on_cloud: str,
    provider_config: Dict[str, Any],
) -> None:
    service_name = _LOADBALANCER_SERVICE_NAME.format(
        cluster_name_on_cloud=cluster_name_on_cloud)
    # TODO(aylei): test coverage
    context = provider_config.get(
        'context', kubernetes_utils.get_current_kube_config_context_name())
    namespace = kubernetes_utils.get_namespace_from_config(provider_config)
    network_utils.delete_namespaced_service(
        context=context,
        namespace=namespace,
        service_name=service_name,
    )


def _cleanup_ports_for_ingress(
    cluster_name_on_cloud: str,
    ports: List[int],
    provider_config: Dict[str, Any],
) -> None:
    context = provider_config.get(
        'context', kubernetes_utils.get_current_kube_config_context_name())
    namespace = kubernetes_utils.get_namespace_from_config(provider_config)

    # Tear down routes before backends: an Ingress that outlives its backing
    # Service is a live route to nothing. Absent on any cluster that is not a
    # SkyServe controller, which is not an error.
    network_utils.delete_namespaced_ingress(
        namespace=namespace,
        context=kubernetes_utils.get_context_from_config(provider_config),
        ingress_name=_SERVE_INGRESS_NAME.format(
            cluster_name_on_cloud=cluster_name_on_cloud),
        missing_ok=True,
    )

    # Delete services for each port
    for port in ports:
        service_name = f'{cluster_name_on_cloud}--skypilot-svc--{port}'
        network_utils.delete_namespaced_service(
            context=context,
            namespace=namespace,
            service_name=service_name,
        )

    # Delete the single ingress used for all ports
    ingress_name = _INGRESS_NAME.format(
        cluster_name_on_cloud=cluster_name_on_cloud)
    network_utils.delete_namespaced_ingress(
        namespace=namespace,
        context=kubernetes_utils.get_context_from_config(provider_config),
        ingress_name=ingress_name,
    )


def query_ports(
    cluster_name_on_cloud: str,
    ports: List[str],
    head_ip: Optional[str] = None,
    provider_config: Optional[Dict[str, Any]] = None,
) -> Dict[int, List[common.Endpoint]]:
    """See sky/provision/__init__.py"""
    del head_ip  # unused
    assert provider_config is not None, 'provider_config is required'
    context = kubernetes_utils.get_context_from_config(provider_config)
    port_mode = network_utils.get_port_mode(
        provider_config.get('port_mode', None), context)
    ports = list(port_ranges_to_set(ports))

    try:
        if port_mode == kubernetes_enums.KubernetesPortMode.LOADBALANCER:
            return _query_ports_for_loadbalancer(
                cluster_name_on_cloud=cluster_name_on_cloud,
                ports=ports,
                provider_config=provider_config,
            )
        elif port_mode == kubernetes_enums.KubernetesPortMode.INGRESS:
            return _query_ports_for_ingress(
                cluster_name_on_cloud=cluster_name_on_cloud,
                ports=ports,
                provider_config=provider_config,
            )
        elif port_mode == kubernetes_enums.KubernetesPortMode.PODIP:
            return _query_ports_for_podip(
                cluster_name_on_cloud=cluster_name_on_cloud,
                ports=ports,
                provider_config=provider_config,
            )
        else:
            return {}
    except kubernetes.kubernetes.client.ApiException as e:
        if e.status == 404:
            return {}
        raise e


def _query_ports_for_loadbalancer(
    cluster_name_on_cloud: str,
    ports: List[int],
    provider_config: Dict[str, Any],
) -> Dict[int, List[common.Endpoint]]:
    logger.debug(f'Getting loadbalancer IP for cluster {cluster_name_on_cloud}')
    result: Dict[int, List[common.Endpoint]] = {}
    service_name = _LOADBALANCER_SERVICE_NAME.format(
        cluster_name_on_cloud=cluster_name_on_cloud)
    context = provider_config.get(
        'context', kubernetes_utils.get_current_kube_config_context_name())
    namespace = provider_config.get(
        'namespace',
        kubernetes_utils.get_kube_config_context_namespace(context))
    external_ip = network_utils.get_loadbalancer_ip(
        context=context,
        namespace=namespace,
        service_name=service_name,
        # Timeout is set so that we can retry the query when the
        # cluster is firstly created and the load balancer is not ready yet.
        timeout=60,
    )

    if external_ip is None:
        return {}

    for port in ports:
        result[port] = [common.SocketEndpoint(host=external_ip, port=port)]

    return result


def _query_ports_for_ingress(
    cluster_name_on_cloud: str,
    ports: List[int],
    provider_config: Dict[str, Any],
) -> Dict[int, List[common.Endpoint]]:
    context = provider_config.get(
        'context', kubernetes_utils.get_current_kube_config_context_name())
    ingress_details = network_utils.get_ingress_external_ip_and_ports(context)
    external_ip, external_ports = ingress_details
    if external_ip is None:
        return {}

    namespace = provider_config.get(
        'namespace',
        kubernetes_utils.get_kube_config_context_namespace(context))
    result: Dict[int, List[common.Endpoint]] = {}
    for port in ports:
        path_prefix = _PATH_PREFIX.format(
            cluster_name_on_cloud=cluster_name_on_cloud,
            port=port,
            namespace=namespace)

        http_port, https_port = external_ports \
            if external_ports is not None else (None, None)
        result[port] = [
            common.HTTPEndpoint(host=external_ip,
                                port=http_port,
                                path=path_prefix.lstrip('/')),
            common.HTTPSEndpoint(host=external_ip,
                                 port=https_port,
                                 path=path_prefix.lstrip('/')),
        ]

    return result


def _query_ports_for_podip(
    cluster_name_on_cloud: str,
    ports: List[int],
    provider_config: Dict[str, Any],
) -> Dict[int, List[common.Endpoint]]:
    context = provider_config.get(
        'context', kubernetes_utils.get_current_kube_config_context_name())
    namespace = provider_config.get(
        'namespace',
        kubernetes_utils.get_kube_config_context_namespace(context))
    pod_name = kubernetes_utils.get_head_pod_name(cluster_name_on_cloud)
    pod_ip = network_utils.get_pod_ip(context, namespace, pod_name)

    result: Dict[int, List[common.Endpoint]] = {}
    if pod_ip is None:
        return {}

    for port in ports:
        result[port] = [common.SocketEndpoint(host=pod_ip, port=port)]

    return result
