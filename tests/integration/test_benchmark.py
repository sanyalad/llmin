import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from llmin.benchmark import BenchmarkRunner, BenchmarkSplit, BenchmarkSuite
from llmin.domain import Evidence, Postcondition
from llmin.execution import Sandbox
from llmin.verification import VerifierRegistry


def load_suite() -> BenchmarkSuite:
    return BenchmarkSuite.model_validate_json(
        Path("benchmarks/stage1-suite.json").read_text(encoding="utf-8")
    )


def test_stage1_suite_has_required_splits_and_mutations() -> None:
    suite = load_suite()

    assert len(suite.cases) == 17
    assert sum(case.split is BenchmarkSplit.TRAIN for case in suite.cases) == 4
    assert sum(case.split is BenchmarkSplit.EVIDENCE for case in suite.cases) == 3
    assert sum(case.split is BenchmarkSplit.HOLDOUT for case in suite.cases) == 10
    assert sum(case.mutation_expected_rejection for case in suite.cases) == 6


def test_baseline_matches_all_expectations_and_rejects_mutations(tmp_path: Path) -> None:
    suite = load_suite()

    report = BenchmarkRunner().run(suite, run_root=tmp_path, seed=7)

    assert report.metrics.total_cases == 17
    assert report.metrics.matched_cases == 17
    assert report.metrics.mutation_cases == 6
    assert report.metrics.safe_rejections == 6
    assert report.metrics.unsafe_acceptances == 0
    assert report.metrics.llm_calls == 0
    assert report.metrics.quality_gate_passed


def test_functional_outcomes_are_reproducible_for_same_seed(tmp_path: Path) -> None:
    suite = load_suite()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    first = BenchmarkRunner().run(suite, run_root=first_root, seed=42)
    second = BenchmarkRunner().run(suite, run_root=second_root, seed=42)

    assert first.suite_fingerprint == second.suite_fingerprint
    assert first.environment_fingerprint == second.environment_fingerprint
    assert first.observed_outcome_fingerprint == second.observed_outcome_fingerprint
    assert first.evaluation_fingerprint == second.evaluation_fingerprint
    assert [result.case_id for result in first.results] == [
        result.case_id for result in second.results
    ]
    assert [result.task_id for result in first.results] == [
        result.task_id for result in second.results
    ]


def test_outcome_fingerprint_is_independent_of_execution_order(tmp_path: Path) -> None:
    suite = load_suite()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    first = BenchmarkRunner().run(suite, run_root=first_root, seed=0)
    second = BenchmarkRunner().run(suite, run_root=second_root, seed=1)

    assert [result.case_id for result in first.results] != [
        result.case_id for result in second.results
    ]
    assert first.observed_outcome_fingerprint == second.observed_outcome_fingerprint
    assert first.evaluation_fingerprint == second.evaluation_fingerprint


def test_contract_ids_change_when_case_content_changes() -> None:
    suite = load_suite()
    original = suite.cases[0]
    changed = original.model_copy(update={"action_value": 999, "expected_value": 999})

    original_task, original_plan = BenchmarkRunner._build_contracts(suite, original)
    changed_task, changed_plan = BenchmarkRunner._build_contracts(suite, changed)

    assert original_task.task_id != changed_task.task_id
    assert original_plan.plan_id != changed_plan.plan_id
    assert original_plan.actions[0].action_id != changed_plan.actions[0].action_id


def test_observed_fingerprint_is_separate_from_manifest_evaluation(tmp_path: Path) -> None:
    suite = load_suite()
    changed_case = suite.cases[0].model_copy(
        update={
            "expected": type(suite.cases[0].expected).model_validate(
                {
                    **suite.cases[0].expected.model_dump(mode="json"),
                    "final_state": "failed",
                }
            )
        }
    )
    changed_suite = suite.model_copy(update={"cases": (changed_case, *suite.cases[1:])})
    original_root = tmp_path / "original"
    changed_root = tmp_path / "changed"
    original_root.mkdir()
    changed_root.mkdir()

    original = BenchmarkRunner().run(suite, run_root=original_root, seed=0)
    changed = BenchmarkRunner().run(changed_suite, run_root=changed_root, seed=0)

    assert original.observed_outcome_fingerprint == changed.observed_outcome_fingerprint
    assert original.evaluation_fingerprint != changed.evaluation_fingerprint


