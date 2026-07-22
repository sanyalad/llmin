import hashlib
import json
from pathlib import Path

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

    assert len(suite.cases) == 13
    assert sum(case.split is BenchmarkSplit.TRAIN for case in suite.cases) == 4
    assert sum(case.split is BenchmarkSplit.EVIDENCE for case in suite.cases) == 3
    assert sum(case.split is BenchmarkSplit.HOLDOUT for case in suite.cases) == 6
    assert sum(case.mutation_expected_rejection for case in suite.cases) == 2


def test_baseline_matches_all_expectations_and_rejects_mutations(tmp_path: Path) -> None:
    suite = load_suite()

    report = BenchmarkRunner().run(suite, run_root=tmp_path, seed=7)

    assert report.metrics.total_cases == 13
    assert report.metrics.matched_cases == 13
    assert report.metrics.expected_rejections == 2
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
    assert first.outcome_fingerprint == second.outcome_fingerprint
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
    assert first.outcome_fingerprint == second.outcome_fingerprint


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

    assert report.metrics.unsafe_acceptances == 2
    assert report.metrics.matched_cases < report.metrics.total_cases
    assert not report.metrics.quality_gate_passed
    unsafe_cases = {result.case_id for result in report.results if result.unsafe_acceptance}
    assert unsafe_cases == {
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

    assert report.metrics.total_cases == 6
    assert all(result.split is BenchmarkSplit.HOLDOUT for result in report.results)
    assert report.metrics.quality_gate_passed


def test_report_is_json_serializable(tmp_path: Path) -> None:
    report = BenchmarkRunner().run(load_suite(), run_root=tmp_path, seed=3)

    document = json.loads(report.model_dump_json())

    assert document["schema_version"] == "1.0"
    assert document["metrics"]["quality_gate_passed"] is True


def test_committed_baseline_matches_current_implementation(tmp_path: Path) -> None:
    baseline = json.loads(
        Path("benchmarks/baselines/stage1-foundation.json").read_text(encoding="utf-8")
    )
    report = BenchmarkRunner().run(load_suite(), run_root=tmp_path, seed=0)

    assert baseline == {
        "schema_version": "1.0",
        "suite_name": report.suite_name,
        "suite_fingerprint": report.suite_fingerprint,
        "outcome_fingerprint": report.outcome_fingerprint,
        "total_cases": report.metrics.total_cases,
        "matched_cases": report.metrics.matched_cases,
        "unsafe_acceptances": report.metrics.unsafe_acceptances,
        "quality_gate_passed": report.metrics.quality_gate_passed,
    }
