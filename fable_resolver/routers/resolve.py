import logging
import uuid

from fable_client import BrokerClient, PPRLClient, FableError
from fable_model import (
    ClientCreationRequest,
    ClientMatchSession,
    PseudonymsSubmissionRequest,
    EntityTransformRequest,
    EntityMaskRequest,
    ClientSubmissionRequest,
    MetaBitVectorEntity,
    ResultsFetchingRequest,
    ClientResultRequest,
    PseudonymMatchResponse,
    BitVectorMetadata,
)
from fastapi import APIRouter, status, Depends, HTTPException
from pydantic import SecretStr
from redis import Redis

from fable_resolver.data_sources import DataSourceBase
from fable_resolver.dependencies import (
    get_broker_client,
    get_pprl_client,
    get_data_source,
    get_redis_client,
    get_session_id,
    get_client_match_session,
)
from fable_resolver.utils import session_hash


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Registers this resolver service as a client for a given match session",
    operation_id="post_register_client",
    response_model=None,
)
async def register_client(
    req: ClientCreationRequest,
    session_key: str = Depends(get_session_id),
    redis_client: Redis = Depends(get_redis_client),
    broker_client: BrokerClient = Depends(get_broker_client),
    data_source: DataSourceBase = Depends(get_data_source),
):
    if redis_client.exists(session_key):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Resolver is already registered for that session.",
        )

    try:
        r = broker_client.get_session(session=req.session.get_secret_value())
    except FableError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Failed to retrieve session information ({e}).",
        ) from e

    for meta_attr in req.metadata_attributes:
        if meta_attr.attribute not in data_source.available_metadata_attributes:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Metadata attribute '{meta_attr.attribute}' is not recognized by the data source.",
            )

    for attr in req.attributes:
        if attr not in data_source.available_attributes:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Attribute '{attr}' is not recognized by the data source.",
            )

    match_session = ClientMatchSession(
        **req.model_dump(),
        client=SecretStr(str(uuid.uuid4())),
        session_expires_at=r.expires_at,
    )

    redis_client.set(session_key, match_session.model_dump_json(), nx=True)

    logger.info("Successfully registered as a client for session %s", session_hash(session_key))


@router.put(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submits pseudonyms to a match session",
    operation_id="put_pseudonyms",
    response_model=None,
)
async def submit_pseudonyms(
    req: PseudonymsSubmissionRequest,
    client_match_session: ClientMatchSession = Depends(get_client_match_session),
    broker_client: BrokerClient = Depends(get_broker_client),
    pprl_client: PPRLClient = Depends(get_pprl_client),
    data_source: DataSourceBase = Depends(get_data_source),
):
    try:
        psn_entities = data_source.resolve(
            psn_list=req.pseudonyms,
            attributes=client_match_session.attributes,
            metadata_attributes=client_match_session.metadata_attributes,
            domain=client_match_session.data_source_domain,
        )
    except (ConnectionError, ValueError) as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Data source failed to resolve pseudonyms ({e}).",
        ) from e
    else:
        entity_to_metadata = {
            e.id: e.metadata + [BitVectorMetadata(name="pseudonym", value=e.pseudonym)] for e in psn_entities
        }

    try:
        entities = pprl_client.transform(
            request=EntityTransformRequest(
                **client_match_session.transformation.model_dump(),
                entities=[e.to_attribute_value_entity() for e in psn_entities],
            )
        ).entities
    except FableError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"PPRL service failed to transform entities ({e}).",
        ) from e
    finally:
        del psn_entities

    try:
        entities = pprl_client.mask(
            request=EntityMaskRequest(
                **client_match_session.masking.model_dump(),
                entities=entities,
            )
        ).entities
    except FableError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"PPRL service failed to mask entities ({e}).",
        ) from e

    try:
        broker_client.submit(
            request=ClientSubmissionRequest(
                session=req.session,
                client=client_match_session.client,
                vectors=[
                    MetaBitVectorEntity(id=e.id, value=e.value, metadata=entity_to_metadata[e.id]) for e in entities
                ],
            )
        )
    except FableError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Failed to submit masked entities to the Broker ({e}).",
        ) from e
    finally:
        del entities, entity_to_metadata

    logger.info(
        "Successfully submitted %s pseudonym(s) to session %s",
        len(req.pseudonyms),
        session_hash(client_match_session.session.get_secret_value()),
    )


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    summary="Returns a list of pseudonymized matches for vectors submitted by this service",
    operation_id="get_pseudonymized_matches",
    response_model=PseudonymMatchResponse,
)
async def fetch_results(
    req: ResultsFetchingRequest,
    client_match_session: ClientMatchSession = Depends(get_client_match_session),
    broker_client: BrokerClient = Depends(get_broker_client),
):
    try:
        r = broker_client.result(
            request=ClientResultRequest(
                session=req.session,
                client=client_match_session.client,
                show_unfinished_results=req.show_unfinished_results,
            )
        )
    except FableError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Failed to fetch results from the Broker ({e}).",
        ) from e

    logger.info(
        "Successfully fetched %s matches for session %s",
        len(r.matches),
        session_hash(client_match_session.session.get_secret_value()),
    )

    return PseudonymMatchResponse(**r.model_dump())
