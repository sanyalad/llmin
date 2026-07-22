import os
import stat
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from llmin.execution import Sandbox, SandboxPolicyError

_PATH_FRAGMENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789/._-",
    min_size=1,
    max_size=30,
)
_TRAVERSAL_LIKE_PATH = st.one_of(
    _PATH_FRAGMENT.map(lambda suffix: f"../{suffix}"),
    _PATH_FRAGMENT.map(lambda prefix: f"{prefix}\\escaped.txt"),
)


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/absolute.txt", "C:/absolute.txt", "a\\b.txt", "."],
)
def test_sandbox_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    sandbox = Sandbox(tmp_path, writable_paths=("allowed.txt",))

    with pytest.raises((ValueError, SandboxPolicyError)):
        sandbox.resolve_read(path)


@given(_TRAVERSAL_LIKE_PATH)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_generated_traversal_like_paths_never_resolve_outside_root(
    tmp_path: Path,
    candidate: str,
) -> None:
    sandbox = Sandbox(tmp_path)

    try:
        resolved = sandbox._resolve(candidate)
    except (ValueError, SandboxPolicyError, OSError):
        return

    assert resolved.is_relative_to(tmp_path.resolve())


def test_write_requires_exact_allowlist_match(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    sandbox = Sandbox(tmp_path, writable_paths=("nested/allowed.txt",))

    assert sandbox.resolve_write("nested/allowed.txt") == tmp_path / "nested" / "allowed.txt"
    with pytest.raises(SandboxPolicyError, match="not allowlisted"):
        sandbox.resolve_write("nested/other.txt")


def test_transaction_rolls_back_modified_and_created_files(tmp_path: Path) -> None:
    original = tmp_path / "original.txt"
    original.write_text("before", encoding="utf-8")
    sandbox = Sandbox(tmp_path, writable_paths=("original.txt", "created.txt"))
    transaction = sandbox.transaction()
    transaction.write_text_atomic("original.txt", "after")
    transaction.write_text_atomic("created.txt", "new")

    transaction.rollback()

    assert original.read_text(encoding="utf-8") == "before"
    assert not (tmp_path / "created.txt").exists()


def test_transaction_commit_returns_hash_manifest(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    sandbox = Sandbox(tmp_path, writable_paths=("target.txt",))
    transaction = sandbox.transaction()
    transaction.write_text_atomic("target.txt", "after")

    changes = transaction.commit()

    assert len(changes) == 1
    assert changes[0].path == "target.txt"
    assert changes[0].kind == "modified"
    assert changes[0].before_sha256 != changes[0].after_sha256


def test_rollback_closes_transaction_and_aggregates_restore_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    sandbox = Sandbox(tmp_path, writable_paths=("target.txt",))
    transaction = sandbox.transaction()
    transaction.write_text_atomic("target.txt", "after")

    def fail_restore(*_args: object, **_kwargs: object) -> None:
        raise SandboxPolicyError("simulated restore failure")

    monkeypatch.setattr(transaction, "_atomic_replace", fail_restore)

    with pytest.raises(SandboxPolicyError, match=r"rollback failed.*simulated restore failure"):
        transaction.rollback()
    with pytest.raises(SandboxPolicyError, match="already closed"):
        transaction.write_text_atomic("target.txt", "again")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
def test_atomic_write_preserves_existing_file_permissions(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o640)
    sandbox = Sandbox(tmp_path, writable_paths=("target.txt",))

    with sandbox.transaction() as transaction:
        transaction.write_text_atomic("target.txt", "after")
        transaction.commit()

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_writes_reject_symlink_components_when_supported(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows configuration")

    sandbox = Sandbox(tmp_path, writable_paths=("link/escaped.txt",))

    with pytest.raises(SandboxPolicyError, match="symlinks"):
        sandbox.resolve_write("link/escaped.txt")
