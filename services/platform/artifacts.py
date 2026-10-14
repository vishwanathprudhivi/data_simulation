"""Artifact store: local filesystem (default) or S3-compatible (FR-API-04)."""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path

__all__ = ["ArtifactStore", "LocalArtifactStore", "S3ArtifactStore", "open_artifact_store"]


class ArtifactStore(ABC):
    """Keys are relative POSIX paths like ``jobs/<id>/summary.json``."""

    @abstractmethod
    def put_file(self, key: str, path: Path) -> None: ...

    @abstractmethod
    def get_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def keys(self, prefix: str) -> list[str]: ...

    def put_bytes(self, key: str, data: bytes) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile() as fh:
            fh.write(data)
            fh.flush()
            self.put_file(key, Path(fh.name))

    def put_tree(self, prefix: str, root: Path) -> list[str]:
        keys = []
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            key = f"{prefix.rstrip('/')}/{path.relative_to(root).as_posix()}"
            self.put_file(key, path)
            keys.append(key)
        return keys

    @abstractmethod
    def local_dir(self, prefix: str) -> Path | None:
        """A local directory backing ``prefix`` if the store is local (else None)."""


def _safe(key: str) -> str:
    parts = Path(key).parts
    if key.startswith("/") or ".." in parts:
        raise ValueError(f"invalid artifact key {key!r}")
    return key


class LocalArtifactStore(ArtifactStore):
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / _safe(key)

    def put_file(self, key: str, path: Path) -> None:
        dest = self._path(key)
        if dest.resolve() == Path(path).resolve():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.exists():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())

    def local_dir(self, prefix: str) -> Path | None:
        path = self._path(prefix)
        path.mkdir(parents=True, exist_ok=True)
        return path


class S3ArtifactStore(ArtifactStore):  # pragma: no cover - needs boto3 + an S3 endpoint
    """S3-compatible store (AWS, MinIO). Credentials come from the environment."""

    def __init__(self, bucket: str, prefix: str = "", endpoint_url: str | None = None) -> None:
        import boto3

        self.s3 = boto3.client("s3", endpoint_url=endpoint_url)
        self.bucket, self.prefix = bucket, prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{_safe(key)}" if self.prefix else _safe(key)

    def put_file(self, key: str, path: Path) -> None:
        self.s3.upload_file(str(path), self.bucket, self._key(key))

    def get_bytes(self, key: str) -> bytes:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self._key(key))
        except self.s3.exceptions.NoSuchKey as exc:
            raise KeyError(key) from exc
        return obj["Body"].read()  # type: ignore[no-any-return]

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception:
            return False

    def keys(self, prefix: str) -> list[str]:
        out: list[str] = []
        pages = self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=self._key(prefix)
        )
        strip = len(self.prefix) + 1 if self.prefix else 0
        for page in pages:
            out += [o["Key"][strip:] for o in page.get("Contents", [])]
        return sorted(out)

    def local_dir(self, prefix: str) -> Path | None:
        return None


def open_artifact_store(url: str) -> ArtifactStore:
    if url.startswith("file://"):
        return LocalArtifactStore(Path(url[len("file://") :]))
    if url.startswith("s3://"):  # pragma: no cover
        bucket, _, prefix = url[5:].partition("/")
        import os

        return S3ArtifactStore(bucket, prefix, os.environ.get("SIMCORE_S3_ENDPOINT"))
    raise ValueError(f"unsupported artifact store {url!r}; use file:// or s3://")
