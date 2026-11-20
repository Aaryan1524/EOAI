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

A custodian prepares new cases and SQLite seeds on an evaluator host. Use
[the schema](datasets/heldout.schema.json), and keep the manifest, expected
outcomes, SQLite seeds, traces, and reports in one dedicated private directory
outside Git checkouts. Give the evaluator and improvement workers distinct
unprivileged accounts. A dedicated evaluator VM with no shared mounts or shared
Ollama endpoint is a stronger boundary. The improvement account must have no
admin rights, access to evaluator service logs, or ability to invoke grading.

The evaluator consumes candidate text bundles after candidate selection closes.
Use a trusted checkout to run the evaluator. Load the candidate bundle as data;
the improvement checkout is never an import path. Pin the suite digest,
evaluator and runner source digests, commit, model digest, harness digest, and
execution limits in the study record before opening the sealed suite. The
`heldout --help` command lists the required options. The held-out command saves
all reports under the private suite directory. Its caller must be the evaluator
account. Keep scores and logs private until both arms and the improvement
process are closed.

Before a freeze, prove that the actual solver deployment and each improvement
role cannot read private input, answers, traces or scores, list their directory,
or invoke the evaluator. Run that probe against the deployed accounts and
service boundary using canary data. The repository tests verify the application
boundary and fail-closed permission checks. Provisioning a private suite and
testing its real deployment are separate readiness checks.
