# EOAI

EOAI is a synthetic enterprise operations sandbox for reviewing invoices, applying contractual payment terms, and determining when an escalation is eligible.

The sandbox provides isolated SQLite copies, a frozen business date, and 18 typed tools for customer, invoice, contract, evidence, ticket, and approval workflows. Its fixture includes overlapping customer names, signed and draft amendments, a dispute, an existing ticket, and conflicting or missing term evidence.

## Run the tests

Use Python 3.12 or later.

```sh
python -m pip install -e '.[test]'
python -m pytest -q
```

The [scripted Acme workflow](tests/test_tools.py) drives the public gateway through 32 calls. It finds the account, exhausts invoice pages, checks contract evidence, and creates and reads back one eligible ticket.

## Assess a synthetic invoice

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from enterprise_ops.sandbox import seed_snapshot
from enterprise_ops.domain.entities import Principal
from enterprise_ops.tools.gateway import ToolGateway

with TemporaryDirectory() as directory:
    root = Path(directory)
    snapshot = seed_snapshot(root / "snapshots", "baseline")
    sandbox = snapshot.clone(root / "runs", "acme-review")
    gateway = ToolGateway(sandbox, Principal("northstar_property", "analyst"))
    response = gateway.invoke("invoice.assess", {"invoice_id": "INV-104"})
    assert response.ok, response.error
    print(response.data.status, response.data.outstanding_minor, response.data.days_past_due)
```

Expected output is `eligible 1000000 49`. The amount is in USD minor units.

The host can supply a principal-bound `WriteScope` to authorize simulated actions. A write requires a current assessment, a preview, and a commit. To verify delivery, match the ticket and creation audit returned by `ticket.get` to the commit receipt. Retry a lost commit response with the exact same arguments and key.

`RunSandbox.reset()` waits for admitted tool operations and returns a fresh copy with a new sandbox identity. Build a fresh gateway for that copy. The retired database stays available to the local operator for audit inspection. See the [design record](docs/milestone-0-design.md) for the complete protocol.

## Run a task with a local model

Start Ollama on loopback with an installed model that supports tools. The runtime accepts a model adapter, a snapshot, a task, and explicit limits. This example uses an already-installed `qwen3:4b-instruct` tag.

```python
from pathlib import Path
from uuid import uuid4

from enterprise_ops.agent.model import OllamaModel
from enterprise_ops.agent.runner import RunTask, run_task
from enterprise_ops.domain.entities import Principal
from enterprise_ops.sandbox import seed_snapshot

root = Path("runs") / uuid4().hex
snapshot = seed_snapshot(root / "snapshots", "baseline")
task = RunTask(
    "acme-list", "List every invoice for Acme Property Group.",
    Principal("northstar_property", "analyst"), "list_invoices",
    customer_query="Acme Property Group",
)
result = run_task(snapshot, task, OllamaModel("qwen3:4b-instruct"), run_id="list", runs_root=root)
print(result.output.model_dump_json(indent=2))
print(result.trace_path)
```

Run artifacts contain the declared profile, snapshot and harness digests, observed tool results, validated output, and audit-backed state changes. The runtime records an action as completed after matching its durable receipt to read-back.

The runner uses short model-facing references for cursors, evidence and previews. It resolves them back to the gateway's issued values in the same run. Tool attempts and retries count toward the run limit. If the runner cannot reconcile a write or verify its read-back, it stops and reports the open outcome.

## Development scenarios

The catalog in `enterprise_ops.fixtures.catalog` includes a paged Acme review and a compound Harborline escalation. Both use the v2 invoice search declaration with `account_ref`, `page_size`, and `page_cursor`. Baseline scenarios continue to use v1. Each snapshot selects one profile for the entire run.

Run `python -m pytest -q tests/test_catalog_workflows.py` to exercise both higher-complexity workflows. Harborline includes overlapping amendments, a disputed invoice, a settled invoice, an existing ticket, and a transient search fault. The test checks that the run reports the conflict and creates and verifies the one eligible missing ticket.

## Operate and replay a run

After installing the package and starting Ollama, use the `ops-agent` command.

```sh
ops-agent tasks
ops-agent seed baseline
ops-agent run baseline --task acme-list --id first --harness v1
ops-agent inspect first
ops-agent replay first --id second --harness v2
ops-agent reset baseline --id fresh
```

The default artifact directory is `runs/`. Pass `--root PATH` before the command to choose a different location. Seed the snapshot with `--task acme-paged-v2` or `--task compound-escalation` for either v2 workflow. An escalation uses a write scope only with `--authorize-writes`, and replaying that authorization requires the flag again.

Replay checks the original snapshot, task, tool declarations, fault plan, model digest and settings, and harness. It creates a fresh sandbox and makes new inference requests, verifying the digest of the installed model tag around inference. `--harness v2` explicitly changes the instructions for the new run. For interrupted runs, `inspect` can recover committed audit events without changing the retained SQLite files.

Local-model smoke testing verified invoice listing, ambiguity handling, and a guided ticket write with read-back. The unassisted reference escalation remains unverified on the installed model.
