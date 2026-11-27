from contextlib import asynccontextmanager, ExitStack
import logging
from typing import AsyncIterator
import time

import redis
from fable_client import BrokerClient, PPRLClient, FableError
from fable_model import HealthResponse, ServiceBaseInformation, ClientMatchSession
from fastapi import FastAPI, status
from fastapi_utils.tasks import repeat_every

from fable_resolver.config import Settings, DataSource
from fable_resolver.data_sources import MosaicDataSource
from fable_resolver.models import AppState
from fable_resolver.routers import resolve, data_source
from fable_resolver.version import __version__
from fable_resolver.utils import batch


logger = logging.getLogger(__name__)
expose_docs = Settings().expose_docs


def clear_expired_sessions(
    redis_client: redis.Redis,
    broker_client: BrokerClient,
    s: Settings,
    lock_key: str = "lock:cleanup",
) -> None:
    """Checks all stored client match sessions for their expiration timestamp. If a session is expired, request the
    Broker and check if this session was extended and save the new expiration timestamp. If not, delete the session.

    A lock is created so that only one worker process that acquired the lock first, iterates over all Redis keys and
    does the cleanup. The lock is never released. Instead, its TTL is defined less than the cleanup interval so it gets
    cleaned up by Redis before the next cleanup check is started.
    """
    lock = redis_client.lock(lock_key, timeout=s.redis.lock_timeout, blocking=False)

    if not lock.acquire():
        return

    logger.debug("Running cleanup routine for Redis entries...")

    now = time.time()
    unlinked_counter = 0
    updated_counter = 0

    for keys_batch in batch(redis_client.scan_iter(match=f"{s.redis.key_prefix}:*", count=500)):
        client_match_sessions = [
            ClientMatchSession.model_validate_json(value)
            for value in redis_client.mget(keys_batch)
            if value is not None
        ]

        for session in client_match_sessions:
            if now >= session.session_expires_at:
                session_key = s.redis.session_key(session_id=session.session)

                try:
                    r = broker_client.get_session(session=session.session.get_secret_value())
                except FableError as e:
                    if e.status_code == status.HTTP_404_NOT_FOUND:  # The session does no longer exist at the Broker.
                        redis_client.unlink(session_key)
                        unlinked_counter += 1
                    else:  # Just raise other unexpected errors.
                        raise
                else:  # If the session still exists at the Broker, naively update the expiration timestamp.
                    session.session_expires_at = r.expires_at
                    redis_client.set(session_key, session.model_dump_json(), xx=True)
                    updated_counter += 1

    logger.debug("Successfully updated %s and deleted %s key(s)", updated_counter, unlinked_counter)


@asynccontextmanager
async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
    s = Settings()
    stack = ExitStack()

    try:
        broker_client = BrokerClient(base_url=s.broker_service_base_url)
        stack.callback(broker_client.close)
        assert broker_client.is_healthy, "The Broker is not healthy"

        pprl_client = PPRLClient(base_url=s.pprl_service_base_url)
        stack.callback(pprl_client.close)
        assert pprl_client.is_healthy, "The PPRL service is not healthy"

        if s.data_provider.source == DataSource.mosaic:
            source = MosaicDataSource(settings=s)
        else:
            raise NotImplementedError(f"There is no implementation for data source '{s.data_provider.source}'.")
        logger.info("Connected to the configured %s data source", s.data_provider.source.value)

        stack.callback(source.close)

        redis_pool = redis.ConnectionPool.from_url(
            s.redis.url,
            decode_responses=True,
            max_connections=s.redis.max_connections_per_worker,
            socket_connect_timeout=2,
            socket_timeout=5,
            health_check_interval=30,
        )
        redis_client = redis.Redis(connection_pool=redis_pool)
        stack.callback(redis_client.close)
        stack.callback(redis_pool.close)
        redis_client.ping()

        @repeat_every(seconds=s.redis.cleanup_interval, wait_first=s.redis.cleanup_interval, logger=logger)
        def _cleanup_routine():
            clear_expired_sessions(redis_client, broker_client, s)

        await _cleanup_routine()

        app_.state.app_state = AppState(
            settings=s,
            broker_client=broker_client,
            pprl_client=pprl_client,
            data_source=source,
            redis_client=redis_client,
        )

        logger.info("The Resolver service (v%s) successfully started", __version__)

        yield
    finally:
        stack.close()
        logger.info("The Resolver service successfully stopped")


app = FastAPI(
    title="FABLE Resolver",
    version=__version__,
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "info",
            "description": "Generic information about the state of the pseudonym resolver service.",
        },
        {
            "name": "session",
            "description": "Session management operations handled by the pseudonym resolver service.",
        },
        {
            "name": "data-source",
            "description": "Information about the data source that is configured for the pseudonym resolver service.",
        },
    ],
    openapi_url="/openapi.json" if expose_docs else None,
    docs_url="/docs" if expose_docs else None,
    redoc_url="/redoc" if expose_docs else None,
)


@app.get(
    "/",
    summary="Get basic information about this service",
    operation_id="get_info",
    response_model=ServiceBaseInformation,
    tags=["info"],
)
async def get_info() -> ServiceBaseInformation:
    """Get basic information about this service. Responds with a 200 on success."""
    return ServiceBaseInformation(version=__version__)


@app.get(
    "/health",
    summary="Check service readiness",
    operation_id="get_health",
    response_model=HealthResponse,
    tags=["info"],
)
async def get_health() -> HealthResponse:
    """Check whether the service is ready to process requests. Responds with a 200 on success."""
    return HealthResponse()


app.include_router(
    resolve.router,
    prefix="/resolve",
    tags=["resolve"],
)

app.include_router(
    data_source.router,
    prefix="/data-source",
    tags=["data-source"],
)
