"""SkyPilot API server plugin that publishes per-service hostnames."""
import functools
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import fastapi

from sky.server import plugins

logger = logging.getLogger(__name__)

# One DNS-1123 label. Single label keeps one wildcard cert valid for every app.
_LABEL = re.compile(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?')
_LABEL_MAX = 63


class SkyAppsPlugin(plugins.BasePlugin):
    """Serve Traefik config and rewrite SkyServe endpoints to hostnames."""

    load_contexts = frozenset({plugins.PluginContext.UVICORN})

    def __init__(self,
                 base_domain: str,
                 scheme: str = 'https',
                 backend_host: Optional[str] = None,
                 backend_scheme: str = 'http'):
        self.base_domain = base_domain
        self.scheme = scheme
        self.backend_host = backend_host
        self.backend_scheme = backend_scheme
        self._last_good: Optional[Dict[str, Any]] = None

    @property
    def name(self) -> Optional[str]:
        return 'skyapps'

    @property
    def rbac_rules(self):
        return [
            ('user',
             plugins.RBACRule(path='/plugins/skyapps/*',
                              method='GET',
                              description='App routing config')),
        ]

    def hostname(self, rec: Dict[str, Any]) -> str:
        label = f"{rec['name']}-{rec.get('workspace') or 'default'}"
        if len(label) > _LABEL_MAX or not _LABEL.fullmatch(label):
            raise ValueError(f'not a DNS-1123 label: {label}')
        return f'{label}.{self.base_domain}'

    def backend_url(self, rec: Dict[str, Any]) -> Optional[str]:
        port = rec.get('load_balancer_port')
        if port is None:
            return None
        if self.backend_host:
            return f'{self.backend_scheme}://{self.backend_host}:{port}'
        endpoint = rec.get('endpoint') or ''
        parsed = urlparse(endpoint if '://' in
                          endpoint else f'http://{endpoint}')
        host = parsed.hostname
        if not host:
            return None
        return f'{self.backend_scheme}://{host}:{port}'

    def render(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        routers: Dict[str, Any] = {}
        services: Dict[str, Any] = {}
        for rec in records:
            key = f"{rec.get('name')}-{rec.get('workspace') or 'default'}"
            try:
                host = self.hostname(rec)
                url = self.backend_url(rec)
            except ValueError as e:
                logger.warning('skipping service %s: %s', rec.get('name'), e)
                continue
            if not url:
                continue
            routers[key] = {'rule': f'Host(`{host}`)', 'service': key}
            services[key] = {'loadBalancer': {'servers': [{'url': url}]}}
        return {'http': {'routers': routers, 'services': services}}

    def _service_status_records(self) -> List[Dict[str, Any]]:
        # pylint: disable=import-outside-toplevel
        from sky.serve.server import impl
        return impl.status()

    def build_router(self) -> fastapi.APIRouter:
        router = fastapi.APIRouter(prefix='/plugins/skyapps')

        @router.get('/traefik')
        async def traefik_config():
            try:
                cfg = self.render(self._service_status_records())
            except Exception:  # pylint: disable=broad-except
                logger.exception('status read failed')
                cfg = None
            # NEVER serve an empty config: it is valid, and it would
            # delete every route Traefik currently holds.
            if not cfg or not cfg['http']['routers']:
                if self._last_good is None:
                    raise fastapi.HTTPException(status_code=503,
                                                detail='no config yet')
                return self._last_good
            self._last_good = cfg
            return cfg

        return router

    def _patch_endpoint_reporting(self) -> None:
        # pylint: disable=import-outside-toplevel
        from sky.serve.server import impl
        original = impl.status

        @functools.wraps(original)
        def patched(*args, **kwargs):
            records = original(*args, **kwargs)
            for rec in records:
                try:
                    if rec.get('load_balancer_port') is not None:
                        rec['endpoint'] = f'{self.scheme}://{self.hostname(rec)}'
                except Exception:  # pylint: disable=broad-except
                    pass  # fail OPEN; never break `sky serve status`
            return records

        impl.status = patched

    def install(self, extension_context: plugins.ExtensionContext):
        if extension_context.app:
            extension_context.app.include_router(self.build_router())
            extension_context.register_rbac_rule(
                path='/plugins/skyapps/*',
                method='GET',
                description='App routing config')
        self._patch_endpoint_reporting()
