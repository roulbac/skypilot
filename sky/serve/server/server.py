"""Rest APIs for SkyServe."""

import asyncio
import pathlib
import urllib.parse

import fastapi

from sky import sky_logging
from sky.serve import serve_authz
from sky.serve.server import core
from sky.server import stream_utils
from sky.server.blob import blob_storage as bs
from sky.server.requests import executor
from sky.server.requests import payloads
from sky.server.requests import request_names
from sky.server.requests import requests as api_requests
from sky.skylet import constants
from sky.utils import common

logger = sky_logging.init_logger(__name__)
router = fastapi.APIRouter()


@router.post('/up')
async def up(
    request: fastapi.Request,
    up_body: payloads.ServeUpBody,
) -> None:
    await executor.schedule_request_async(
        request_id=request.state.request_id,
        request_name=request_names.RequestName.SERVE_UP,
        request_body=up_body,
        func=core.up,
        schedule_type=api_requests.ScheduleType.LONG,
        request_cluster_name=common.SKY_SERVE_CONTROLLER_NAME,
        auth_user=request.state.auth_user,
    )


@router.post('/update')
async def update(
    request: fastapi.Request,
    update_body: payloads.ServeUpdateBody,
) -> None:
    await executor.schedule_request_async(
        request_id=request.state.request_id,
        request_name=request_names.RequestName.SERVE_UPDATE,
        request_body=update_body,
        func=core.update,
        schedule_type=api_requests.ScheduleType.SHORT,
        request_cluster_name=common.SKY_SERVE_CONTROLLER_NAME,
        auth_user=request.state.auth_user,
    )


@router.post('/down')
async def down(
    request: fastapi.Request,
    down_body: payloads.ServeDownBody,
) -> None:
    await executor.schedule_request_async(
        request_id=request.state.request_id,
        request_name=request_names.RequestName.SERVE_DOWN,
        request_body=down_body,
        func=core.down,
        schedule_type=api_requests.ScheduleType.SHORT,
        request_cluster_name=common.SKY_SERVE_CONTROLLER_NAME,
        auth_user=request.state.auth_user,
    )


@router.post('/terminate-replica')
async def terminate_replica(
    request: fastapi.Request,
    terminate_replica_body: payloads.ServeTerminateReplicaBody,
) -> None:
    await executor.schedule_request_async(
        request_id=request.state.request_id,
        request_name=request_names.RequestName.SERVE_TERMINATE_REPLICA,
        request_body=terminate_replica_body,
        func=core.terminate_replica,
        schedule_type=api_requests.ScheduleType.SHORT,
        request_cluster_name=common.SKY_SERVE_CONTROLLER_NAME,
        auth_user=request.state.auth_user,
    )


@router.get('/authz')
async def authz(request: fastapi.Request) -> fastapi.Response:
    """Forward-auth endpoint for SkyServe wildcard-subdomain endpoints.

    The edge proxy calls this before every request to a service hostname and
    forwards the original request's ``Host``. Authentication has already
    happened in middleware -- via the SSO session cookie for a human, or a
    SkyPilot service account token for a machine -- so this only has to decide
    whether that identity may reach *this* service.

    Deliberately not an ``executor.schedule_request_async`` route: it is on the
    hot path of every request to every service, so it answers inline.

    Returns 200 to allow, 403 to deny. A 401 is produced by the middleware
    when there is no valid identity, which the proxy turns into a login
    redirect.
    """
    # pylint: disable=import-outside-toplevel
    from sky.users import permission

    auth_user = getattr(request.state, 'auth_user', None)
    if auth_user is None or getattr(request.state, 'anonymous_user', False):
        # Should be unreachable: middleware rejects unauthenticated requests
        # before reaching a route. Fail closed regardless.
        return fastapi.Response(status_code=401)

    # The proxy forwards the hostname the browser asked for. ingress-nginx
    # sends the full original URL; other proxies send a host header. Without
    # one we cannot tell which service is being authorized, so deny.
    host = (request.headers.get('X-Forwarded-Host') or
            request.headers.get('X-Original-Host') or '')
    if not host:
        original_url = request.headers.get('X-Original-URL', '')
        if original_url:
            host = urllib.parse.urlparse(original_url).netloc
    if not host:
        logger.warning('SkyServe authz request without a forwarded host; '
                       'denying.')
        return fastapi.Response(status_code=403)

    resolved = serve_authz.resolve_host(host)
    if resolved is None:
        # An unknown hostname under the wildcard domain: no service claims it.
        logger.debug(f'SkyServe authz: no service for host {host!r}; denying.')
        return fastapi.Response(status_code=403)
    service_name, workspace = resolved

    allowed = await asyncio.to_thread(
        permission.permission_service.check_workspace_permission, auth_user.id,
        workspace)
    if not allowed:
        logger.info(f'SkyServe authz: {auth_user.name} denied access to '
                    f'service {service_name!r} in workspace {workspace!r}.')
        return fastapi.Response(status_code=403)

    # Pass the caller's identity to the service, so it never has to
    # authenticate anyone itself.
    return fastapi.Response(status_code=200,
                            headers={
                                'X-Skypilot-User': auth_user.name or '',
                                'X-Skypilot-User-Id': auth_user.id,
                                'X-Skypilot-Workspace': workspace,
                            })


