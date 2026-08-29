# SkyApps plugin

Renders a hostname per SkyServe service and serves Traefik HTTP dynamic config
from the API server. App traffic never crosses the API server.

## Install

```bash
pip install -e skyapps
cp skyapps/plugins.yaml ~/.sky/plugins.yaml   # then edit base_domain
sky api stop; sky api start
```

`SkyAppsPlugin` loads only in the uvicorn worker (`PluginContext.UVICORN`).

## Traefik

```yaml
providers:
  http:
    endpoint: "https://<api-server>/plugins/skyapps/traefik"
    pollInterval: 10s
    headers:
      Authorization: "Bearer ${SKY_SERVICE_ACCOUNT_TOKEN}"
```

Ten seconds is the exposure window after a port is reused. Do not stretch it
to minutes.

Set `backend_host` to the serve controller's in-cluster DNS (or IP) so Traefik
forwards to `:load_balancer_port` rather than to SkyPilot's path-based ingress
URL. Unset, the plugin uses the host from `sky serve status` when that looks
like `host:port`.

## Hostnames

`{service-name}-{workspace or default}.{base_domain}` as a single DNS-1123
label, so one wildcard cert covers every app. Serve records do not currently
carry workspace, so the label uses `default` unless the record has one.

An empty Traefik config is valid and would delete every route Traefik holds.
The handler never serves that: it returns the last good config, or 503 if
there is none.

`sky serve status --endpoint` is rewritten to the hostname. The patch is on
`sky.serve.server.impl.status` and fails open if that symbol moves.
