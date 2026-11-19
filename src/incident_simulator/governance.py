"""Application governance shared by LangChain tools and the approval API.

The model proposes operations. Only a trusted application caller can supply
Reviewer identity. This layer does not authenticate HTTP requests.
The in-process ledger is intentionally replaceable by PostgreSQL transactions.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Awaitable, Callable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Risk(str, Enum):
    READ = "read"
    APPROVAL = "approval"
    DENIED = "denied"


class Status(str, Enum):
    READY = "ready"
    WAITING = "waiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    UNKNOWN = "outcome_unknown"
    FAILED = "failed"


class Context(Contract):
    task_id: UUID
    environment_id: UUID
    environment_version: int = Field(ge=0)
    trace_id: UUID


class Reviewer(Contract):
    """Construct from authenticated server identity, never an LLM tool argument."""
    subject: str = Field(min_length=1)
    can_approve: bool = False


class Operation(Contract):
    id: UUID = Field(default_factory=uuid4)
    context: Context
    tool: str
    arguments_json: str
    risk: Risk
    reason: str = Field(min_length=1)
    expected_impact: str = Field(min_length=1)
    created_at: datetime
    expires_at: datetime
    digest: str


class Event(Contract):
    sequence: int
    operation_id: UUID
    status: Status
    at: datetime
    actor: str
    detail: str = ""


class ToolDefinition(Contract):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    risk: Risk
    timeout_seconds: float = Field(default=30, gt=0, le=300)


class GovernanceError(ValueError):
    pass


Handler = Callable[[BaseModel, Context], Awaitable[dict]]


class ToolRegistry:
    def __init__(self):
        self._entries: dict[str, tuple[ToolDefinition, type[BaseModel], Handler]] = {}

    def register(self, definition: ToolDefinition, schema: type[BaseModel], handler: Handler):
        if definition.name in self._entries:
            raise GovernanceError("Tool already registered")
        if schema.model_config.get("extra") != "forbid":
            raise GovernanceError("Tool schemas must reject unknown arguments")
        self._entries[definition.name] = (definition, schema, handler)

    def get(self, name: str):
        try:
            return self._entries[name]
        except KeyError:
            raise GovernanceError("Unknown tool") from None

    def schemas(self):
        return [{"name": d.name, "description": d.description,
                 "parameters": s.model_json_schema(), "risk": d.risk.value}
                for d, s, _ in self._entries.values() if d.risk != Risk.DENIED]


class ExecutionGate:
    """Single-process, single-event-loop ledger; no cross-process durability yet.

    Reservation happens under the lock before awaiting external execution.
    Mutating calls are never automatically retried after an uncertain result.
    """
    def __init__(self, registry: ToolRegistry, clock=utcnow):
        self.registry = registry
        self.clock = clock
        self._operations: dict[UUID, Operation] = {}
        self._statuses: dict[UUID, Status] = {}
        self._results: dict[UUID, str] = {}
        self._events: list[Event] = []
        self._lock = asyncio.Lock()
        self.persist = None

    def _event(self, op: Operation, status: Status, actor: str, detail=""):
        self._statuses[op.id] = status
        self._events.append(Event(sequence=len(self._events) + 1, operation_id=op.id,
                                  status=status, at=self.clock(), actor=actor, detail=detail))

    def status(self, operation_id: UUID) -> Status:
        return self._statuses[operation_id]

    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    async def propose(self, context: Context, tool: str, arguments: dict, *,
                      reason: str, expected_impact: str, ttl_seconds: int = 900) -> Operation:
        definition, schema, _ = self.registry.get(tool)
        if definition.risk == Risk.DENIED:
            raise GovernanceError("Tool denied by policy")
        if not 1 <= ttl_seconds <= 3600:
            raise GovernanceError("Invalid approval lifetime")
        validated = schema.model_validate(arguments)
        arguments_json = json.dumps(validated.model_dump(mode="json"), sort_keys=True,
                                    ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        now = self.clock()
        payload = {"context": context.model_dump(mode="json"), "tool": tool,
                   "arguments_json": arguments_json, "risk": definition.risk.value}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        op = Operation(context=context, tool=tool, arguments_json=arguments_json,
                       risk=definition.risk, reason=reason, expected_impact=expected_impact,
                       created_at=now, expires_at=now + timedelta(seconds=ttl_seconds), digest=digest)
        async with self._lock:
            self._operations[op.id] = op
            self._event(op, Status.READY if op.risk == Risk.READ else Status.WAITING, "policy")
        return op

    async def decide(self, operation_id: UUID, *, reviewer: Reviewer, approve: bool,
                     expected_digest: str, comment: str = ""):
        if not reviewer.can_approve:
            raise GovernanceError("Reviewer lacks approval permission")
        async with self._lock:
            op = self._operations[operation_id]
            if self._statuses[op.id] != Status.WAITING:
                raise GovernanceError("Operation is not awaiting approval")
            if expected_digest != op.digest:
                raise GovernanceError("Approval does not match the displayed operation")
            if self.clock() >= op.expires_at:
                self._event(op, Status.EXPIRED, "policy")
                raise GovernanceError("Approval expired")
            self._event(op, Status.APPROVED if approve else Status.REJECTED,
                        reviewer.subject, comment)

    async def execute(self, operation_id: UUID, *, current_context: Context) -> dict:
        async with self._lock:
            op = self._operations[operation_id]
            if current_context != op.context:
                raise GovernanceError("Task or environment changed; propose a new operation")
            state = self._statuses[op.id]
            if state == Status.SUCCEEDED:
                return json.loads(self._results[op.id])
            required = Status.READY if op.risk == Risk.READ else Status.APPROVED
            if state != required:
                raise GovernanceError(f"Cannot execute operation in state {state.value}")
            if self.clock() >= op.expires_at:
                self._event(op, Status.EXPIRED, "policy")
                raise GovernanceError("Operation expired")
            definition, schema, handler = self.registry.get(op.tool)
            if definition.risk != op.risk:
                raise GovernanceError("Tool policy changed")
            params = schema.model_validate_json(op.arguments_json)
            self._event(op, Status.RUNNING, "worker")
        if self.persist:
            await self.persist()
        try:
            result = await asyncio.wait_for(handler(params, op.context), definition.timeout_seconds)
            if not isinstance(result, dict):
                raise TypeError("Tool result must be an object")
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except BaseException as exc:
            # Cancellation after dispatch is also uncertain for writes.
            async with self._lock:
                self._event(op, Status.UNKNOWN if op.risk == Risk.APPROVAL else Status.FAILED,
                            "worker", type(exc).__name__)
            raise
        async with self._lock:
            self._results[op.id] = encoded
            self._event(op, Status.SUCCEEDED, "worker")
        if self.persist:
            await self.persist()
        return json.loads(encoded)