@router.post('/status')
async def status(
    request: fastapi.Request,
    status_body: payloads.ServeStatusBody,
) -> None:
    await executor.schedule_request_async(
        request_id=request.state.request_id,
        request_name=request_names.RequestName.SERVE_STATUS,
        request_body=status_body,
        func=core.status,
        schedule_type=api_requests.ScheduleType.SHORT,
        request_cluster_name=common.SKY_SERVE_CONTROLLER_NAME,
        auth_user=request.state.auth_user,
    )


@router.post('/logs')
async def tail_logs(
    request: fastapi.Request, log_body: payloads.ServeLogsBody,
    background_tasks: fastapi.BackgroundTasks
) -> fastapi.responses.StreamingResponse:
    executor.check_request_thread_executor_available()
    request_task = await executor.prepare_request_async(
        request_id=request.state.request_id,
        request_name=request_names.RequestName.SERVE_LOGS,
        request_body=log_body,
        func=core.tail_logs,
        schedule_type=api_requests.ScheduleType.SHORT,
        request_cluster_name=common.SKY_SERVE_CONTROLLER_NAME,
        auth_user=request.state.auth_user,
    )
    task = executor.execute_request_in_coroutine(request_task)
    # Cancel the coroutine after the request is done or client disconnects
    background_tasks.add_task(task.cancel)
    return stream_utils.stream_response_for_long_request(
        request_id=request_task.request_id,
        logs_path=request_task.log_path,
        background_tasks=background_tasks,
        kill_request_on_disconnect=False,
    )


@router.post('/sync-down-logs')
async def download_logs(
    request: fastapi.Request,
    download_logs_body: payloads.ServeDownloadLogsBody,
) -> None:
    user_hash = download_logs_body.env_vars[constants.USER_ID_ENV_VAR]
    timestamp = sky_logging.get_run_timestamp()
    logs_dir_on_api_server = (
        pathlib.Path(bs.get_blob_storage().download_tmp_dir(user_hash)) /
        'service' / f'{download_logs_body.service_name}_{timestamp}')
    logs_dir_on_api_server.expanduser().mkdir(parents=True, exist_ok=True)
    # We should reuse the original request body, so that the env vars, such as
    # user hash, are kept the same.
    download_logs_body.local_dir = str(logs_dir_on_api_server)
    await executor.schedule_request_async(
        request_id=request.state.request_id,
        request_name=request_names.RequestName.SERVE_SYNC_DOWN_LOGS,
        request_body=download_logs_body,
        func=core.sync_down_logs,
        schedule_type=api_requests.ScheduleType.SHORT,
        request_cluster_name=common.SKY_SERVE_CONTROLLER_NAME,
        auth_user=request.state.auth_user,
    )
