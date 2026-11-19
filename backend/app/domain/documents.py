"""Project documents: upload, versioning, extraction, embedding, download, and search.

Retrieved passages are evidence for agents and users. They are untrusted data: never treat
their text as instructions (spec §7, agents and retrieval). Search is always scoped to the
caller's project and only covers the latest version of each document.
"""

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.adapters.embeddings import Embedder
from app.adapters.extraction import (
    STORED_ONLY_TYPES,
    SUPPORTED_TYPES,
    ExtractionError,
    ExtractionResult,
    Passage,
    _split_long,
    content_type_for,
    extract,
)
from app.adapters.personal_data import mask_text
from app.adapters.storage import ObjectStore, object_key
from app.adapters.vision import ImageDescriber, VisionError
from app.domain.access import NotFoundInScope, Permission, ProjectScope
from app.domain.tenancy import InvalidOperation
from app.persistence.documents import (
    EMBEDDING_DIMENSIONS,
    Document,
    DocumentPassage,
    DocumentVersion,
    VersionStatus,
)


@dataclass(frozen=True)
class SearchHit:
    document_id: uuid.UUID
    document_title: str
    version_id: uuid.UUID
    version_no: int
    filename: str
    passage_id: uuid.UUID
    text: str
    location: dict[str, Any]
    score: float  # cosine similarity, 1 = identical direction


def _actor_user(scope: ProjectScope) -> uuid.UUID:
    return scope.actor.on_behalf_of or scope.actor.id


def _clean_filename(name: str) -> str:
    # Display only: drop any path components a browser sent and limit the length.
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip() or "upload"
    return base[:255]


# --- Upload ---------------------------------------------------------------------------------


def upload(
    session: Session,
    scope: ProjectScope,
    store: ObjectStore,
    embedder: Embedder,
    *,
    filename: str,
    declared_type: str | None,
    data: bytes,
    max_bytes: int,
    title: str | None = None,
    document_id: uuid.UUID | None = None,
    describer: ImageDescriber | None = None,
) -> DocumentVersion:
    """Store a new document, or a new version of an existing one, then extract and embed it.

    The file and its metadata are always kept. Extraction or embedding problems are recorded
    on the version's status instead of failing the upload.
    """
    scope.require(Permission.PROJECT_EDIT)
    if not data:
        raise InvalidOperation("the file is empty")
    if len(data) > max_bytes:
        raise InvalidOperation(f"the file is larger than {max_bytes // (1024 * 1024)} MB")
    filename = _clean_filename(filename)
    content_type = content_type_for(filename, declared_type)
    if content_type not in SUPPORTED_TYPES and content_type not in STORED_ONLY_TYPES:
        raise InvalidOperation(f"unsupported file type ({content_type})")
    digest = hashlib.sha256(data).hexdigest()

    if document_id is None:
        document = Document(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            title=(title or filename).strip()[:255],
            created_by=_actor_user(scope),
        )
        session.add(document)
        session.flush()
        version_no = 1
    else:
        document = get_document(session, scope, document_id)
        latest = _latest_version(session, document.id)
        if latest is not None and latest.sha256 == digest:
            raise InvalidOperation("this file is identical to the current version")
        version_no = (latest.version_no if latest else 0) + 1

    key = object_key(scope.tenant_id, scope.project_id, document.id, version_no, filename)
    version = DocumentVersion(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        document_id=document.id,
        version_no=version_no,
        storage_key=key,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=digest,
        status=VersionStatus.PROCESSING,
        notes=[],
        uploaded_by=_actor_user(scope),
    )
    session.add(version)
    session.flush()
    # If the database transaction later rolls back, this object is orphaned but unreachable,
    # because only database rows reference keys.
    store.put(key, data, content_type)
    _process(session, version, data, embedder, describer)
    return version


IMAGE_NOTE = "[Machine transcription of an image. Verify important figures against the original.]"


def _transcribe(data: bytes, version: DocumentVersion, describer: ImageDescriber | None):
    """Image -> located passages via a vision model. Returns (passages, notes)."""
    if describer is None:
        return [], ["Image stored; image reading is not configured, so its content isn't searchable yet."]
    try:
        text = describer.describe(data, version.filename)
    except VisionError as exc:
        return [], [f"Image stored, but it couldn't be read: {exc}"]
    passages = [
        Passage(f"{IMAGE_NOTE}\n{piece}", {"image": "transcription", "char_start": start, "char_end": end})
        for piece, start, end in _split_long(text, 0)
    ]
    return passages, [f"Text transcribed from the image by {describer.model}; it may contain reading errors."]


