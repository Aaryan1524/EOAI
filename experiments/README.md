# EOAI evaluation

The evaluator lives outside the EOAI package. It runs the same tools, policies
and agent runner for every harness. The editable input is one UTF-8 JSON
object with exactly three string fields: `instructions`, `skills`, and `memory`.
Omit the bundle to run the packaged v3 instructions. The bundle is data and
cannot specify Python modules, tools, budgets, accounts, or snapshots.

## Run development checks

Install the checked-in dependencies and run a public development case.

```sh
uv sync --locked --extra test --no-install-project
PYTHONPATH=src .venv/bin/python -m experiments.eoai_eval adaptation \
  --case acme-account-escalation --trials 3 \
  --model-digest 0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0 \
  --out runs/acme-evaluation
```

The command requires a local Ollama server with the pinned model. The gate
command uses the separate gate manifest. The gate expectations describe the
desired business outcome. A known failure can fail scoring while still
satisfying the write checks.

Each trial clones and verifies its source SQLite snapshot and uses a fresh fault
schedule. It retains the trace, the sandbox database, runtime metrics, and an
evaluation report. The provenance records the source commit and exact file
digests, model identity and settings, dependency lock and installed versions,
task and snapshot identity, limits, and tool schema versions. Semantic results
exclude generated IDs and tool ordering. The raw artifacts retain them for audit.

## Run held-out cases

A custodian prepares new cases and SQLite seeds on the evaluator host. Use
[the schema](datasets/heldout.schema.json) and keep the manifest, expected
outcomes, SQLite seeds, traces, and reports in one private directory outside
Git checkouts and the improvement workspace.

The held-out command requires macOS with a working native process sandbox.
It verifies the live policy for the solver, reviewer, distiller, and candidate
generator before running either arm. Each solver starts in a fresh sandboxed
Python process. It receives only the task, a verified copy of the scenario
seed, the selected harness, and fixed runner settings. The evaluator process
owns the private suite and grades after the solver exits. The solver can reach
only a local broker that forwards model identity and inference calls to the
pinned model. The three improvement role profiles have no network allowance.
The worker receives an empty environment and only its standard input, output,
and error descriptors.

Use the same default-deny launcher for all future improvement workers. Run
no worker code as the unrestricted custodian account. The checks establish
isolation for processes started through the launcher. The underlying host,
Python environment, and model service are trusted. Hosts that cannot apply the
kernel policy fail closed. GUARD workers are not part of this repository.

The evaluator consumes a candidate text bundle after candidate selection
closes. Pin the suite and source digests, commit, model digest, harness digest,
and limits in the study record before running the pilot. Both arms run in one
invocation with identical fixed settings. The held-out command stores all
reports, including the role access probes, inside the private directory. Keep
those reports private until both arms and improvement are closed.

The initial rubric covers ticket creation, invoice inventory and review for an
identified account. Use cases supported by the rubric when preparing the sealed
pilot. Approval writes and tasks whose correct result has no resolved customer
are outside this rubric.
