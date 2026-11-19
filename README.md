# EOAI

EOAI is a synthetic enterprise operations sandbox for reviewing invoices, applying contractual payment terms, and determining when an escalation is eligible.

Milestone 1 provides a SQLite schema, a frozen simulation clock, tenant-scoped access, and deterministic balance, due-date, and eligibility rules. Its fixture includes overlapping customer names, a signed amendment, a draft amendment, a dispute, an existing ticket, and invoices with conflicting or missing term evidence.

## Run the domain tests

Use Python 3.12 or later.

```sh
python -m pip install -e '.[test]'
python -m pytest -q
```

## Assess a synthetic invoice

```python
from contextlib import closing

from enterprise_ops.domain.entities import Principal
from enterprise_ops.fixtures.seed import seed_baseline
from enterprise_ops.storage.repository import assess_invoice
from enterprise_ops.storage.sqlite import connect, initialize

with closing(connect(":memory:")) as db:
    initialize(db)
    clock = seed_baseline(db)
    principal = Principal("northstar_property", "analyst")
    result = assess_invoice(db, principal, "INV-104", clock)
    print(result.status, result.balance.outstanding_minor, result.days_past_due)
```

Expected output is `eligible 1000000 49`. The amount is in USD minor units. An eligibility assessment is read-only. The [design record](docs/milestone-0-design.md) describes how later milestones will submit and verify simulated ticket actions.
