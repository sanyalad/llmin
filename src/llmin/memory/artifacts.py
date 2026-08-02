"""Content-addressed immutable artifact blobs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import tomllib
from pathlib import Path
from threading import Lock

from llmin.domain import normalize_relative_path
from llmin.memory.models import ArtifactBlob
from llmin.observability import redact

_SUPPORTED_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/toml",
        "text/plain",
    }
)
_LOCKS_GUARD = Lock()
_STORE_LOCKS: dict[Path, Lock] = {}


class ArtifactStoreError(ValueError):
    pass


class ContentAddressedArtifactStore:
    def __init__(
        self,
        root: Path,
        *,
        max_blob_bytes: int = 1_048_576,
        max_total_bytes: int = 16_777_216,
    ) -> None:
        if not root.parent.exists() or not root.parent.is_dir():
            raise ArtifactStoreError("artifact store parent must be an existing directory")
        if max_blob_bytes <= 0 or max_total_bytes < max_blob_bytes:
            raise ArtifactStoreError("artifact quotas must be positive and internally consistent")
        root.mkdir(exist_ok=True)
        if not root.is_dir() or self._is_reparse_point(root):
            raise ArtifactStoreError("artifact store root must be a regular directory")
        self.root = root.resolve(strict=True)
        self._max_blob_bytes = max_blob_bytes
        self._max_total_bytes = max_total_bytes
        with _LOCKS_GUARD:
            self._write_lock = _STORE_LOCKS.setdefault(self.root, Lock())

    def put(
        self,
        content: bytes,
        *,
        logical_name: str,
        media_type: str = "text/plain",
    ) -> ArtifactBlob:
        if media_type not in _SUPPORTED_TEXT_MEDIA_TYPES:
            raise ArtifactStoreError("Stage 1 artifact store accepts only allowlisted text media")
        if any(ord(character) < 32 or ord(character) == 127 for character in logical_name):
            raise ArtifactStoreError("artifact logical name cannot contain control characters")
        try:
            logical_name = normalize_relative_path(logical_name)
        except ValueError as error:
            raise ArtifactStoreError(
                "artifact logical name must be normalized and relative"
            ) from error
        if "\x00" in logical_name:
            raise ArtifactStoreError("artifact logical name cannot contain NUL")
        if len(content) > self._max_blob_bytes:
            raise ArtifactStoreError("artifact payload exceeds per-blob quota")
        try:
            text = content.decode("utf-8")
        except UnicodeError as error:
            raise ArtifactStoreError("Stage 1 artifacts must be valid UTF-8") from error
        self._validate_payload(text, media_type)
        digest = hashlib.sha256(content).hexdigest()
        blob = ArtifactBlob(
            sha256=digest,
            size_bytes=len(content),
            media_type=media_type,
            logical_name=logical_name,
        )
        with self._write_lock:
            directory = self.root / digest[:2]
            self._require_safe_path(directory, allow_missing=True)
            directory.mkdir(exist_ok=True)
            self._require_safe_path(directory)
            path = directory / digest
            self._require_safe_path(path, allow_missing=True)
            if path.exists():
                self._verify(path, blob)
                return blob
            if self._stored_bytes() + len(content) > self._max_total_bytes:
                raise ArtifactStoreError("artifact store total quota would be exceeded")

            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=directory,
                    prefix=f".{digest}.",
                    delete=False,
                ) as stream:
                    temporary_path = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    temporary_path.replace(path)
                except FileExistsError:
                    self._verify(path, blob)
                if os.name != "nt":
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                if os.name != "nt":
                    path.chmod(0o400)
            except OSError as error:
                raise ArtifactStoreError(f"artifact write failed: {error}") from error
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink()
        self._verify(path, blob)
        return blob

    def read(self, blob: ArtifactBlob) -> bytes:
        path = self._path(blob.sha256)
        self._require_safe_path(path)
        self._verify(path, blob)
        return path.read_bytes()

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def _require_safe_path(self, path: Path, *, allow_missing: bool = False) -> None:
        candidate = path
        while candidate != self.root:
            if candidate.exists():
                if self._is_reparse_point(candidate):
                    raise ArtifactStoreError(
                        "artifact path cannot contain symlink or reparse point"
                    )
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(self.root):
                    raise ArtifactStoreError("artifact path escapes trusted root")
            elif not allow_missing:
                raise ArtifactStoreError("artifact path does not exist")
            candidate = candidate.parent
        if self._is_reparse_point(self.root):
            raise ArtifactStoreError("artifact store root became a reparse point")

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        if path.is_symlink():
            return True
        try:
            return bool(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except AttributeError:
            return False

    def _stored_bytes(self) -> int:
        total = 0
        for shard in self.root.iterdir():
            if self._is_reparse_point(shard):
                raise ArtifactStoreError("artifact store contains symlink or reparse point")
            if not shard.is_dir():
                raise ArtifactStoreError("artifact store contains an unexpected root entry")
            for blob in shard.iterdir():
                if self._is_reparse_point(blob) or not blob.is_file():
                    raise ArtifactStoreError("artifact store contains an unsafe blob entry")
                total += blob.stat().st_size
        return total

    @staticmethod
    def _validate_payload(text: str, media_type: str) -> None:
        try:
            if media_type == "application/json":
                parsed = json.loads(text)
            elif media_type == "application/toml":
                parsed = tomllib.loads(text)
            else:
                parsed = text
        except (json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
            raise ArtifactStoreError(
                f"artifact does not match declared {media_type} format"
            ) from error
        if redact(parsed) != parsed:
            raise ArtifactStoreError("artifact payload contains data requiring redaction")

    @staticmethod
    def _verify(path: Path, blob: ArtifactBlob) -> None:
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ArtifactStoreError(f"artifact read failed: {error}") from error
        if len(content) != blob.size_bytes or hashlib.sha256(content).hexdigest() != blob.sha256:
            raise ArtifactStoreError("artifact content does not match its address")
