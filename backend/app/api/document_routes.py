"""Document upload, versions, passages, download, and search.

Downloads stream through the API after the scope check. Storage keys and bucket URLs are
never returned to clients.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from pydantic import BaseModel, Field

from app.adapters.embeddings import Embedder
from app.adapters.storage import ObjectStore
from app.adapters.vision import ImageDescriber
from app.api.deps import ProjectScopeDep, SessionDep
from app.config import get_settings
from app.domain import documents
from app.persistence.documents import DocumentVersion, VersionStatus

router = APIRouter(prefix="/tenants/{tenant_id}/projects/{project_id}", tags=["documents"])


def get_store(request: Request) -> ObjectStore:
    return request.app.state.object_store


def get_embedder(request: Request) -> Embedder:
    return request.app.state.embedder


def get_describer(request: Request) -> ImageDescriber | None:
    return request.app.state.image_describer


StoreDep = Annotated[ObjectStore, Depends(get_store)]
DescriberDep = Annotated[ImageDescriber | None, Depends(get_describer)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]


class VersionOut(BaseModel):
    id: uuid.UUID
    version_no: int
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: VersionStatus
    status_detail: str | None
    notes: list[str]
    embedding_model: str | None
    uploaded_by: uuid.UUID
    uploaded_at: datetime


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    latest: VersionOut


class PassageOut(BaseModel):
    id: uuid.UUID
    ordinal: int
    text: str
    location: dict[str, Any]


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=8, ge=1, le=50)
    document_ids: list[uuid.UUID] | None = None


class SearchHitOut(BaseModel):
    document_id: uuid.UUID
    document_title: str
    version_id: uuid.UUID
    version_no: int
    filename: str
    passage_id: uuid.UUID
    text: str
    location: dict[str, Any]
    score: float


def _version_out(v: DocumentVersion) -> VersionOut:
    return VersionOut(
        id=v.id, version_no=v.version_no, filename=v.filename, content_type=v.content_type,
        size_bytes=v.size_bytes, sha256=v.sha256, status=v.status, status_detail=v.status_detail,
        notes=list(v.notes or []), embedding_model=v.embedding_model, uploaded_by=v.uploaded_by,
        uploaded_at=v.uploaded_at,
    )


async def _read_limited(file: UploadFile) -> bytes:
    limit = get_settings().max_upload_bytes
    data = await file.read(limit + 1)  # one extra byte detects oversize without reading it all
    return data


@router.get("/documents")
def list_documents(session: SessionDep, scope: ProjectScopeDep) -> list[DocumentOut]:
    return [
        DocumentOut(id=d.id, title=d.title, created_at=d.created_at, latest=_version_out(v))
        for d, v in documents.list_documents(session, scope)
    ]


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    session: SessionDep,
    scope: ProjectScopeDep,
    store: StoreDep,
    embedder: EmbedderDep,
    describer: DescriberDep,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form(max_length=255)] = None,
) -> DocumentOut:
    data = await _read_limited(file)
    version = documents.upload(
        session, scope, store, embedder,
        filename=file.filename or "upload", declared_type=file.content_type, data=data,
        max_bytes=get_settings().max_upload_bytes, title=title, describer=describer,
    )
    document = documents.get_document(session, scope, version.document_id)
    return DocumentOut(id=document.id, title=document.title, created_at=document.created_at, latest=_version_out(version))


@router.post("/documents/{document_id}/versions", status_code=status.HTTP_201_CREATED)
async def upload_version(
    document_id: uuid.UUID,
    session: SessionDep,
    scope: ProjectScopeDep,
    store: StoreDep,
    embedder: EmbedderDep,
    describer: DescriberDep,
    file: Annotated[UploadFile, File()],
) -> VersionOut:
    data = await _read_limited(file)
    version = documents.upload(
        session, scope, store, embedder,
        filename=file.filename or "upload", declared_type=file.content_type, data=data,
        max_bytes=get_settings().max_upload_bytes, document_id=document_id, describer=describer,
    )
    return _version_out(version)


@router.get("/documents/{document_id}/versions")
def list_versions(document_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> list[VersionOut]:
    return [_version_out(v) for v in documents.list_versions(session, scope, document_id)]


@router.get("/document-versions/{version_id}/passages")
def list_passages(version_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep) -> list[PassageOut]:
    return [
        PassageOut(id=p.id, ordinal=p.ordinal, text=p.text, location=p.location)
        for p in documents.list_passages(session, scope, version_id)
    ]


@router.get("/document-versions/{version_id}/content", response_class=Response)
def download(version_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep, store: StoreDep) -> Response:
    version, data = documents.read_content(session, scope, store, version_id)
    return Response(
        content=data,
        media_type=version.content_type,
        headers={
            # Always download, never render inline: uploaded files are untrusted content.
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(version.filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/document-versions/{version_id}/reembed")
def reembed(
    version_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep, embedder: EmbedderDep
) -> VersionOut:
    return _version_out(documents.reembed(session, scope, embedder, version_id))


@router.post("/document-versions/{version_id}/reprocess")
def reprocess(
    version_id: uuid.UUID, session: SessionDep, scope: ProjectScopeDep, store: StoreDep,
    embedder: EmbedderDep, describer: DescriberDep,
) -> VersionOut:
    """Re-extract and re-embed with the current settings, e.g. after switching the embedding
    model or turning on image reading."""
    return _version_out(documents.reprocess_version(session, scope, store, embedder, describer, version_id))


@router.post("/search")
def search(body: SearchIn, session: SessionDep, scope: ProjectScopeDep, embedder: EmbedderDep) -> list[SearchHitOut]:
    return [
        SearchHitOut(**vars(hit))
        for hit in documents.search(
            session, scope, embedder, body.query, limit=body.limit, document_ids=body.document_ids
        )
    ]
