"""Solver process. Its launch payload contains no evaluator inputs or credentials."""

import json
import sys
from datetime import datetime
from pathlib import Path

from enterprise_ops.agent.harness import HarnessContent
from enterprise_ops.agent.model import OllamaModel
from enterprise_ops.agent.runner import RunLimits, RunTask
from enterprise_ops.clock import FrozenClock
from enterprise_ops.domain.entities import Capability, EscalationRestrictions, Principal
from enterprise_ops.sandbox import FaultKind, FaultPlan, FaultStep, Snapshot
from enterprise_ops.tools.session import WriteScope

from .execution import TrialConfig, load_variant, run_trial


def main():
    data = json.load(sys.stdin)
    root = Path.cwd()
    snapshot_data = data["snapshot"]
    snapshot = Snapshot(
        snapshot_data["id"], root / "seed.sqlite3", snapshot_data["digest"],
        snapshot_data["profile"], FrozenClock(datetime.fromisoformat(snapshot_data["time"])),
    )
    task_data = data["task"]
    principal = Principal(**task_data.pop("principal"))
    scope_data = task_data.pop("scope")
    scope = None
    if scope_data is not None:
        scope = WriteScope(
            principal, scope_data["id"], scope_data["customer_id"],
            frozenset(Capability(value) for value in scope_data["capabilities"]),
            invoice_ids=scope_data["invoice_ids"],
            restrictions=EscalationRestrictions(**scope_data["restrictions"]),
        )
    task = RunTask(principal=principal, write_scope=scope, **task_data)
    config_data = data["config"]
    limits = RunLimits(**config_data.pop("limits"))
    config = TrialConfig(limits=limits, **config_data)
    harness = HarnessContent(**data["harness"]) if data["harness"] is not None else load_variant(None)
    faults = FaultPlan(tuple(
        FaultStep(row["tool_name"], row["attempt"], FaultKind(row["kind"])) for row in data["faults"]
    ))
    model = OllamaModel(data["model_id"], f"http://127.0.0.1:{data['port']}")
    result = run_trial(
        config, model, harness, snapshot, task, faults, run_id=data["run_id"],
        root=root / "scratch", scenario=data["scenario"],
    )
    print(result.trace_path.name)
