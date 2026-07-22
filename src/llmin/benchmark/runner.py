"""Deterministic benchmark materialization, execution, and reporting."""

from __future__ import annotations

import hashlib
import json
import platform
import random
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from llmin import __version__
from llmin.benchmark.models import (
    BenchmarkCase,
    BenchmarkCaseResult,
    BenchmarkMetrics,
    BenchmarkReport,
    BenchmarkSplit,
    BenchmarkSuite,
    CaseMode,
)
from llmin.domain import (
    Action,
    Budget,
    ExecutionPlan,
    PlannerKind,
    Postcondition,
    TaskConstraints,
    TaskSpec,
)
from llmin.execution import CapabilityRegistry, Executor, SandboxFactory
from llmin.observability import InMemoryTraceSink
from llmin.orchestrator import TaskState
from llmin.pipeline import Pipeline
from llmin.planning import FakePlanner
from llmin.verification import VerificationService, VerifierRegistry


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BenchmarkRunner:
    def __init__(
        self,
        *,
        verifier_registry_factory: Callable[[], VerifierRegistry] = (
            VerifierRegistry.with_builtins
        ),
    ) -> None:
        self._verifier_registry_factory = verifier_registry_factory

    def run(
        self,
        suite: BenchmarkSuite,
        *,
        run_root: Path,
        seed: int = 0,
        selected_split: BenchmarkSplit | None = None,
    ) -> BenchmarkReport:
        if not run_root.exists() or not run_root.is_dir():
            raise ValueError("benchmark run_root must be an existing directory")
        cases = [case for case in suite.cases if selected_split in {None, case.split}]
        random.Random(seed).shuffle(cases)
        results = tuple(self._run_case(suite, case, run_root) for case in cases)
        outcome_payload = [
            {
                "case_id": result.case_id,
                "final_state": result.final_state.value,
                "execution_success": result.execution_success,
                "verification_verdict": (
                    result.verification_verdict.value
                    if result.verification_verdict is not None
                    else None
                ),
                "matched_expectation": result.matched_expectation,
                "unsafe_acceptance": result.unsafe_acceptance,
            }
            for result in sorted(results, key=lambda item: item.case_id)
        ]
        matched = sum(result.matched_expectation for result in results)
        unsafe = sum(result.unsafe_acceptance for result in results)
        total_elapsed = sum(result.elapsed_ms for result in results)
        metrics = BenchmarkMetrics(
            total_cases=len(results),
            matched_cases=matched,
            completed_cases=sum(result.final_state is TaskState.COMPLETED for result in results),
            failed_cases=sum(result.final_state is TaskState.FAILED for result in results),
            expected_rejections=sum(result.mutation_expected_rejection for result in results),
            unsafe_acceptances=unsafe,
            mean_latency_ms=total_elapsed / len(results) if results else 0,
            quality_gate_passed=matched == len(results) and unsafe == 0,
        )
        environment = {
            "implementation": platform.python_implementation(),
            "machine": platform.machine(),
            "os": platform.system(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "llmin": __version__,
        }
        return BenchmarkReport(
            suite_name=suite.name,
            suite_fingerprint=_canonical_hash(suite.model_dump(mode="json")),
            environment_fingerprint=_canonical_hash(environment),
            outcome_fingerprint=_canonical_hash(outcome_payload),
            seed=seed,
            selected_split=selected_split,
            results=results,
            metrics=metrics,
        )

    def _run_case(
        self,
        suite: BenchmarkSuite,
        case: BenchmarkCase,
        run_root: Path,
    ) -> BenchmarkCaseResult:
        task, plan = self._build_contracts(suite, case)
        workspace = run_root / task.workspace
        workspace.mkdir(parents=True, exist_ok=False)
        if case.initial_toml:
            (workspace / "config.toml").write_text(case.initial_toml, encoding="utf-8")

        sink = InMemoryTraceSink()
        sandbox_factory = SandboxFactory(run_root)
        executor = Executor(
            CapabilityRegistry.with_builtins(),
            sandbox_factory=sandbox_factory,
            trace_sink=sink,
        )
        verification = VerificationService(
            self._verifier_registry_factory(),
            sandbox_factory=sandbox_factory,
            trace_sink=sink,
        )
        pipeline = Pipeline(
            planner=FakePlanner(lambda _task: plan),
            executor=executor,
            verification=verification,
            trace_sink=sink,
        )
        started = perf_counter()
        result = pipeline.run(task)
        elapsed_ms = (perf_counter() - started) * 1_000
        execution_success = (
            result.execution_report.success if result.execution_report is not None else None
        )
        verification_verdict = (
            result.verification_report.verdict if result.verification_report is not None else None
        )
        matched = (
            result.final_state is case.expected.final_state
            and execution_success is case.expected.execution_success
            and verification_verdict is case.expected.verification_verdict
        )
        unsafe_acceptance = (
            case.mutation_expected_rejection and result.final_state is TaskState.COMPLETED
        )
        return BenchmarkCaseResult(
            case_id=case.case_id,
            split=case.split,
            task_id=task.task_id,
            final_state=result.final_state,
            execution_success=execution_success,
            verification_verdict=verification_verdict,
            matched_expectation=matched,
            mutation_expected_rejection=case.mutation_expected_rejection,
            unsafe_acceptance=unsafe_acceptance,
            trace_events=len(sink.events),
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _build_contracts(
        suite: BenchmarkSuite,
        case: BenchmarkCase,
    ) -> tuple[TaskSpec, ExecutionPlan]:
        namespace = f"https://llmin.local/benchmark/{suite.name}/{case.case_id}"
        task_id = uuid5(NAMESPACE_URL, namespace + "/task")
        plan_id = uuid5(NAMESPACE_URL, namespace + "/plan")
        action_id = uuid5(NAMESPACE_URL, namespace + "/action/1")
        workspace = f"cases/{case.case_id}"

        if case.mode is CaseMode.INCOMPATIBLE:
            capability = "unknown_capability"
            arguments = {}
            constraints = TaskConstraints(
                allowed_capabilities=frozenset({capability}),
            )
            postcondition = Postcondition(
                type="text_equals",
                parameters={"path": "output.txt", "value": "never created"},
            )
        else:
            capability = "patch_toml"
            arguments = {
                "path": "config.toml",
                "key": case.key,
                "value": case.action_value,
            }
            constraints = TaskConstraints(
                readable_paths=("config.toml",),
                writable_paths=("config.toml",),
                allowed_capabilities=frozenset({capability}),
            )
            postcondition = Postcondition(
                type="toml_value_equals",
                parameters={
                    "path": "config.toml",
                    "key": case.key,
                    "value": case.expected_value,
                },
            )

        task = TaskSpec(
            task_id=task_id,
            family="config_patch" if case.mode is not CaseMode.INCOMPATIBLE else "incompatible",
            objective=f"Benchmark case {case.case_id}",
            workspace=workspace,
            inputs={"case_id": case.case_id, "split": case.split.value},
            constraints=constraints,
            postconditions=(postcondition,),
            budget=Budget(max_llm_calls=0, max_cost_usd="0", max_actions=1),
            created_at=suite.created_at,
        )
        plan = ExecutionPlan(
            plan_id=plan_id,
            task_id=task_id,
            planner_kind=PlannerKind.FAKE,
            actions=(
                Action(
                    action_id=action_id,
                    capability=capability,
                    arguments=arguments,
                ),
            ),
        )
        return task, plan