def _process(
    session: Session,
    version: DocumentVersion,
    data: bytes,
    embedder: Embedder,
    describer: ImageDescriber | None = None,
) -> None:
    if version.content_type in STORED_ONLY_TYPES:
        passages_, notes = _transcribe(data, version, describer)
        result = ExtractionResult(passages=passages_, notes=notes)
    else:
        try:
            result = extract(data, version.content_type, version.filename)
        except ExtractionError as exc:
            version.status = VersionStatus.EXTRACTION_FAILED
            version.status_detail = str(exc)
            session.flush()
            return
    # Personal data is masked once, here, before passages are stored or embedded: agents, the
    # embedding provider, transcripts and traces only ever see the masked text. The stored file
    # is unchanged and still downloads as uploaded.
    masked = result.masked
    clean: list[Passage] = []
    for p in result.passages:
        text, n = mask_text(p.text)
        masked += n
        clean.append(Passage(text, p.location))
    result.passages = clean
    version.personal_data_masked = masked
    if masked:
        result.notes.append(f"{masked} personal data value(s) masked before the text was stored or searched; "
                            "the original file is unchanged.")
    version.notes = result.notes
    if not result.passages:
        version.status = VersionStatus.STORED_ONLY
        session.flush()
        return

    passages = [
        DocumentPassage(
            tenant_id=version.tenant_id,
            project_id=version.project_id,
            version_id=version.id,
            ordinal=i,
            text=p.text,
            location=p.location,
        )
        for i, p in enumerate(result.passages)
    ]
    session.add_all(passages)
    session.flush()
    _embed(session, version, passages, embedder)


def _embed(session: Session, version: DocumentVersion, passages: list[DocumentPassage], embedder: Embedder) -> None:
    if embedder.dimensions != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"embedder returns {embedder.dimensions} dims but the column holds {EMBEDDING_DIMENSIONS}"
        )
    try:
        vectors = embedder.embed([p.text for p in passages])
    except Exception as exc:  # provider outage or quota: keep the passages, retry later
        version.status = VersionStatus.EMBEDDING_FAILED
        version.status_detail = f"embedding failed: {type(exc).__name__}: {exc}"[:2000]
        session.flush()
        return
    for passage, vector in zip(passages, vectors, strict=True):
        passage.embedding = vector
    version.embedding_model = embedder.model
    version.status = VersionStatus.READY
    version.status_detail = None
    session.flush()


def reembed(session: Session, scope: ProjectScope, embedder: Embedder, version_id: uuid.UUID) -> DocumentVersion:
    """Retry embedding for a version whose embedding step failed."""
    scope.require(Permission.PROJECT_EDIT)
    version = get_version(session, scope, version_id)
    if version.status is not VersionStatus.EMBEDDING_FAILED:
        raise InvalidOperation(f"version is {version.status}, not embedding_failed")
    passages = list(
        session.scalars(
            select(DocumentPassage).where(DocumentPassage.version_id == version.id).order_by(DocumentPassage.ordinal)
        )
    )
    _embed(session, version, passages, embedder)
    return version


def reprocess(
    session: Session,
    version: DocumentVersion,
    store: ObjectStore,
    embedder: Embedder,
    describer: ImageDescriber | None,
) -> DocumentVersion:
    """Re-extract and re-embed one version with the current settings (new embedding model,
    image reading turned on, better extraction). Passages are derived data, so they're
    replaced; the stored file and the version record stay as they are.

    Unscoped: callers must authorize first (see reprocess_version) or be an operator script.
    """
    data = store.get(version.storage_key)
    session.execute(delete(DocumentPassage).where(DocumentPassage.version_id == version.id))
    version.status = VersionStatus.PROCESSING
    version.status_detail = None
    version.embedding_model = None
    session.flush()
    _process(session, version, data, embedder, describer)
    return version


def reprocess_version(
    session: Session,
    scope: ProjectScope,
    store: ObjectStore,
    embedder: Embedder,
    describer: ImageDescriber | None,
    version_id: uuid.UUID,
) -> DocumentVersion:
    scope.require(Permission.PROJECT_EDIT)
    return reprocess(session, get_version(session, scope, version_id), store, embedder, describer)


def needs_reprocessing(version: DocumentVersion, embedder: Embedder, describer: ImageDescriber | None) -> bool:
    """Stale embeddings, failed embeddings, passages extracted before personal data was masked,
    or images that weren't read and now can be."""
    if version.status in (VersionStatus.READY, VersionStatus.EMBEDDING_FAILED):
        return version.embedding_model != embedder.model or version.personal_data_masked is None
    if version.status is VersionStatus.STORED_ONLY:
        return version.content_type in STORED_ONLY_TYPES and describer is not None
    return False


# --- Reading --------------------------------------------------------------------------------


def _latest_version(session: Session, document_id: uuid.UUID) -> DocumentVersion | None:
    return session.scalar(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_no.desc())
        .limit(1)
    )


