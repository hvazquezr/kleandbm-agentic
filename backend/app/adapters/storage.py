"""Object storage for original document files.

Keys are built by the application from internal identifiers only, so a stored key never
contains anything a client supplied (spec §7: an object-storage path is not proof of
authorization, and identifiers stay independent of storage locations).
"""

import re
import uuid
from pathlib import Path
from typing import Protocol

from app.config import Settings


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


def object_key(
    tenant_id: uuid.UUID, project_id: uuid.UUID, document_id: uuid.UUID, version_no: int, filename: str
) -> str:
    # Keep only the extension, for content sniffing by humans browsing the bucket.
    ext = re.sub(r"[^a-z0-9]", "", filename.rsplit(".", 1)[-1].lower())[:10] if "." in filename else ""
    name = f"original.{ext}" if ext else "original"
    return f"tenants/{tenant_id}/projects/{project_id}/documents/{document_id}/v{version_no}/{name}"


class LocalObjectStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):  # defense in depth; keys are internal
            raise ValueError("key escapes the storage root")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3ObjectStore:
    def __init__(self, bucket: str, prefix: str = "", region: str | None = None) -> None:
        import boto3

        if not bucket:
            raise ValueError("KLEANDBM_S3_BUCKET must be set for the s3 storage backend")
        self.bucket = bucket
        self.prefix = prefix
        self.client = boto3.client("s3", region_name=region or None)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.prefix + key,
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self.prefix + key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self.prefix + key)


def store_from_settings(settings: Settings) -> ObjectStore:
    if settings.storage_backend == "s3":
        return S3ObjectStore(settings.s3_bucket, settings.s3_prefix, settings.s3_region)
    if settings.storage_backend == "local":
        return LocalObjectStore(settings.local_storage_dir)
    raise ValueError(f"unknown storage backend {settings.storage_backend!r}")
