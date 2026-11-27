import builtins

from fable_model import MetadataAttribute, PseudonymizedAttributeValueEntity
import pytest

from fable_resolver.config import Settings, MosaicConfig
from fable_resolver.data_sources import MosaicDataSource
from tests.helpers import random_string


TEST_URL = "http://test"
TEST_WSDL_URL = TEST_URL + "?wsdl"


def settings_factory(
    epix_wsdl_url: str = TEST_WSDL_URL,
    epix_management_wsdl_url: str = TEST_WSDL_URL,
    gpas_wsdl_url: str = TEST_WSDL_URL,
    gpas_domain_wsdl_url: str = TEST_WSDL_URL,
) -> Settings:
    return Settings(
        broker_service_base_url=TEST_URL,
        pprl_service_base_url=TEST_URL,
        data_provider=MosaicConfig(
            epix_wsdl_url=epix_wsdl_url,
            epix_management_wsdl_url=epix_management_wsdl_url,
            gpas_wsdl_url=gpas_wsdl_url,
            gpas_domain_wsdl_url=gpas_domain_wsdl_url,
        ),
        _env_file=None,
    )


def test_import_error(monkeypatch):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "mosaic_client":
            raise ModuleNotFoundError("No module named 'mosaic_client'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    with pytest.raises(RuntimeError) as e:
        MosaicDataSource(settings=settings_factory())

    assert "Please install fable-resolver[mosaic]." in str(e.value)


def test_epix_connection_error():
    with pytest.raises(ConnectionError) as e:
        MosaicDataSource(settings=settings_factory())

    assert str(e.value) == "Failed to connect to E-PIX services."


def test_gpas_connection_error(epix_wsdl_url, epix_management_wsdl_url):
    with pytest.raises(ConnectionError) as e:
        MosaicDataSource(
            settings=settings_factory(
                epix_wsdl_url=epix_wsdl_url,
                epix_management_wsdl_url=epix_management_wsdl_url,
                gpas_wsdl_url=TEST_WSDL_URL,
                gpas_domain_wsdl_url=TEST_WSDL_URL,
            ),
        )

    assert str(e.value) == "Failed to connect to gPAS services."


def test_available_metadata_attributes(mosaic_data_source):
    assert len(mosaic_data_source.available_metadata_attributes) > 0


def test_available_attributes(mosaic_data_source):
    assert len(mosaic_data_source.available_attributes) > 0


def test_resolve_gpas_connection_error(monkeypatch, mosaic_data_source):
    with pytest.raises(ConnectionError) as e:
        mosaic_data_source.resolve(psn_list=[random_string()], attributes=[], metadata_attributes=[], domain="")

    assert str(e.value) == "Failed to resolve pseudonyms. Are they correct and is the domain name valid?"


def test_resolve_epix_connection_error(mosaic_data_source, domain_name, gpas_client):
    psn = gpas_client.get_or_create_pseudonyms_for(domain_name=domain_name, value=random_string()).pseudonym

    with pytest.raises(ConnectionError) as e:
        mosaic_data_source.resolve(psn_list=[psn], attributes=[], metadata_attributes=[], domain=domain_name)

    assert str(e.value) == "Failed to resolve MPIs. Have they been correctly pseudonymized?"


def test_resolve_unknown_metadata_source_error(mosaic_data_source, domain_name, pseudonym):
    metadata = MetadataAttribute(name=random_string(), attribute=random_string())

    with pytest.raises(ValueError) as e:
        mosaic_data_source.resolve(
            psn_list=[pseudonym],
            attributes=[],
            metadata_attributes=[metadata],
            domain=domain_name,
        )

    assert str(e.value) == f"Mandatory metadata field '{metadata.name}' is not set."


def test_resolve(mosaic_data_source, epix_client, domain_name, pseudonym, mpi):
    attr, metadata_attr = "last_name", "person_created"
    entities = mosaic_data_source.resolve(
        psn_list=[pseudonym],
        attributes=[attr],
        metadata_attributes=[MetadataAttribute(name=metadata_attr, attribute=metadata_attr)],
        domain=domain_name,
    )

    assert len(entities) == 1

    entity = entities[0]

    assert isinstance(entity, PseudonymizedAttributeValueEntity)
    assert entity.pseudonym == pseudonym
    assert entity.attributes == {
        "last_name": epix_client.get_person_by_mpi(domain_name=domain_name, mpi_id=mpi).reference_identity.last_name,
    }
    assert len(entity.metadata) == 1
    assert entity.metadata[0].name == metadata_attr
