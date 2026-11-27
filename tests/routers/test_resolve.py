import datetime
import uuid
import random

import pytest
from fable_model import (
    ClientCreationRequest,
    BaseTransformRequest,
    BaseMaskRequest,
    TransformConfig,
    EmptyValueHandling,
    MaskConfig,
    CLKFilter,
    HashConfig,
    HashFunction,
    HashAlgorithm,
    DoubleHash,
    StaticAttributeConfig,
    ClientMatchSession,
    GlobalTransformerConfig,
    NormalizationTransformer,
    MetadataAttribute,
    PseudonymsSubmissionRequest,
    ResultsFetchingRequest,
    PseudonymMatchResponse,
    BitVectorMetadata,
)
from fable_client import FableError
from fastapi import status
from pydantic import SecretStr, ValidationError, BaseModel
from httpx2 import Response, Request
from mosaic_client.epix import Identity

from fable_resolver.config import Settings
from fable_resolver.dependencies import get_settings
from tests.helpers import detail_of, random_string, assert_eventually


def _register_client(test_client, session, domain_name, attributes=None, metadata_attributes=None) -> Response:
    if attributes is None:
        attributes = [StaticAttributeConfig(attribute_name="first_name")]
    if metadata_attributes is None:
        metadata_attributes = []

    return test_client.post(
        "/resolve/register",
        json=ClientCreationRequest(
            session=session,
            data_source_domain=domain_name,
            transformation=BaseTransformRequest(
                config=TransformConfig(
                    empty_value=EmptyValueHandling.skip,
                ),
                global_transformers=GlobalTransformerConfig(
                    after=[NormalizationTransformer()],
                ),
            ),
            masking=BaseMaskRequest(
                config=MaskConfig(
                    token_size=2,
                    filter=CLKFilter(
                        hash_values=5,
                        filter_size=1_000,
                    ),
                    hash=HashConfig(
                        function=HashFunction(
                            algorithms=[HashAlgorithm.sha1],
                        ),
                        strategy=DoubleHash(),
                    ),
                ),
                attributes=attributes,
            ),
            metadata_attributes=metadata_attributes,
        ).model_dump(),
    )


def _create_pseudonym(epix_client, gpas_client, domain_name, epix_source, first_name) -> str:
    mpi = epix_client.request_mpi(
        domain_name=domain_name,
        source_name=epix_source,
        identity=Identity(
            first_name=first_name,  # Only the first name is being masked.
            last_name=random_string(),
            birth_date=datetime.datetime.now(tz=datetime.UTC),
            gender=random.choice(["m", "f"]),
        ),
    ).person.mpi()
    return gpas_client.get_or_create_pseudonyms_for(domain_name=domain_name, value=mpi).pseudonym


def test_register_client_200(test_client, match_session, domain_name, redis_client, settings):
    r = _register_client(test_client, match_session.session, domain_name)

    assert r.status_code == status.HTTP_201_CREATED, r.text

    raw = redis_client.get(settings.redis.session_key(match_session.session))

    assert raw is not None

    client_match_session = ClientMatchSession.model_validate_json(raw)

    assert client_match_session.session_expires_at == match_session.expires_at

    uuid.UUID(client_match_session.client.get_secret_value())  # Check if it is a UUID.


def test_register_client_400_unrecognized_attribute(test_client, match_session, domain_name):
    r = _register_client(
        test_client,
        match_session.session,
        domain_name,
        attributes=[StaticAttributeConfig(attribute_name="foo")],
    )

    assert r.status_code == status.HTTP_400_BAD_REQUEST
    assert detail_of(r) == "Attribute 'foo' is not recognized by the data source."


def test_register_client_400_unrecognized_meta_attribute(test_client, match_session, domain_name):
    r = _register_client(
        test_client,
        match_session.session,
        domain_name,
        metadata_attributes=[MetadataAttribute(name="foo", attribute="bar")],
    )

    assert r.status_code == status.HTTP_400_BAD_REQUEST
    assert detail_of(r) == "Metadata attribute 'bar' is not recognized by the data source."


