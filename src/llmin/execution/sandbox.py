"""Filesystem sandbox with exact write allowlists and transactional rollback."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from llmin.domain.models import normalize_relative_path
from llmin.execution.models import ChangeKind, ChangeRecord


class SandboxPolicyError(ValueError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Sandbox:
    """Resolve portable task paths while rejecting traversal and symlink ambiguity."""

    def __init__(
        self,
        root: Path,
        *,
        writable_paths: tuple[str, ...] = (),
        max_file_bytes: int = 1_048_576,
    ) -> None:
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        if not root.exists() or not root.is_dir():
            raise SandboxPolicyError("sandbox root must be an existing directory")

        self.root = root.resolve(strict=True)
        self.writable_paths = frozenset(normalize_relative_path(path) for path in writable_paths)
        self.max_file_bytes = max_file_bytes

    def resolve_read(self, relative_path: str) -> Path:
        path = self._resolve(relative_path)
        if not path.exists() or not path.is_file():
            raise SandboxPolicyError(f"read target is not an existing file: {relative_path}")
        if path.stat().st_size > self.max_file_bytes:
            raise SandboxPolicyError(f"read target exceeds {self.max_file_bytes} bytes")
        return path

    def resolve_write(self, relative_path: str) -> Path:
        normalized = normalize_relative_path(relative_path)
        if normalized not in self.writable_paths:
            raise SandboxPolicyError(f"write target is not allowlisted: {normalized}")

        path = self._resolve(normalized)
        if not path.parent.exists() or not path.parent.is_dir():
            raise SandboxPolicyError("write target parent must already exist")
        if path.exists() and not path.is_file():
            raise SandboxPolicyError("write target must be a regular file or not yet exist")
        return path

    def transaction(self) -> SandboxTransaction:
        return SandboxTransaction(self)

    def _resolve(self, relative_path: str) -> Path:
        normalized = normalize_relative_path(relative_path)
        lexical = self.root.joinpath(*normalized.split("/"))
        self._reject_symlink_components(lexical)
        resolved = lexical.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise SandboxPolicyError("resolved path escapes the sandbox")
        return resolved

    def _reject_symlink_components(self, path: Path) -> None:
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise SandboxPolicyError("symlinks are not allowed in sandbox paths")


class SandboxTransaction:
    """Record original bytes and make a group of writes rollback-capable."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox
        self._originals: dict[Path, bytes | None] = {}
        self._relative_paths: dict[Path, str] = {}
        self._closed = False

    def read_text(self, relative_path: str, *, encoding: str = "utf-8") -> str:
        self._ensure_open()
        self._require_utf8(encoding)
        path = self._sandbox.resolve_read(relative_path)
        try:
            return path.read_text(encoding=encoding)
        except (OSError, UnicodeError) as error:
            raise SandboxPolicyError(f"cannot read {relative_path}: {error}") from error

    def write_text_atomic(
        self,
        relative_path: str,
        content: str,
        *,
        encoding: str = "utf-8",
    ) -> None:
        self._ensure_open()
        self._require_utf8(encoding)
        encoded = content.encode(encoding)
        if len(encoded) > self._sandbox.max_file_bytes:
            raise SandboxPolicyError(
                f"write content exceeds {self._sandbox.max_file_bytes} bytes"
            )

        path = self._sandbox.resolve_write(relative_path)
        if path not in self._originals:
            try:
                self._originals[path] = path.read_bytes() if path.exists() else None
            except OSError as error:
                raise SandboxPolicyError(f"cannot snapshot {relative_path}: {error}") from error
            self._relative_paths[path] = normalize_relative_path(relative_path)

        self._atomic_replace(path, encoded)

    def commit(self) -> tuple[ChangeRecord, ...]:
        self._ensure_open()
        changes: list[ChangeRecord] = []
        for path, original in self._originals.items():
            try:
                current = path.read_bytes()
            except OSError as error:
                raise SandboxPolicyError(f"cannot inspect committed file: {error}") from error
            if current == original:
                continue
            changes.append(
                ChangeRecord(
                    path=self._relative_paths[path],
                    kind=ChangeKind.CREATED if original is None else ChangeKind.MODIFIED,
                    before_sha256=None if original is None else _sha256(original),
                    after_sha256=_sha256(current),
                    size_bytes=len(current),
                )
            )
        self._closed = True
        return tuple(changes)

    def rollback(self) -> None:
        if self._closed:
            return
        errors: list[str] = []
        for path, original in reversed(tuple(self._originals.items())):
            try:
                if original is None:
                    if path.exists():
                        path.unlink()
                else:
                    self._atomic_replace(path, original)
            except OSError as error:
                errors.append(f"{self._relative_paths[path]}: {error}")
        self._closed = True
        if errors:
            raise SandboxPolicyError("rollback failed: " + "; ".join(errors))

    def _atomic_replace(self, path: Path, content: bytes) -> None:
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".llmin-", dir=path.parent)
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        except OSError as error:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
            raise SandboxPolicyError(f"atomic write failed for {path.name}: {error}") from error

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxPolicyError("transaction is already closed")

    @staticmethod
    def _require_utf8(encoding: str) -> None:
        if encoding.casefold().replace("-", "") != "utf8":
            raise SandboxPolicyError("only UTF-8 text operations are supported")
