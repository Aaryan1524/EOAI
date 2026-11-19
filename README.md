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
