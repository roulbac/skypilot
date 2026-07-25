"""Authorization state for SkyServe wildcard-subdomain endpoints.

SkyPilot, not the deployed service, decides who may reach a service. The edge
proxy asks the API server about every request (see the ``/serve/authz``
endpoint), and the API server answers using two pieces of state kept here:

- ``service -> workspace``, recorded when a service is created, because a
  service's workspace is what authorization is checked against.
- ``hostname -> (service, workspace)``, published by the endpoint reconciler
  so the authorization endpoint can resolve a request's ``Host`` header
  without a Kubernetes API call on every request.

Both live in the API server's ``system_config`` key-value table, which is
shared across server and daemon processes.
"""
import json
from typing import Dict, Optional, Tuple

from sky import global_user_state
from sky import sky_logging

logger = sky_logging.init_logger(__name__)

# {service name: workspace}
_SERVICE_WORKSPACES_KEY = 'serve_service_workspaces'
# {hostname: {'service': ..., 'workspace': ...}}
_ENDPOINT_HOSTS_KEY = 'serve_endpoint_host_map'


def _load(key: str) -> Dict:
    raw = global_user_state.get_system_config(key)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f'Corrupt {key!r} row; treating as empty.')
        return {}
    return value if isinstance(value, dict) else {}


def record_service_workspace(service_name: str, workspace: str) -> None:
    """Records the workspace a service was created in.

    Called from ``sky serve up``. Authorization for the service's endpoint is
    checked against this workspace.
    """
    services = _load(_SERVICE_WORKSPACES_KEY)
    if services.get(service_name) == workspace:
        return
    services[service_name] = workspace
    global_user_state.set_system_config(_SERVICE_WORKSPACES_KEY,
                                        json.dumps(services, sort_keys=True))


def forget_service_workspace(service_name: str) -> None:
    """Drops a torn-down service's workspace record."""
    services = _load(_SERVICE_WORKSPACES_KEY)
    if services.pop(service_name, None) is None:
        return
    global_user_state.set_system_config(_SERVICE_WORKSPACES_KEY,
                                        json.dumps(services, sort_keys=True))


def get_service_workspaces() -> Dict[str, str]:
    """Returns the recorded {service name: workspace} map."""
    return {str(k): str(v) for k, v in _load(_SERVICE_WORKSPACES_KEY).items()}


def publish_endpoint_hosts(hosts: Dict[str, Tuple[str, str]]) -> None:
    """Publishes {hostname: (service, workspace)} for the authz endpoint.

    Called by the endpoint reconciler with the full current set, so a service
    that has gone away loses its hostname here at the same time it loses its
    ingress rule.
    """
    payload = {
        host: {
            'service': service,
            'workspace': workspace,
        } for host, (service, workspace) in hosts.items()
    }
    global_user_state.set_system_config(_ENDPOINT_HOSTS_KEY,
                                        json.dumps(payload, sort_keys=True))


def resolve_host(host: str) -> Optional[Tuple[str, str]]:
    """Resolves a request hostname to (service name, workspace).

    Returns None for a hostname SkyPilot does not serve, which the
    authorization endpoint treats as a denial rather than a pass-through.
    """
    # Strip any port; Host headers may carry one.
    hostname = host.split(':', 1)[0].strip().rstrip('.').lower()
    if not hostname:
        return None
    entry = _load(_ENDPOINT_HOSTS_KEY).get(hostname)
    if not isinstance(entry, dict):
        return None
    service = entry.get('service')
    workspace = entry.get('workspace')
    if not service or not workspace:
        return None
    return str(service), str(workspace)
