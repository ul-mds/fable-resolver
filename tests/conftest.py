from typing import Iterator, Callable
import uuid

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient
from mosaic_client import EPIXClient, GPASClient
from mosaic_client.epix import Source, IdentifierDomain, Domain as EPIXDomain
from mosaic_client.gpas import Domain as GPASDomain
from fable_client import BrokerClient, PPRLClient, FableError
from fable_model import (
    SessionCreationRequest,
    MatchConfig,
    SimilarityMeasure,
    SessionCreationResponse,
    SessionDeletionRequest,
)
from redis import Redis

from fable_resolver.config import Settings
from fable_resolver.dependencies import get_app_state
from fable_resolver.models import AppState
from fable_resolver.server import app
from fable_resolver.data_sources import MosaicDataSource
from tests.helpers import random_string, random_identity


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def epix_wsdl_url(settings) -> str:
    return settings.data_provider.epix_wsdl_url


@pytest.fixture(scope="session")
def epix_management_wsdl_url(settings) -> str:
    return settings.data_provider.epix_management_wsdl_url


@pytest.fixture(scope="session")
def gpas_wsdl_url(settings) -> str:
    return settings.data_provider.gpas_wsdl_url


@pytest.fixture(scope="session")
def gpas_domain_wsdl_url(settings) -> str:
    return settings.data_provider.gpas_domain_wsdl_url


@pytest.fixture(scope="session")
def pprl_service_url(settings) -> str:
    return settings.pprl_service_base_url


@pytest.fixture(scope="session")
def broker_service_url(settings) -> str:
    return settings.broker_service_base_url


@pytest.fixture(scope="session")
def redis_url(settings) -> str:
    return settings.redis.url


@pytest.fixture(scope="session")
def gpas_client(gpas_wsdl_url, gpas_domain_wsdl_url) -> GPASClient:
    return GPASClient(gpas_wsdl_url, gpas_domain_wsdl_url)


@pytest.fixture(scope="session")
def epix_client(epix_wsdl_url, epix_management_wsdl_url) -> EPIXClient:
    return EPIXClient(epix_wsdl_url, epix_management_wsdl_url)


@pytest.fixture(scope="session")
def pprl_client(pprl_service_url) -> Iterator[PPRLClient]:
    client = PPRLClient(base_url=pprl_service_url)

    yield client

    client.close()


@pytest.fixture(scope="session")
def broker_client(broker_service_url) -> Iterator[BrokerClient]:
    client = BrokerClient(base_url=broker_service_url)

    yield client

    client.close()


def _remove_test_keys_from_redis(redis_client: Redis, settings: Settings):
    keys = list(redis_client.scan_iter(match=f"{settings.redis.key_prefix}*"))
    if len(keys) != 0:
        redis_client.unlink(*keys)


@pytest.fixture(scope="session")
def redis_client(redis_url, settings) -> Iterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    _remove_test_keys_from_redis(client, settings)

    yield client

    _remove_test_keys_from_redis(client, settings)
    client.close()


@pytest.fixture()
def match_session_factory(broker_client) -> Iterator[Callable[[int], SessionCreationResponse]]:
    responses = []

    def _create(expires_in: int):
        r = broker_client.create_session(
            request=SessionCreationRequest(
                session=SecretStr(str(uuid.uuid4())),
                match_config=MatchConfig(
                    measures=[SimilarityMeasure.jaccard],
                    thresholds=[0.8],
                ),
                expires_in=expires_in,
            )
        )

        responses.append(r)

        return r

    yield _create

    try:
        for r in responses:
            broker_client.delete_session(
                request=SessionDeletionRequest(
                    session=r.session,
                    token=r.token,
                )
            )
    except FableError:  # In case the session was already cleaned up by the Broker.
        pass


@pytest.fixture()
def match_session(match_session_factory) -> SessionCreationResponse:
    return match_session_factory(3_600)


@pytest.fixture(scope="session")
def epix_source(epix_client) -> Iterator[str]:
    name = random_string()
    epix_client.add_source(source=Source(name=name))

    yield name

    epix_client.delete_source(source_name=name)


@pytest.fixture(scope="session")
def epix_identifier_domain(epix_client) -> Iterator[str]:
    name = random_string()
    epix_client.add_identifier_domain(identifier_domain=IdentifierDomain(name=name, label=name))

    yield name

    epix_client.delete_identifier_domain(identifier_domain_name=name)


@pytest.fixture(scope="session")
def domain_name_factory(epix_client, gpas_client, epix_source, epix_identifier_domain) -> Iterator[Callable[[], str]]:
    _names = []

    def _factory() -> str:
        name = random_string()
        epix_client.add_domain(
            domain=EPIXDomain(
                name=name,
                label=name,
                mpi_domain=epix_client.get_identifier_domain(identifier_domain_name=epix_identifier_domain),
                safe_source=epix_client.get_source(source_name=epix_source),
            ),
        )
        gpas_client.add_domain(domain=GPASDomain(name=name, label=name))

        _names.append(name)

        return name

    yield _factory

    for n in _names:
        epix_client.delete_domain(domain_name=n, force=True)
        gpas_client.delete_domain(domain_name=n)


@pytest.fixture(scope="session")
def domain_name(domain_name_factory) -> str:
    return domain_name_factory()


@pytest.fixture(scope="session")
def mpi(epix_client, domain_name, epix_source) -> str:
    response = epix_client.request_mpi(domain_name=domain_name, source_name=epix_source, identity=random_identity())
    return response.person.mpi()


@pytest.fixture(scope="session")
def pseudonym(gpas_client, domain_name, mpi) -> str:
    pseudonym = gpas_client.get_or_create_pseudonyms_for(domain_name=domain_name, value=mpi)
    return pseudonym.pseudonym


@pytest.fixture(scope="session")
def mosaic_data_source(settings) -> Iterator[MosaicDataSource]:
    source = MosaicDataSource(settings=settings)

    yield source

    source.close()


@pytest.fixture(scope="session")
def test_client(settings, pprl_client, broker_client, mosaic_data_source, redis_client) -> TestClient:
    def override_app_state():
        return AppState(
            settings=settings,
            broker_client=broker_client,
            pprl_client=pprl_client,
            data_source=mosaic_data_source,
            redis_client=redis_client,
        )

    app.dependency_overrides[get_app_state] = override_app_state

    return TestClient(app)
