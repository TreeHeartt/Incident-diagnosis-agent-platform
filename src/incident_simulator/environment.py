"""Adapter over an external SRE-Bench checkout. No upstream code is vendored."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import sys
from typing import Literal
from uuid import uuid4

from pydantic import Field
from .governance import Context, Contract, GovernanceError, Risk, ToolDefinition, ToolRegistry


class ServiceInput(Contract):
    service: str = Field(min_length=1, max_length=80)


class MetricsInput(Contract):
    target: str = Field(min_length=1, max_length=80)


class DiagnosticInput(Contract):
    type: str = Field(default="general", max_length=80)


class RecoveryInput(Contract):
    reason: str = Field(min_length=1, max_length=2000)
    expected_impact: str = Field(min_length=1, max_length=2000)


class ServiceRecoveryInput(RecoveryInput):
    service: str = Field(min_length=1, max_length=80)


class FixInput(RecoveryInput):
    fix_type: Literal["restart_auth_with_connection_cleanup", "increase_pool_size",
                      "reprocess_affected_orders", "delete_log"]
    target: str | None = Field(default=None, max_length=200)


class EmptyInput(Contract):
    pass


class ServiceHealth(Contract):
    name: str
    status: str
    error_rate: float = Field(ge=0, le=1)
    latency_p99_ms: int = Field(ge=0)


class PublicObservation(Contract):
    terminal_output: str
    services: list[ServiceHealth]
    step: int
    sla_remaining: int
    incident_description: str
    last_command_error: str | None = None


def _public_observation(env, terminal_output, error=None):
    """Boundary override: preserve valid sub-1% rates rejected by upstream schema.

    Reads an explicit allowlist; neither private root cause nor reward is projected.
    Fault transitions and action implementations remain upstream methods.
    """
    return PublicObservation(terminal_output=terminal_output,
        services=[ServiceHealth.model_validate(s) for s in env._state.get("services", [])],
        step=env._step_count, sla_remaining=max(0, env.SLA_STEPS - env._step_count),
        incident_description=env._state.get("incident_description", ""), last_command_error=error)


class SRESession:
    def __init__(self, checkout: Path, scenario: str):
        root = checkout.resolve()
        if not (root / "environment.py").is_file() or not (root / "tasks/__init__.py").is_file():
            raise ValueError("SRE_BENCH_PATH must point to an external SRE-Bench checkout")
        # Upstream uses absolute imports. Reject a conflicting module rather than silently reuse it.
        for name, relative in (("environment", "environment.py"), ("tasks", "tasks/__init__.py")):
            loaded = sys.modules.get(name)
            if loaded and Path(loaded.__file__).resolve() != root / relative:
                raise ValueError(f"Conflicting upstream module: {name}")
        sys.path.insert(0, str(root))
        try:
            module = importlib.import_module("environment")
            tasks = importlib.import_module("tasks").TASK_REGISTRY
        finally:
            sys.path.remove(str(root))
        if scenario not in tasks:
            raise ValueError(f"Unknown scenario; supported: {', '.join(tasks)}")
        self._action = module.Action
        adapted = type("Adapted" + tasks[scenario].__name__, (tasks[scenario],),
                       {"_build_observation": _public_observation})
        self._env = adapted()
        self.scenario = scenario
        self._observation = self._env.reset().model_dump()
        self.context = Context(task_id=uuid4(), environment_id=uuid4(),
                               environment_version=0, trace_id=uuid4())
        self.done = False
        self.resolved = False
        self._lock = asyncio.Lock()

    def observation(self) -> dict:
        return json.loads(json.dumps(self._observation))

    def snapshot(self):
        return json.loads(json.dumps({'version':1,'scenario':self.scenario,
            'context':self.context.model_dump(mode='json'),'observation':self._observation,
            'done':self.done,'resolved':self.resolved,
            'state':self._env._state,'step':self._env._step_count,
            'reward':self._env._episode_reward,'env_done':self._env._done}))

    def restore(self, data):
        if data['version'] != 1 or data['scenario'] != self.scenario:
            raise ValueError('Environment snapshot version mismatch')
        self.context=Context.model_validate(data['context'])
        self._observation=data['observation']; self.done=data['done']; self.resolved=data['resolved']
        self._env._state=data['state']; self._env._step_count=data['step']
        self._env._episode_reward=data['reward']; self._env._done=data['env_done']

    def verification(self) -> dict:
        return {"environment_resolved": self.resolved, "episode_done": self.done,
                "independent_probe": "not_available",
                "scope": "simulator_terminal_state_only",
                "services": self.observation()["services"]}

    async def call(self, command: str, params: dict, context: Context) -> dict:
        async with self._lock:
            if context != self.context:
                raise GovernanceError("Environment version changed")
            if command == "get_service_status":
                return self.verification()
            if self.done:
                return {"error": "episode_finished", "verification": self.verification()}
            if command == "apply_fix" and params.get("fix_type") == "delete_log":
                if params.get("target") != "/var/log/app/app.log.2024-01-14":
                    raise GovernanceError("Only the scenario's rotated log can be removed")
            obs, _, done, info = self._env.step(self._action(command=command, params=params))
            self._observation = obs.model_dump()
            self.done = done
            self.resolved = bool(info.get("resolved", False))
            self.context = self.context.model_copy(update={
                "environment_version": self.context.environment_version + 1})
            # No state(), reward, ground-truth fields or solution metadata exposed to the model.
            return {"observation": self.observation(), "episode_done": self.done}

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        specs = [
            ("read_logs", "Read a service's available logs.", ServiceInput, "read_log", Risk.READ),
            ("get_metrics", "Query metrics for a service, database or disk.", MetricsInput, "check_metrics", Risk.READ),
            ("run_diagnostic", "Run diagnostics, for example deploy_history.", DiagnosticInput, "run_diagnostic", Risk.READ),
            ("get_service_status", "Check current simulator health; not an independent recovery probe.", EmptyInput, "get_service_status", Risk.READ),
            ("restart_service", "Restart a service after human approval.", ServiceRecoveryInput, "restart_service", Risk.APPROVAL),
            ("rollback_service", "Roll back a service after human approval.", ServiceRecoveryInput, "rollback", Risk.APPROVAL),
            ("apply_fix", "Apply an allowlisted scenario repair after human approval.", FixInput, "apply_fix", Risk.APPROVAL),
        ]
        for name, description, schema, command, risk in specs:
            async def handler(args, context, command=command):
                params = args.model_dump(exclude={"reason", "expected_impact"}, exclude_none=True)
                return await self.call(command, params, context)
            registry.register(ToolDefinition(name=name, description=description, risk=risk), schema, handler)
        return registry
