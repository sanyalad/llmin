"""Verifier registry and aggregate verdict construction."""

from __future__ import annotations

from time import perf_counter
from uuid import UUID

from llmin.domain import (
    Evidence,
    TaskSpec,
    VerificationReport,
    VerificationVerdict,
    normalize_relative_path,
)
from llmin.execution.sandbox import SandboxFactory, SandboxPolicyError
from llmin.observability.trace import TraceEvent, TraceSink
from llmin.verification.verifiers import (
    TextEqualsVerifier,
    TomlValueEqualsVerifier,
    VerificationMismatch,
    Verifier,
    VerifierError,
)


class VerifierRegistry:
    def __init__(self) -> None:
        self._verifiers: dict[str, Verifier] = {}

    @classmethod
    def with_builtins(cls) -> VerifierRegistry:
        registry = cls()
        registry.register(TextEqualsVerifier())
        registry.register(TomlValueEqualsVerifier())
        return registry

    def register(self, verifier: Verifier) -> None:
        if verifier.postcondition_type in self._verifiers:
            raise ValueError(f"verifier already registered: {verifier.postcondition_type}")
        self._verifiers[verifier.postcondition_type] = verifier

    def get(self, postcondition_type: str) -> Verifier | None:
        return self._verifiers.get(postcondition_type)


class VerificationService:
    def __init__(
        self,
        registry: VerifierRegistry,
        *,
        sandbox_factory: SandboxFactory,
        trace_sink: TraceSink,
        verifier_version: str = "stage1-v1",
    ) -> None:
        self._registry = registry
        self._sandbox_factory = sandbox_factory
        self._trace_sink = trace_sink
        self._verifier_version = verifier_version

    def verify(
        self,
        *,
        task: TaskSpec,
        trace_id: UUID,
        attempt_id: UUID,
    ) -> VerificationReport:
        started = perf_counter()
        required = frozenset(
            index for index, condition in enumerate(task.postconditions) if condition.required
        )
        readable_paths: list[str] = []
        preparation_errors: list[str] = []
        for index, condition in enumerate(task.postconditions):
            path = condition.parameters.get("path")
            if not isinstance(path, str):
                if condition.required:
                    preparation_errors.append(f"postcondition {index} has no valid path")
                continue
            try:
                readable_paths.append(normalize_relative_path(path))
            except ValueError as error:
                if condition.required:
                    preparation_errors.append(f"postcondition {index}: {error}")

        if preparation_errors:
            return self._report(
                task=task,
                trace_id=trace_id,
                attempt_id=attempt_id,
                required=required,
                covered=frozenset(),
                evidence=(),
                errors=tuple(preparation_errors),
                verdict=VerificationVerdict.INCONCLUSIVE,
                started=started,
            )

        try:
            sandbox = self._sandbox_factory.for_verification(task, tuple(readable_paths))
        except SandboxPolicyError as error:
            return self._report(
                task=task,
                trace_id=trace_id,
                attempt_id=attempt_id,
                required=required,
                covered=frozenset(),
                evidence=(),
                errors=(str(error),),
                verdict=VerificationVerdict.INCONCLUSIVE,
                started=started,
            )

        covered: set[int] = set()
        evidence: list[Evidence] = []
        mismatches: list[str] = []
        internal_errors: list[str] = []
        for index, condition in enumerate(task.postconditions):
            verifier = self._registry.get(condition.type)
            if verifier is None:
                if condition.required:
                    internal_errors.append(
                        f"postcondition {index} has no verifier: {condition.type}"
                    )
                continue
            try:
                item = verifier.verify(condition, sandbox)
                evidence.append(item)
                covered.add(index)
            except VerificationMismatch as error:
                evidence.append(error.evidence)
                covered.add(index)
                if condition.required:
                    mismatches.append(f"postcondition {index}: {error}")
            except VerifierError as error:
                if condition.required:
                    internal_errors.append(f"postcondition {index}: {error}")
            except Exception as error:
                if condition.required:
                    internal_errors.append(
                        "postcondition "
                        f"{index}: unexpected verifier failure ({type(error).__name__})"
                    )

        if mismatches:
            verdict = VerificationVerdict.FAILED
            errors = tuple(mismatches + internal_errors)
        elif internal_errors or not required.issubset(covered):
            verdict = VerificationVerdict.INCONCLUSIVE
            errors = tuple(internal_errors or ["required postconditions are not fully covered"])
        else:
            verdict = VerificationVerdict.PASSED
            errors = ()

        return self._report(
            task=task,
            trace_id=trace_id,
            attempt_id=attempt_id,
            required=required,
            covered=frozenset(covered),
            evidence=tuple(evidence),
            errors=errors,
            verdict=verdict,
            started=started,
        )

    def _report(
        self,
        *,
        task: TaskSpec,
        trace_id: UUID,
        attempt_id: UUID,
        required: frozenset[int],
        covered: frozenset[int],
        evidence: tuple[Evidence, ...],
        errors: tuple[str, ...],
        verdict: VerificationVerdict,
        started: float,
    ) -> VerificationReport:
        report = VerificationReport(
            task_id=task.task_id,
            attempt_id=attempt_id,
            verdict=verdict,
            required_postconditions=required,
            covered_postconditions=covered,
            evidence=evidence,
            errors=errors,
            verifier_version=self._verifier_version,
        )
        self._trace_sink.emit(
            TraceEvent(
                trace_id=trace_id,
                task_id=task.task_id,
                attempt_id=attempt_id,
                event_type="verification.completed",
                payload={
                    "report_id": str(report.report_id),
                    "verdict": report.verdict.value,
                    "covered": sorted(report.covered_postconditions),
                    "required": sorted(report.required_postconditions),
                    "evidence_ids": [str(item.evidence_id) for item in report.evidence],
                    "elapsed_ms": (perf_counter() - started) * 1_000,
                },
            )
        )
        return report