class _AlwaysPassTomlVerifier:
    postcondition_type = "toml_value_equals"

    def verify(self, postcondition: Postcondition, sandbox: Sandbox) -> Evidence:
        path = str(postcondition.parameters["path"])
        content = sandbox.resolve_read(path).read_bytes()
        return Evidence(
            kind="mutated_verifier",
            locator=path,
            sha256=hashlib.sha256(content).hexdigest(),
            metadata={"mutation": "always_pass"},
        )


def _mutated_registry() -> VerifierRegistry:
    registry = VerifierRegistry()
    registry.register(_AlwaysPassTomlVerifier())
    return registry


def test_quality_gate_detects_always_pass_verifier_mutation(tmp_path: Path) -> None:
    suite = load_suite()
    runner = BenchmarkRunner(verifier_registry_factory=_mutated_registry)

    report = runner.run(suite, run_root=tmp_path, seed=0)

    assert report.metrics.unsafe_acceptances == 6
    assert report.metrics.safe_rejections == 0
    assert report.metrics.matched_cases < report.metrics.total_cases
    assert not report.metrics.quality_gate_passed
    unsafe_cases = {result.case_id for result in report.results if result.unsafe_acceptance}
    assert unsafe_cases == {
        "holdout_mutation_false_vs_zero",
        "holdout_mutation_integer_vs_float",
        "holdout_mutation_string_vs_integer",
        "holdout_mutation_true_vs_one",
        "holdout_mutation_wrong_flag",
        "holdout_mutation_wrong_timeout",
    }


def test_holdout_filter_never_runs_train_or_evidence_cases(tmp_path: Path) -> None:
    report = BenchmarkRunner().run(
        load_suite(),
        run_root=tmp_path,
        seed=1,
        selected_split=BenchmarkSplit.HOLDOUT,
    )

    assert report.metrics.total_cases == 10
    assert all(result.split is BenchmarkSplit.HOLDOUT for result in report.results)
    assert report.metrics.quality_gate_passed


def test_report_is_json_serializable(tmp_path: Path) -> None:
    report = BenchmarkRunner().run(load_suite(), run_root=tmp_path, seed=3)

    document = json.loads(report.model_dump_json())

    assert document["schema_version"] == "1.0"
    assert document["metrics"]["quality_gate_passed"] is True


def test_mutation_case_cannot_use_capability_failure() -> None:
    payload = load_suite().cases[0].model_dump(mode="json")
    payload.update(
        {
            "mode": "capability_error",
            "mutation_expected_rejection": True,
            "action_value": 31,
            "expected_value": 30,
            "expected": {
                "final_state": "failed",
                "execution_success": True,
                "verification_verdict": "failed",
            },
        }
    )

    with pytest.raises(ValidationError, match="must execute patch_toml"):
        type(load_suite().cases[0]).model_validate(payload)


def test_mutation_case_requires_strictly_different_values() -> None:
    payload = load_suite().cases[0].model_dump(mode="json")
    payload.update(
        {
            "mutation_expected_rejection": True,
            "expected": {
                "final_state": "failed",
                "execution_success": True,
                "verification_verdict": "failed",
            },
        }
    )

    with pytest.raises(ValidationError, match="must differ strictly"):
        type(load_suite().cases[0]).model_validate(payload)


def test_committed_baseline_matches_current_implementation(tmp_path: Path) -> None:
    baseline = json.loads(
        Path("benchmarks/baselines/stage1-foundation.json").read_text(encoding="utf-8")
    )
    report = BenchmarkRunner().run(load_suite(), run_root=tmp_path, seed=0)

    assert baseline == {
        "schema_version": "1.0",
        "suite_name": report.suite_name,
        "suite_fingerprint": report.suite_fingerprint,
        "observed_outcome_fingerprint": report.observed_outcome_fingerprint,
        "evaluation_fingerprint": report.evaluation_fingerprint,
        "total_cases": report.metrics.total_cases,
        "matched_cases": report.metrics.matched_cases,
        "mutation_cases": report.metrics.mutation_cases,
        "safe_rejections": report.metrics.safe_rejections,
        "unsafe_acceptances": report.metrics.unsafe_acceptances,
        "quality_gate_passed": report.metrics.quality_gate_passed,
    }