def get_document(session: Session, scope: ProjectScope, document_id: uuid.UUID) -> Document:
    scope.require(Permission.PROJECT_READ)
    document = session.scalar(
        select(Document).where(
            Document.tenant_id == scope.tenant_id,
            Document.project_id == scope.project_id,
            Document.id == document_id,
        )
    )
    if document is None:
        raise NotFoundInScope("document")
    return document


def list_documents(session: Session, scope: ProjectScope) -> list[tuple[Document, DocumentVersion]]:
    """Each document with its latest version."""
    scope.require(Permission.PROJECT_READ)
    latest_no = (
        select(DocumentVersion.document_id, func.max(DocumentVersion.version_no).label("n"))
        .where(DocumentVersion.project_id == scope.project_id)
        .group_by(DocumentVersion.document_id)
        .subquery()
    )
    rows = session.execute(
        select(Document, DocumentVersion)
        .join(latest_no, latest_no.c.document_id == Document.id)
        .join(
            DocumentVersion,
            (DocumentVersion.document_id == Document.id) & (DocumentVersion.version_no == latest_no.c.n),
        )
        .where(Document.tenant_id == scope.tenant_id, Document.project_id == scope.project_id)
        .order_by(Document.title)
    )
    return [(d, v) for d, v in rows]


def list_versions(session: Session, scope: ProjectScope, document_id: uuid.UUID) -> list[DocumentVersion]:
    get_document(session, scope, document_id)
    return list(
        session.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.project_id == scope.project_id, DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_no.desc())
        )
    )


def get_version(session: Session, scope: ProjectScope, version_id: uuid.UUID) -> DocumentVersion:
    scope.require(Permission.PROJECT_READ)
    version = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.tenant_id == scope.tenant_id,
            DocumentVersion.project_id == scope.project_id,
            DocumentVersion.id == version_id,
        )
    )
    if version is None:
        raise NotFoundInScope("document version")
    return version


def read_content(
    session: Session, scope: ProjectScope, store: ObjectStore, version_id: uuid.UUID
) -> tuple[DocumentVersion, bytes]:
    """Authorize, then read the stored file. The storage key never leaves the server."""
    version = get_version(session, scope, version_id)
    return version, store.get(version.storage_key)


def list_passages(session: Session, scope: ProjectScope, version_id: uuid.UUID) -> list[DocumentPassage]:
    version = get_version(session, scope, version_id)
    return list(
        session.scalars(
            select(DocumentPassage)
            .where(DocumentPassage.version_id == version.id)
            .order_by(DocumentPassage.ordinal)
        )
    )


# --- Search ---------------------------------------------------------------------------------


def search(
    session: Session,
    scope: ProjectScope,
    embedder: Embedder,
    query: str,
    *,
    limit: int = 8,
    document_ids: list[uuid.UUID] | None = None,
) -> list[SearchHit]:
    """Nearest passages to `query`, from the latest ready version of each project document."""
    scope.require(Permission.PROJECT_READ)
    if not query.strip():
        raise InvalidOperation("query is empty")
    [vector] = embedder.embed([query])

    latest_no = (
        select(DocumentVersion.document_id, func.max(DocumentVersion.version_no).label("n"))
        .where(DocumentVersion.tenant_id == scope.tenant_id, DocumentVersion.project_id == scope.project_id)
        .group_by(DocumentVersion.document_id)
        .subquery()
    )
    distance = DocumentPassage.embedding.cosine_distance(vector)
    query_ = (
        select(DocumentPassage, DocumentVersion, Document, distance.label("distance"))
        .join(
            DocumentVersion,
            (DocumentVersion.id == DocumentPassage.version_id)
            & (DocumentVersion.project_id == DocumentPassage.project_id),
        )
        .join(latest_no, (latest_no.c.document_id == DocumentVersion.document_id) & (latest_no.c.n == DocumentVersion.version_no))
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentPassage.tenant_id == scope.tenant_id,
            DocumentPassage.project_id == scope.project_id,
            DocumentPassage.embedding.is_not(None),
            DocumentVersion.status == VersionStatus.READY,
            # Vectors from a different model aren't comparable with this query's vector.
            DocumentVersion.embedding_model == embedder.model,
        )
        .order_by(distance)
        .limit(max(1, min(limit, 50)))
    )
    if document_ids:
        query_ = query_.where(Document.id.in_(document_ids))
    return [
        SearchHit(
            document_id=d.id,
            document_title=d.title,
            version_id=v.id,
            version_no=v.version_no,
            filename=v.filename,
            passage_id=p.id,
            text=p.text,
            location=p.location,
            score=round(1.0 - float(dist), 4),
        )
        for p, v, d, dist in session.execute(query_)
    ]
