from fastapi import APIRouter, Depends

from fable_resolver.data_sources import DataSourceBase
from fable_resolver.dependencies import get_data_source


router = APIRouter()


@router.get(
    "/attributes",
    response_model=list[str],
    summary="Returns the list of attributes supported by the configured data source",
    operation_id="get_data_source_attributes",
)
async def get_available_attributes(
    data_source: DataSourceBase = Depends(get_data_source),
) -> list[str]:
    return data_source.available_attributes


@router.get(
    "/metadata-attributes",
    response_model=list[str],
    summary="Returns the list of metadata attributes supported by the configured data source",
    operation_id="get_data_source_metadata_attributes",
)
async def get_available_metadata_sources(
    data_source: DataSourceBase = Depends(get_data_source),
) -> list[str]:
    return data_source.available_metadata_attributes
