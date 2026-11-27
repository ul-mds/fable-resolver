from enum import StrEnum
from functools import cached_property
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class FrozenBaseModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
    )


class RedisConfig(FrozenBaseModel):
    url: str = "redis://localhost:6379/0"
    key_prefix: str = "fable-resolver"
    max_connections_per_worker: int = Field(default=50, ge=1)
    cleanup_interval: int = Field(default=300, ge=2)

    @cached_property
    def lock_timeout(self):
        """The timeout of the Redis lock is set just below the cleanup interval."""
        return int(0.95 * self.cleanup_interval)

    def session_key(self, session_id: SecretStr) -> str:
        return f"{self.key_prefix}:{session_id.get_secret_value()}"


class DataSource(StrEnum):
    mosaic = "mosaic"


class MosaicConfig(FrozenBaseModel):
    source: Literal[DataSource.mosaic] = DataSource.mosaic
    epix_wsdl_url: str = "http://localhost:8082/epix/epixService?wsdl"
    epix_management_wsdl_url: str = "http://localhost:8082/epix/epixManagementService?wsdl"
    gpas_wsdl_url: str = "http://localhost:8083/gpas/gpasService?wsdl"
    gpas_domain_wsdl_url: str = "http://localhost:8083/gpas/DomainService?wsdl"

    @staticmethod
    def check_for_wsdl(value: str):
        if not value.endswith("?wsdl"):
            raise ValueError("The provided URL does not end with '?wsdl'.")

    @field_validator("epix_wsdl_url")
    @classmethod
    def validate_epix_wsdl_url(cls, v):
        AnyHttpUrl(v)
        cls.check_for_wsdl(v)
        return v

    @field_validator("epix_management_wsdl_url")
    @classmethod
    def validate_epix_management_wsdl_url(cls, v):
        AnyHttpUrl(v)
        cls.check_for_wsdl(v)
        return v

    @field_validator("gpas_wsdl_url")
    @classmethod
    def validate_gpas_wsdl_url(cls, v):
        AnyHttpUrl(v)
        cls.check_for_wsdl(v)
        return v

    @field_validator("gpas_domain_wsdl_url")
    @classmethod
    def validate_gpas_domain_wsdl_url(cls, v):
        AnyHttpUrl(v)
        cls.check_for_wsdl(v)
        return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        frozen=True,
        extra="ignore",
    )

    broker_service_base_url: str = "http://localhost:8081"
    pprl_service_base_url: str = "http://localhost:8080"
    redis: Annotated[RedisConfig, Field(default_factory=RedisConfig)]
    data_provider: Annotated[MosaicConfig, Field(discriminator="source", default_factory=MosaicConfig)]

    expose_docs: bool = True

    @field_validator("broker_service_base_url")
    @classmethod
    def validate_broker_service_base_url(cls, v):
        AnyHttpUrl(v)
        return v

    @field_validator("pprl_service_base_url")
    @classmethod
    def validate_pprl_service_base_url(cls, v):
        AnyHttpUrl(v)
        return v