def test_register_client_400_already_registered(test_client, match_session, domain_name):
    _register_client(test_client, match_session.session, domain_name)
    r = _register_client(test_client, match_session.session, domain_name)

    assert r.status_code == status.HTTP_400_BAD_REQUEST
    assert detail_of(r) == "Resolver is already registered for that session."


def test_register_client_502_broker_error(test_client, domain_name):
    r = _register_client(test_client, SecretStr("dummy-session"), domain_name)

    assert r.status_code == status.HTTP_502_BAD_GATEWAY
    assert "Failed to retrieve session information" in detail_of(r)


def test_submit_pseudonyms_200(test_client, match_session, domain_name, pseudonym):
    r = _register_client(test_client, match_session.session, domain_name)

    assert r.status_code == status.HTTP_201_CREATED

    r = test_client.put(
        "/resolve",
        json=PseudonymsSubmissionRequest(
            session=match_session.session,
            pseudonyms=[pseudonym],
        ).model_dump(),
    )

    assert r.status_code == status.HTTP_202_ACCEPTED, r.text


def test_submit_pseudonyms_400_resolver_not_registered(test_client, pseudonym):
    r = test_client.put(
        "/resolve",
        json=PseudonymsSubmissionRequest(
            session=SecretStr("dummy-session"),
            pseudonyms=[pseudonym],
        ).model_dump(),
    )

    assert r.status_code == status.HTTP_400_BAD_REQUEST
    assert detail_of(r) == "Resolver is not registered to that session yet."


@pytest.mark.parametrize("exception_cls", [ConnectionError, ValueError])
def test_submit_pseudonyms_502_while_resolving(
    monkeypatch,
    test_client,
    match_session,
    domain_name,
    pseudonym,
    mosaic_data_source,
    exception_cls,
):
    r = _register_client(test_client, match_session.session, domain_name)

    assert r.status_code == status.HTTP_201_CREATED

    def _resolve(*_, **__):
        raise exception_cls("dummy exception")

    monkeypatch.setattr(mosaic_data_source, "resolve", _resolve)

    r = test_client.put(
        "/resolve",
        json=PseudonymsSubmissionRequest(
            session=match_session.session,
            pseudonyms=[pseudonym],
        ).model_dump(),
    )

    assert r.status_code == status.HTTP_502_BAD_GATEWAY, r.text
    assert detail_of(r) == "Data source failed to resolve pseudonyms (dummy exception)."


