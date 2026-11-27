import time
from typing import Annotated, Literal

from fable_client import FableError
from fable_model import SessionUpdateRequest, ClientMatchSession
from fastapi import status
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError, BaseModel, Field

from fable_resolver.config import Settings
from fable_resolver.server import app
from fable_resolver.models import AppState
from tests.helpers import assert_eventually
from tests.routers.test_resolve import _register_client


def test_lifespan():
    with TestClient(app):
        assert isinstance(app.state.app_state, AppState)

    assert app.state.app_state.pprl_client._client.is_closed
    assert app.state.app_state.broker_client._client.is_closed


def test_lifespan_unknown_data_source(monkeypatch):
    monkeypatch.setenv("DATA_PROVIDER__SOURCE", "unknown")

    with pytest.raises(ValidationError):
        with TestClient(app):
            pass


def test_lifespan_not_supported_data_source(monkeypatch):
    class DummyConfig(BaseModel):
        source: Literal["dummy"] = "dummy"

    class TestSettings(Settings):
        data_provider: Annotated[DummyConfig, Field(discriminator="source")] = DummyConfig()

    monkeypatch.setattr("fable_resolver.server.Settings", TestSettings)
    monkeypatch.setenv("DATA_PROVIDER__SOURCE", "dummy")

    with pytest.raises(NotImplementedError) as e:
        with TestClient(app):
            pass

    assert str(e.value) == "There is no implementation for data source 'dummy'."


def test_lifespan_unexpired_session_is_not_deleted(monkeypatch, match_session, domain_name, redis_client, settings):
    monkeypatch.setenv("REDIS__CLEANUP_INTERVAL", "2")

    with TestClient(app) as client:
        _register_client(client, match_session.session, domain_name)
        time.sleep(2)  # Be sure that the routine ran once.

        assert redis_client.exists(settings.redis.session_key(session_id=match_session.session))


def test_lifespan_expired_session_is_cleaned_up(
    monkeypatch,
    match_session_factory,
    redis_client,
    domain_name,
    settings,
    broker_client,
):
    monkeypatch.setenv("REDIS__CLEANUP_INTERVAL", "2")

    match_session = match_session_factory(2)  # Match session that expires in 2 seconds.

    with TestClient(app) as client:
        _register_client(client, match_session.session, domain_name)

        def _wait_for_match_session_to_expire():
            try:
                broker_client.get_session(session=match_session.session.get_secret_value())
            except FableError as e:
                assert e.status_code == status.HTTP_404_NOT_FOUND

        assert_eventually(_wait_for_match_session_to_expire)

        def _wait_for_cleanup():  # This is bound to the cleanup interval configured in the Broker.
            assert not redis_client.exists(settings.redis.session_key(session_id=match_session.session))

        assert_eventually(_wait_for_cleanup)


def test_lifespan_session_expired_locally_but_was_extended_at_broker(
    monkeypatch,
    match_session_factory,
    domain_name,
    broker_client,
    redis_client,
    settings,
):
    monkeypatch.setenv("REDIS__CLEANUP_INTERVAL", "2")

    match_session = match_session_factory(2)  # Match session that expires in 2 seconds.

    with TestClient(app) as client:
        _register_client(client, match_session.session, domain_name)  # Resolver fetches session expiration.

        broker_client.refresh_session(
            request=SessionUpdateRequest(
                session=match_session.session,
                token=match_session.token,
            ),
        )

        def _wait_for_update():
            raw = redis_client.get(settings.redis.session_key(session_id=match_session.session))

            assert raw is not None

            updated_match_session = ClientMatchSession.model_validate_json(raw)

            assert match_session.expires_at < updated_match_session.session_expires_at

        assert_eventually(_wait_for_update)
