from pathlib import Path

import pytest

from llmin.domain import Postcondition
from llmin.execution import Sandbox
from llmin.verification.verifiers import TomlValueEqualsVerifier, VerificationMismatch


@pytest.mark.parametrize(
    ("toml_value", "expected"),
    [
        ("true", 1),
        ("false", 0),
        ("1", 1.0),
        ('"1"', 1),
    ],
)
def test_toml_verifier_rejects_equal_python_values_with_different_types(
    tmp_path: Path,
    toml_value: str,
    expected: bool | int | float,
) -> None:
    (tmp_path / "config.toml").write_text(f"value = {toml_value}\n", encoding="utf-8")
    sandbox = Sandbox(tmp_path, readable_paths=("config.toml",))
    postcondition = Postcondition(
        type="toml_value_equals",
        parameters={"path": "config.toml", "key": "value", "value": expected},
    )

    with pytest.raises(VerificationMismatch, match="type or value"):
        TomlValueEqualsVerifier().verify(postcondition, sandbox)


@pytest.mark.parametrize("value", [True, False, 1, 1.0, "1"])
def test_toml_verifier_accepts_matching_type_and_value(tmp_path: Path, value: object) -> None:
    serialized = f'"{value}"' if isinstance(value, str) else str(value).lower()
    (tmp_path / "config.toml").write_text(f"value = {serialized}\n", encoding="utf-8")
    sandbox = Sandbox(tmp_path, readable_paths=("config.toml",))
    postcondition = Postcondition(
        type="toml_value_equals",
        parameters={"path": "config.toml", "key": "value", "value": value},
    )

    evidence = TomlValueEqualsVerifier().verify(postcondition, sandbox)

    assert evidence.sha256 is not None
