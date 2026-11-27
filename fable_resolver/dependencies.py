from typing import Annotated

from fable_client import BrokerClient, PPRLClient
from fable_model import BaseSessionModel, ClientMatchSession
from fastapi import Depends, Request, HTTPException, status
from redis import Redis
from pydantic import ValidationError

from fable_resolver.config import Settings
from fable_resolver.data_sources import DataSourceBase
from fable_resolver.models import AppState


def get_app_state(request: Request) -> AppState:
    return request.app.state.app_state


def get_settings(state: Annotated[AppState, Depends(get_app_state)]) -> Settings:
    return state.settings


def get_broker_client(state: Annotated[AppState, Depends(get_app_state)]) -> BrokerClient:
    return state.broker_client


def get_pprl_client(state: Annotated[AppState, Depends(get_app_state)]) -> PPRLClient:
    return state.pprl_client


def get_data_source(state: Annotated[AppState, Depends(get_app_state)]) -> DataSourceBase:
    return state.data_source


def get_redis_client(state: Annotated[AppState, Depends(get_app_state)]) -> Redis:
    return state.redis_client


async def get_session_id(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> str:
    try:
        session = BaseSessionModel.model_validate(await request.json()).session
    except (ValidationError, ValueError):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Request body must contain a valid 'session' key.",
        )

    return settings.redis.session_key(session_id=session)


def get_client_match_session(
    session_key: Annotated[str, Depends(get_session_id)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
) -> ClientMatchSession:
    raw = redis_client.get(session_key)

    if raw is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Resolver is not registered to that session yet.",
        )

    try:
        return ClientMatchSession.model_validate_json(raw)
    except ValidationError as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Error while validating the client match session.",
        ) from e
