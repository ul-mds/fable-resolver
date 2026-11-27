from abc import ABC, abstractmethod
from contextlib import ExitStack
from dataclasses import fields
from datetime import datetime
from functools import cached_property
from pydantic.alias_generators import to_snake
from typing import Any
import secrets

from fable_model import PseudonymizedAttributeValueEntity, MetadataAttribute, BitVectorMetadata

from fable_resolver.config import Settings


class DataSourceBase(ABC):
    def __init__(self, settings: Settings):
        self.settings = settings

    @abstractmethod
    def get_available_attributes(self) -> list[str]:
        """Returns the list of attributes supported by this data source."""
        ...

    @cached_property
    def available_attributes(self) -> list[str]:
        return self.get_available_attributes()

    @abstractmethod
    def get_available_metadata_attributes(self) -> list[str]:
        """Returns the list of metadata attributes supported by this data source."""
        ...

    @cached_property
    def available_metadata_attributes(self) -> list[str]:
        return self.get_available_metadata_attributes()

    @abstractmethod
    def resolve(
        self,
        psn_list: list[str],
        attributes: list[str],
        metadata_attributes: list[MetadataAttribute],
        domain: str,
    ) -> list[PseudonymizedAttributeValueEntity]:
        """Resolve pseudonyms into the original data."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Close all clients and connections that are needed during the lifetime of a data source here. This method is
        called during teardown of the application."""
        ...


class MosaicDataSource(DataSourceBase):
    def __init__(self, settings: Settings):
        super().__init__(settings)

        try:
            import mosaic_client
        except ImportError as e:
            raise RuntimeError(
                "The MOSAIC client needs to be installed to use this data source. Please install "
                "fable-resolver[mosaic]."
            ) from e
        else:
            from requests.exceptions import RequestException
            from zeep.exceptions import Error

        self.stack = ExitStack()

        try:
            self._epix = mosaic_client.EPIXClient(
                self._create_wsdl_client(self.settings.data_provider.epix_wsdl_url),
                self._create_wsdl_client(self.settings.data_provider.epix_management_wsdl_url),
            )
        except (Error, RequestException) as e:
            raise ConnectionError("Failed to connect to E-PIX services.") from e

        try:
            self._gpas = mosaic_client.GPASClient(
                self._create_wsdl_client(self.settings.data_provider.gpas_wsdl_url),
                self._create_wsdl_client(self.settings.data_provider.gpas_domain_wsdl_url),
            )
        except (Error, RequestException) as e:
            raise ConnectionError("Failed to connect to gPAS services.") from e

    def _create_wsdl_client(self, wsdl: str):
        from requests import Session
        from zeep import Client
        from zeep.transports import Transport

        session = Session()
        transport = Transport(session=session, timeout=30)

        self.stack.callback(session.close)

        return Client(wsdl, transport=transport)

    def get_available_metadata_attributes(self) -> list[str]:
        from mosaic_client.epix import Person

        return [f.name for f in fields(Person) if f.type == datetime]

    def get_available_attributes(self) -> list[str]:
        from mosaic_client.epix import Identity, Contact

        attrs = [f.name for f in fields(Identity) if f.name not in ("identifiers", "contacts")]
        attrs += [f"contact.{f.name}" for f in fields(Contact)]

        attrs.sort()

        return attrs

    @staticmethod
    def _cast_attribute_value(value: Any) -> str:
        if value is None:
            return ""

        if isinstance(value, (datetime, str)):
            return str(value)

        raise ValueError(f"Unsupported data type for E-PIX identity/contact: {type(value)}.")

    def resolve(
        self,
        psn_list: list[str],
        attributes: list[str],
        metadata_attributes: list[MetadataAttribute],
        domain: str,
    ) -> list[PseudonymizedAttributeValueEntity]:
        from mosaic_client.epix import FullIdentity, Person
        from zeep.exceptions import Fault

        try:
            psn_mpi_pairs: list[tuple[str, str]] = self._gpas.get_value_for_list(domain, psn_list)
            # Temporary mapping of mpi to psn, so that we can do entity id -> mpi -> psn later.
            _mpi_to_psn: dict[str, str] = {pair[1]: pair[0] for pair in psn_mpi_pairs}
            mpi_ids: list[str] = list(_mpi_to_psn.keys())
        except Fault as e:
            raise ConnectionError("Failed to resolve pseudonyms. Are they correct and is the domain name valid?") from e

        try:
            persons: list[Person] = [self._epix.get_person_by_mpi(domain, mpi) for mpi in mpi_ids]
        except Fault as e:
            raise ConnectionError("Failed to resolve MPIs. Have they been correctly pseudonymized?") from e

        contact_prefix: str = "contact."
        results: list[PseudonymizedAttributeValueEntity] = []

        for person in persons:
            identity: FullIdentity = person.reference_identity
            psn: str = _mpi_to_psn[person.mpi()]
            metadata: list[BitVectorMetadata] = []
            attribute_values: dict[str, str] = {}

            # Collect metadata for this person.
            for metadata_attribute in metadata_attributes:
                spec_value = getattr(person, to_snake(metadata_attribute.attribute), None)

                # Check if the value has been set.
                if spec_value is None:
                    raise ValueError(f"Mandatory metadata field '{metadata_attribute.name}' is not set.")

                # If the value is a datetime, convert it to a ISO string.
                if isinstance(spec_value, datetime):
                    spec_value = spec_value.isoformat(timespec="seconds")
                else:
                    spec_value = str(spec_value)

                metadata.append(metadata_attribute.to_metadata(spec_value))

            # Collect attributes from identity.
            for attr_name in attributes:
                # Check if it's a contact attribute.
                if attr_name.startswith(contact_prefix):
                    # Trim off contact prefix.
                    attr_name = attr_name[len(contact_prefix) :]

                    # Iterate over all contacts assigned to this identity.
                    for i, contact in enumerate(identity.contacts):
                        # The key becomes "contact[<index>].<attribute>".
                        contact_attr_name = f"contact[{i}].{attr_name}"
                        attr_value = getattr(contact, to_snake(attr_name), None)
                        attribute_values[contact_attr_name] = self._cast_attribute_value(attr_value)
                else:  # This is for when it's a regular top-level attribute.
                    attr_value = getattr(identity, to_snake(attr_name), None)
                    attribute_values[attr_name] = self._cast_attribute_value(attr_value)

            # Create a new entity with a unique ID.
            results.append(
                PseudonymizedAttributeValueEntity(
                    id=secrets.token_urlsafe(32),
                    attributes=attribute_values,
                    pseudonym=psn,
                    metadata=metadata,
                )
            )

        return results

    def close(self) -> None:
        self.stack.close()
