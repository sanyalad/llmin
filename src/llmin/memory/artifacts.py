"""Content-addressed immutable artifact blobs."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from llmin.memory.models import ArtifactBlob
from llmin.observability import redact

_SUPPORTED_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/toml",
        "text/plain",
    }
)


class ArtifactStoreError(ValueError):
    pass


class ContentAddressedArtifactStore:
    def __init__(self, root: Path) -> None:
        if not root.parent.exists() or not root.parent.is_dir():
            raise ArtifactStoreError("artifact store parent must be an existing directory")
        root.mkdir(exist_ok=True)
        if not root.is_dir() or root.is_symlink():
            raise ArtifactStoreError("artifact store root must be a regular directory")
        self.root = root.resolve(strict=True)

    def put(
        self,
        content: bytes,
        *,
        logical_name: str,
        media_type: str = "text/plain",
    ) -> ArtifactBlob:
        if media_type not in _SUPPORTED_TEXT_MEDIA_TYPES:
            raise ArtifactStoreError("Stage 1 artifact store accepts only allowlisted text media")
        try:
            text = content.decode("utf-8")
        except UnicodeError as error:
            raise ArtifactStoreError("Stage 1 artifacts must be valid UTF-8") from error
        if redact(text) != text:
            raise ArtifactStoreError("artifact payload contains data requiring redaction")
        digest = hashlib.sha256(content).hexdigest()
        blob = ArtifactBlob(
            sha256=digest,
            size_bytes=len(content),
            media_type=media_type,
            logical_name=logical_name,
        )
        directory = self.root / digest[:2]
        directory.mkdir(exist_ok=True)
        path = directory / digest
        if path.exists():
            self._verify(path, blob)
            return blob

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
        self._verify(path, blob)
        return path.read_bytes()

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    @staticmethod
    def _verify(path: Path, blob: ArtifactBlob) -> None:
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ArtifactStoreError(f"artifact read failed: {error}") from error
        if len(content) != blob.size_bytes or hashlib.sha256(content).hexdigest() != blob.sha256:
            raise ArtifactStoreError("artifact content does not match its address")
