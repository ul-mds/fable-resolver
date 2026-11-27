from pydantic import BaseModel, ConfigDict

from fable_client import BrokerClient, PPRLClient
from fable_model import ClientMatchSession
from pydantic import SecretStr, Field
from redis import Redis

from fable_resolver.config import Settings
from fable_resolver.data_sources import DataSourceBase


class AppState(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    settings: Settings
    broker_client: BrokerClient
    pprl_client: PPRLClient
    data_source: DataSourceBase
    redis_client: Redis
    session_mapping: dict[SecretStr, ClientMatchSession] = Field(default_factory=dict)