def test_submit_pseudonyms_502_while_transforming(
    monkeypatch,
    test_client,
    match_session,
    domain_name,
    pseudonym,
    pprl_client,
):
    r = _register_client(test_client, match_session.session, domain_name)

    assert r.status_code == status.HTTP_201_CREATED

    def _transform(*_, **__):
        raise FableError(
            "dummy exception",
            request=Request(method="POST", url="dummy"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    monkeypatch.setattr(pprl_client, "transform", _transform)

    r = test_client.put(
        "/resolve",
        json=PseudonymsSubmissionRequest(
            session=match_session.session,
            pseudonyms=[pseudonym],
        ).model_dump(),
    )

    assert r.status_code == status.HTTP_502_BAD_GATEWAY, r.text
    assert detail_of(r) == "PPRL service failed to transform entities (dummy exception)."


def test_submit_pseudonyms_502_while_masking(
    monkeypatch,
    test_client,
    match_session,
    domain_name,
    pseudonym,
    pprl_client,
):
    r = _register_client(test_client, match_session.session, domain_name)

    assert r.status_code == status.HTTP_201_CREATED

    def _mask(*_, **__):
        raise FableError(
            "dummy exception",
            request=Request(method="POST", url="dummy"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    monkeypatch.setattr(pprl_client, "mask", _mask)

    r = test_client.put(
        "/resolve",
        json=PseudonymsSubmissionRequest(
            session=match_session.session,
            pseudonyms=[pseudonym],
        ).model_dump(),
    )

    assert r.status_code == status.HTTP_502_BAD_GATEWAY, r.text
    assert detail_of(r) == "PPRL service failed to mask entities (dummy exception)."


def test_submit_pseudonyms_502_while_submitting_to_broker(
    monkeypatch,
    test_client,
    match_session,
    domain_name,
    pseudonym,
    broker_client,
):
    r = _register_client(test_client, match_session.session, domain_name)

    assert r.status_code == status.HTTP_201_CREATED

    def _submit(*_, **__):
        raise FableError(
            "dummy exception",
            request=Request(method="POST", url="dummy"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    monkeypatch.setattr(broker_client, "submit", _submit)

    r = test_client.put(
        "/resolve",
        json=PseudonymsSubmissionRequest(
            session=match_session.session,
            pseudonyms=[pseudonym],
        ).model_dump(),
    )

    assert r.status_code == status.HTTP_502_BAD_GATEWAY, r.text
    assert detail_of(r) == "Failed to submit masked entities to the Broker (dummy exception)."


def test_submit_pseudonyms_500_while_validating_client_match_session(
    monkeypatch,
    test_client,
    match_session,
    domain_name,
    pseudonym,
):
    r = _register_client(test_client, match_session.session, domain_name)

    assert r.status_code == status.HTTP_201_CREATED

    def _validate(*_, **__):
        raise ValidationError.from_exception_data(title="dummy exception", line_errors=[])

    monkeypatch.setattr(BaseModel, "model_validate_json", _validate)

    r = test_client.put(
        "/resolve",
        json=PseudonymsSubmissionRequest(
            session=match_session.session,
            pseudonyms=[pseudonym],
        ).model_dump(),
    )

    assert r.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR, r.text
    assert detail_of(r) == "Error while validating the client match session."


def test_fetch_results_200(
    test_client,
    epix_client,
    gpas_client,
    match_session,
    domain_name_factory,
    epix_source,
    settings,
    redis_client,
):
    try:
        # Use two different key prefixes to simulate two different resolvers.
        settings2 = Settings(redis={"key_prefix": f"{settings.redis.key_prefix}2"})

        assert settings.redis.key_prefix != settings2.redis.key_prefix

        domain_name1, domain_name2 = domain_name_factory(), domain_name_factory()

        def override_settings1():
            return settings

        def override_settings2():
            return settings2

        r = _register_client(test_client, match_session.session, domain_name1)
        assert r.status_code == status.HTTP_201_CREATED, r.text

        test_client.app.dependency_overrides[get_settings] = override_settings2
        r = _register_client(test_client, match_session.session, domain_name2)
        assert r.status_code == status.HTTP_201_CREATED, r.text

        assert redis_client.get(f"{settings.redis.key_prefix}:{match_session.session.get_secret_value()}") is not None
        assert redis_client.get(f"{settings2.redis.key_prefix}:{match_session.session.get_secret_value()}") is not None

        psn1 = _create_pseudonym(epix_client, gpas_client, domain_name1, epix_source, "foo")
        psn2 = _create_pseudonym(epix_client, gpas_client, domain_name2, epix_source, "foo")

        values1 = gpas_client.get_value_for_list(domain_name=domain_name1, pseudonyms=[psn1])
        values2 = gpas_client.get_value_for_list(domain_name=domain_name2, pseudonyms=[psn2])

        assert len(values1) == 1
        assert len(values2) == 1
        epix_client.get_person_by_mpi(domain_name=domain_name1, mpi_id=values1[0][1])
        epix_client.get_person_by_mpi(domain_name=domain_name2, mpi_id=values2[0][1])

        r = test_client.put(
            "/resolve",
            json=PseudonymsSubmissionRequest(
                session=match_session.session,
                pseudonyms=[psn2],
            ).model_dump(),
        )
        assert r.status_code == status.HTTP_202_ACCEPTED, r.text

        test_client.app.dependency_overrides[get_settings] = override_settings1
        r = test_client.put(
            "/resolve",
            json=PseudonymsSubmissionRequest(
                session=match_session.session,
                pseudonyms=[psn1],
            ).model_dump(),
        )
        assert r.status_code == status.HTTP_202_ACCEPTED, r.text

        def _check_if_matching_has_finished():
            r = test_client.post(
                "/resolve",
                json=ResultsFetchingRequest(session=match_session.session, show_unfinished_results=False).model_dump(),
            )

            assert r.status_code == status.HTTP_200_OK, r.text

            model = PseudonymMatchResponse(**r.json())

            assert model.finished, "Matching hasn't finished yet."

        assert_eventually(_check_if_matching_has_finished)

        r = test_client.post(
            "/resolve",
            json=ResultsFetchingRequest(session=match_session.session, show_unfinished_results=False).model_dump(),
        )

        assert r.status_code == status.HTTP_200_OK, r.text

        model = PseudonymMatchResponse(**r.json())

        assert len(model.matches) == 1

        match = model.matches[0]

        assert match.similarities[0] == 1.0
        assert match.vector.metadata == [BitVectorMetadata(name="pseudonym", value=psn1)]
        assert match.reference_metadata == [BitVectorMetadata(name="pseudonym", value=psn2)]

        # Also check with the other client.
        test_client.app.dependency_overrides[get_settings] = override_settings2
        r = test_client.post(
            "/resolve",
            json=ResultsFetchingRequest(session=match_session.session, show_unfinished_results=False).model_dump(),
        )

        assert r.status_code == status.HTTP_200_OK, r.text

        model = PseudonymMatchResponse(**r.json())

        assert len(model.matches) == 1

        match = model.matches[0]

        assert match.similarities[0] == 1.0
        assert match.vector.metadata == [BitVectorMetadata(name="pseudonym", value=psn2)]
        assert match.reference_metadata == [BitVectorMetadata(name="pseudonym", value=psn1)]
    finally:
        del test_client.app.dependency_overrides[get_settings]


def test_fetch_results_400_resolver_not_registered(test_client, match_session):
    r = test_client.post(
        "/resolve",
        json=ResultsFetchingRequest(session=match_session.session).model_dump(),
    )

    assert r.status_code == status.HTTP_400_BAD_REQUEST, r.text
    assert detail_of(r) == "Resolver is not registered to that session yet."


def test_fetch_results_502_while_fetching_from_broker(
    monkeypatch,
    test_client,
    match_session,
    domain_name,
    broker_client,
):
    r = _register_client(test_client, match_session.session, domain_name)

    assert r.status_code == status.HTTP_201_CREATED

    def _result(*_, **__):
        raise FableError(
            "dummy exception",
            request=Request(method="POST", url="dummy"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    monkeypatch.setattr(broker_client, "result", _result)

    r = test_client.post(
        "/resolve",
        json=ResultsFetchingRequest(session=match_session.session).model_dump(),
    )

    assert r.status_code == status.HTTP_502_BAD_GATEWAY, r.text
    assert detail_of(r) == "Failed to fetch results from the Broker (dummy exception)."


def test_fetch_results_500_while_validating_client_match_session(monkeypatch, test_client, match_session, domain_name):
    r = _register_client(test_client, match_session.session, domain_name)

    assert r.status_code == status.HTTP_201_CREATED

    def _validate(*_, **__):
        raise ValidationError.from_exception_data(title="dummy exception", line_errors=[])

    monkeypatch.setattr(BaseModel, "model_validate_json", _validate)

    r = test_client.post(
        "/resolve",
        json=ResultsFetchingRequest(session=match_session.session).model_dump(),
    )

    assert r.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR, r.text
    assert detail_of(r) == "Error while validating the client match session."
