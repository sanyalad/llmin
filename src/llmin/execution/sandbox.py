"""Filesystem sandbox with exact write allowlists and transactional rollback."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from types import TracebackType

from llmin.domain.models import TaskSpec, normalize_relative_path
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
        readable_paths: tuple[str, ...] = (),
        writable_paths: tuple[str, ...] = (),
        max_file_bytes: int = 1_048_576,
    ) -> None:
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        if not root.exists() or not root.is_dir():
            raise SandboxPolicyError("sandbox root must be an existing directory")

        self.root = root.resolve(strict=True)
        self.readable_paths = frozenset(normalize_relative_path(path) for path in readable_paths)
        self.writable_paths = frozenset(normalize_relative_path(path) for path in writable_paths)
        self.max_file_bytes = max_file_bytes

    def resolve_read(self, relative_path: str) -> Path:
        normalized = normalize_relative_path(relative_path)
        if normalized not in self.readable_paths:
            raise SandboxPolicyError(f"read target is not allowlisted: {normalized}")
        path = self._resolve(normalized)
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


class SandboxFactory:
    """Bind every task sandbox to one trusted base root and its declared workspace."""

    def __init__(self, base_root: Path, *, max_file_bytes: int = 1_048_576) -> None:
        if not base_root.exists() or not base_root.is_dir():
            raise SandboxPolicyError("sandbox base root must be an existing directory")
        self.base_root = base_root.resolve(strict=True)
        self.max_file_bytes = max_file_bytes

    def for_execution(self, task: TaskSpec) -> Sandbox:
        return Sandbox(
            self._task_root(task),
            readable_paths=task.constraints.readable_paths,
            writable_paths=task.constraints.writable_paths,
            max_file_bytes=self.max_file_bytes,
        )

    def for_verification(self, task: TaskSpec, readable_paths: tuple[str, ...]) -> Sandbox:
        return Sandbox(
            self._task_root(task),
            readable_paths=readable_paths,
            writable_paths=(),
            max_file_bytes=self.max_file_bytes,
        )

    def _task_root(self, task: TaskSpec) -> Path:
        relative = normalize_relative_path(task.workspace)
        lexical = self.base_root.joinpath(*relative.split("/"))
        current = self.base_root
        for part in lexical.relative_to(self.base_root).parts:
            current = current / part
            if current.is_symlink():
                raise SandboxPolicyError("symlinks are not allowed in workspace paths")
        resolved = lexical.resolve(strict=False)
        if not resolved.is_relative_to(self.base_root):
            raise SandboxPolicyError("task workspace escapes the sandbox base root")
        if not resolved.exists() or not resolved.is_dir():
            raise SandboxPolicyError("declared task workspace must be an existing directory")
        return resolved


class SandboxTransaction:
    """Record original bytes and make a group of writes rollback-capable."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox
        self._originals: dict[Path, bytes | None] = {}
        self._relative_paths: dict[Path, str] = {}
        self._original_modes: dict[Path, int | None] = {}
        self._closed = False
        self._rolled_back = False

    @property
    def rolled_back(self) -> bool:
        return self._rolled_back

    def __enter__(self) -> SandboxTransaction:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        if self._closed:
            return False
        try:
            self.rollback()
        except SandboxPolicyError as rollback_error:
            if exception is not None:
                raise SandboxPolicyError(
                    f"rollback failed after {exception_type.__name__}: {rollback_error}"
                ) from exception
            raise
        return False

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
            raise SandboxPolicyError(f"write content exceeds {self._sandbox.max_file_bytes} bytes")

        path = self._sandbox.resolve_write(relative_path)
        if path not in self._originals:
            try:
                self._originals[path] = path.read_bytes() if path.exists() else None
                self._original_modes[path] = (
                    stat.S_IMODE(path.stat().st_mode) if path.exists() else None
                )
            except OSError as error:
                raise SandboxPolicyError(f"cannot snapshot {relative_path}: {error}") from error
            self._relative_paths[path] = normalize_relative_path(relative_path)

        self._atomic_replace(path, encoded, mode=self._original_modes[path])

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
        try:
            for path, original in reversed(tuple(self._originals.items())):
                try:
                    if original is None:
                        if path.exists():
                            path.unlink()
                            self._fsync_directory(path.parent)
                    else:
                        self._atomic_replace(
                            path,
                            original,
                            mode=self._original_modes[path],
                        )
                except (OSError, SandboxPolicyError) as error:
                    errors.append(f"{self._relative_paths[path]}: {error}")
        finally:
            self._closed = True
            self._rolled_back = not errors
        if errors:
            raise SandboxPolicyError("rollback failed: " + "; ".join(errors))

    def _atomic_replace(self, path: Path, content: bytes, *, mode: int | None) -> None:
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".llmin-", dir=path.parent)
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if mode is not None:
                os.chmod(temporary_path, mode)
            os.replace(temporary_path, path)
            self._fsync_directory(path.parent)
        except OSError as error:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
            raise SandboxPolicyError(f"atomic write failed for {path.name}: {error}") from error

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxPolicyError("transaction is already closed")

    @staticmethod
    def _require_utf8(encoding: str) -> None:
        if encoding.casefold().replace("-", "") != "utf8":
            raise SandboxPolicyError("only UTF-8 text operations are supported")
