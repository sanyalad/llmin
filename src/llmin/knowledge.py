"""Deterministic contracts for compiled knowledge routing."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from llmin.domain import ContractModel, ExecutionPlan, PlannerKind, TaskSpec
from llmin.memory import (
    ActivationState,
    Applicability,
    ArtifactKind,
    MemoryArtifact,
    MemoryLayer,
)


def task_signature(task: TaskSpec) -> str:
    """Return a stable exact-match signature for reusable task semantics.

    Volatile identity fields, creation time and budgets are intentionally excluded.
    Any change to the requested work, constraints or required outcome produces a miss.
    """

    payload = {
        "family": task.family,
        "objective": task.objective,
        "workspace": task.workspace,
        "inputs": task.inputs,
        "constraints": task.constraints,
        "postconditions": task.postconditions,
        "risk_class": task.risk_class,
    }
    encoded = json.dumps(
        payload,
        default=lambda value: value.model_dump(mode="json")
        if hasattr(value, "model_dump")
        else str(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CompiledSkill(MemoryArtifact):
    """A previously verified execution plan eligible for exact-match reuse."""

    kind: Literal[ArtifactKind.COMPILED_SKILL] = ArtifactKind.COMPILED_SKILL
    layer: Literal[MemoryLayer.PROCEDURAL] = MemoryLayer.PROCEDURAL
    activation_state: ActivationState = ActivationState.CANDIDATE
    task_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan: ExecutionPlan
    source_task_id: UUID
    source_attempt_id: UUID
    source_verification_report_id: UUID

    @model_validator(mode="after")
    def validate_compiled_plan(self) -> "CompiledSkill":
        if self.plan.planner_kind is not PlannerKind.COMPILED:
            raise ValueError("compiled skills must contain a compiled execution plan")
        if self.plan.knowledge_artifact_id != self.artifact_id:
            raise ValueError("compiled plan must reference its owning skill")
        if self.plan.task_id != self.source_task_id:
            raise ValueError("stored compiled plan must remain bound to its source task")
        if self.source_verification_report_id not in self.provenance.verification_report_ids:
            raise ValueError("source verification report must be present in provenance")
        return self


class RouteOutcome(StrEnum):
    HIT = "hit"
    MISS = "miss"
    REJECTED = "rejected"


class RouteDecision(ContractModel):
    outcome: RouteOutcome
    reason: str = Field(min_length=1, max_length=1_000)
    task_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_id: UUID | None = None
    plan: ExecutionPlan | None = None
    llm_calls: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_shape(self) -> "RouteDecision":
        if self.outcome is RouteOutcome.HIT:
            if self.skill_id is None or self.plan is None or self.llm_calls != 0:
                raise ValueError("route hits require skill, plan and zero LLM calls")
            if self.plan.planner_kind is not PlannerKind.COMPILED:
                raise ValueError("route hits must return a compiled plan")
        elif self.skill_id is not None or self.plan is not None:
            raise ValueError("misses and rejections cannot return a skill plan")
        return self


class ExactMatchKnowledgeRouter:
    """Select only active skills whose applicability matches exactly."""

    def route(
        self,
        *,
        task: TaskSpec,
        environment_fingerprint: str,
        skills: tuple[CompiledSkill, ...],
    ) -> RouteDecision:
        signature = task_signature(task)
        family_skills = tuple(skill for skill in skills if skill.applicability.family == task.family)
        if not family_skills:
            return RouteDecision(
                outcome=RouteOutcome.MISS,
                reason="no skill for task family",
                task_signature=signature,
                llm_calls=1,
            )

        for skill in family_skills:
            if self._rejection_reason(
                task=task,
                environment_fingerprint=environment_fingerprint,
                signature=signature,
                skill=skill,
            ) is not None:
                continue
            rebound = skill.plan.model_copy(update={"task_id": task.task_id})
            return RouteDecision(
                outcome=RouteOutcome.HIT,
                reason="active skill matched task and environment exactly",
                task_signature=signature,
                skill_id=skill.artifact_id,
                plan=rebound,
                llm_calls=0,
            )

        return RouteDecision(
            outcome=RouteOutcome.REJECTED,
            reason="candidate skills existed but none passed exact applicability checks",
            task_signature=signature,
            llm_calls=1,
        )

    @staticmethod
    def _rejection_reason(
        *,
        task: TaskSpec,
        environment_fingerprint: str,
        signature: str,
        skill: CompiledSkill,
    ) -> str | None:
        if skill.activation_state is not ActivationState.ACTIVE:
            return "skill is not active"
        if skill.task_signature != signature:
            return "task signature mismatch"
        applicability: Applicability = skill.applicability
        if environment_fingerprint not in applicability.environment_fingerprints:
            return "environment mismatch"
        if not applicability.required_capabilities.issubset(
            task.constraints.allowed_capabilities
        ):
            return "required capability unavailable"
        if applicability.exclusions:
            return "skill has unresolved exclusions"
        return None
