"""Dependency readiness is separate from scientific input prerequisites."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from .readiness_prerequisites import ReadinessPrerequisites
from .schedule_schemas import ScheduleWorker


class ReadinessCheck(BaseModel):
    status: Literal["ok", "failed", "skipped"]
    code: str
    message: str


class SchemaCheck(ReadinessCheck):
    expected: str
    observed: list[str] = Field(default_factory=list)


class WorkerCheck(ReadinessCheck):
    worker: ScheduleWorker | None = None


class SchedulerCheck(ReadinessCheck):
    revision: int | None = None
    applied_revision: int | None = None
    observed_at: AwareDatetime | None = None


class ReadinessChecks(BaseModel):
    database: ReadinessCheck
    schema_check: SchemaCheck = Field(alias="schema")
    artifacts: ReadinessCheck
    worker: WorkerCheck
    scheduler: SchedulerCheck


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    service: Literal["YureAtlas"] = "YureAtlas"
    checked_at: AwareDatetime
    checks: ReadinessChecks
    prerequisites: ReadinessPrerequisites
