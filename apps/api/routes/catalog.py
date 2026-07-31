from __future__ import annotations

from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.dependencies.auth import require_internal_service_token
from apps.api.dependencies.database import get_api_session_factory
from apps.api.schemas.catalog import CatalogImportResponse
from talap.catalog import (
    CatalogImportExecutionError,
    MerchantInactiveError,
    MerchantNotFoundError,
    import_catalog_csv,
)
from talap.db.models import CatalogImport, CatalogImportError

router = APIRouter(
    prefix="/api/v1",
    tags=["catalog"],
)

MAX_CATALOG_UPLOAD_BYTES = 5 * 1024 * 1024
_MAX_FILENAME_LENGTH = 255


@router.post(
    "/merchants/{merchant_id}/catalog/import",
    response_model=CatalogImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_catalog(
    merchant_id: UUID,
    response: Response,
    file: UploadFile = File(...),
    _auth: None = Depends(require_internal_service_token),
    _session_factory: async_sessionmaker[AsyncSession] = Depends(get_api_session_factory),
) -> CatalogImportResponse:
    try:
        filename = _validate_upload_filename(file.filename)
        content = await _read_upload(file)
        summary = await import_catalog_csv(
            merchant_id=merchant_id,
            filename=filename,
            content=content,
            session_factory=_session_factory,
        )
    except MerchantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Merchant not found.",
        ) from exc
    except MerchantInactiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Merchant is inactive.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid catalog upload.",
        ) from exc
    except CatalogImportExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Catalog import failed.",
        ) from exc
    finally:
        await file.close()

    response.headers["Location"] = f"/api/v1/catalog/imports/{summary.import_id}"
    return CatalogImportResponse.from_summary(summary)


@router.get(
    "/catalog/imports/{import_id}",
    response_model=CatalogImportResponse,
)
async def get_catalog_import(
    import_id: UUID,
    _auth: None = Depends(require_internal_service_token),
    _session_factory: async_sessionmaker[AsyncSession] = Depends(get_api_session_factory),
) -> CatalogImportResponse:
    async with _session_factory() as session:
        import_record = await session.get(CatalogImport, import_id)
        if import_record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Catalog import not found.",
            )
        error_count = (
            await session.execute(
                select(func.count())
                .select_from(CatalogImportError)
                .where(CatalogImportError.catalog_import_id == import_id)
            )
        ).scalar_one()
    return CatalogImportResponse.from_import_record(import_record, error_count)


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read(MAX_CATALOG_UPLOAD_BYTES + 1)
    if len(content) > MAX_CATALOG_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Catalog file exceeds the 5 MiB upload limit.",
        )
    return content


def _validate_upload_filename(filename: str | None) -> str:
    if filename is None or not filename.strip():
        raise ValueError("filename must not be blank.")
    if len(filename) > _MAX_FILENAME_LENGTH:
        raise ValueError("filename must not exceed 255 characters.")
    if not filename.lower().endswith(".csv"):
        raise ValueError("filename must end with .csv.")
    return filename
