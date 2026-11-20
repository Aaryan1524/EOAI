# GUARD Phase 1

This implementation follows the fixed-batch, execution-scored path in
[GUARD arXiv v1](https://arxiv.org/html/2611.23476v1#S4). The solver runs the
same `qwen3:4b-instruct`, tool profiles, policies, limits, and production
runner as the frozen `eoai-baseline-v0` tag. Only text in the harness can
change. The adaptation controller verifies the tag, source and dependency
digests, and model ID and digest before launching a trial.

## Boundaries

| Module | Responsibility |
|---|---|
| `experiments/guard/__main__.py` | Freeze all checklists before execution, stage development snapshots, call the existing isolated solver, and verify paired manifests and artifact hashes. |
| `review.py` | Derive checks from the public task and tool schemas. Verify execution against disposable SQLite state, tool events, receipts, and read-back. Use the semantic reviewer only for the remaining predicates. |
| `math.py` | Score, rank, select and gate using deterministic functions. |
| `loop.py` | Run Algorithm 1 rounds, save each harness by digest, record exact diffs and contrasts, and retain the incumbent on rejection. |
| `roles.py` | Use the existing Seatbelt launcher for reviewer, distiller and candidate-generator message construction and validation. A trusted loopback relay calls the pinned model with no tools. It records prompt and reply digests, tokens, latency, purpose, seed and status. |
| `audit.py` | Independently score public development artifacts after selection and a new post-selection run. GUARD's adaptation path never imports this scorer or reads its rubrics. |

Solver trials receive fresh seed copies and fault counters. The pilot invokes
all four live access probes against synthetic canaries before any role runs.
Each review prompt receives a bounded projection of the public task,
observable evidence and tool schema. No held-out suite path is accepted by
the Phase 1 entry point. Only one child process can see its temporary solver
area, and it has no access to the independent grader.

The two training tasks are Long Acme and Acme account escalation. The replay
buffer contains Harborline, Acme list, and Meridian conflict. Candidate
fitness uses the same training tasks for all candidates. Soft-HURT uses the
full paired training and replay buffer for its winner. Observable replay
regressions can inform the next round. The independent grader runs only after
selection, and its public development labels are never used by the controller.

## Equations and decisions

The following references use the numbering in [§4 and Appendix B](https://arxiv.org/html/2611.23476v1#S4).

| Paper | Implementation |
|---|---|
| §4.1, Eq. 2 | Continuous score `S = 100 × passed / graded`. Ungraded criteria are omitted. Strict score is zero after a blocking error, 100 when all graded criteria pass, and `S / 2` otherwise. An invalid semantic verdict fails its binary criterion. |
| §4.2, Eqs. 3–4 | Obedience is the mean of a criterion's binary verdicts over graded replicates. Hurt is `max(0, previous_obedience − current_obedience)` when both are available. First-round or unmatched hurt is zero. Gap is `1 − incumbent_obedience`. Priority sums hurt and gap. A rejected winner or repair supplies the comparison for hurt, while gap stays anchored to the incumbent. |
| §4.2, Eq. 5 | Select top and bottom rollout by strict score, then continuous score. Save a contrast for a positive strict delta or a continuous delta at least 10 points. |
| §4.3, Eqs. 6–7 | Start with identical proposal prompts. Average replicates per task. Help and hurt require a continuous-score change strictly beyond +15 or −15 points. |
| §4.3, Eqs. 8–9 | `performance = mean(strict) + 0.25 × mean(continuous)`. Reward subtracts `3.5 × hurt count` and adds `1.5 × helped count`. Standardize against group mean and population standard deviation plus `1e−6`. Pick distinct best and worst indices. |
| §4.3, Eq. 10 | Send scrubbed best and worst contrast to the model. The next pool assigns the first half to exploitation and the remainder to the narrowed strategy. |
| §4.4, Eqs. 11–13 | Hurt penalty is the sum of full qualifying task drops divided by the size of the paired buffer. Promote only if the hurt budget holds, at least one task is helped when any task is hurt, and mean continuous gain strictly exceeds both the hurt penalty and the conditional margin. The default budget is one hurt task and the margin is 1.5 points when hurt exists. |
| §4.4 and Algorithm 1 | Permit at most one scoped repair for an isolated hurt. Rerun the whole paired buffer and apply the gate with zero margin. A failed or unavailable repair leaves the incumbent selected. A bounded rejection ledger retains metrics and rule excerpts for the next round. |

Mastered requirements are passed to the proposer as pinned constraints. They
remain in frozen checklists on future runs. Pinning is a proposal constraint
and an observable review criterion. The paper's gate can accept limited hurt
or changes within the noise floor. The trace therefore reports every
criterion loss separately from the thresholded gate metrics.

## Reproduce

Use the local baseline tag and install from the existing lockfile. Start the
pinned local Ollama service on loopback with the model artifact recorded in
the baseline tag. Run on a macOS host where the kernel sandbox can be applied.

```sh
uv sync --locked --extra test --no-install-project
.venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m experiments.guard \
  --out runs/guard-phase1/reproduction --rounds 1 --pool-size 2 --replicates 1
PYTHONPATH=src .venv/bin/python -m experiments.guard.audit runs/guard-phase1/reproduction
```

The run directory must be new. Read `protocol.json`, the frozen checklist
files, `r1-regret.json`, both `r1-candidate-*.json`, `r1-decision.json`,
`selection.json`, `post-selection.json`, and the independent report.
Candidate JSON retains the exact old and new text and a unified diff. The
digest-named files under `harnesses/` are read-only harness artifacts. The
incumbent is a digest pointer. A rejection changes no stored harness.
`reviews/` links criterion evidence to the raw solver outputs, snapshots and
traces. The role metrics file records model calls and their costs. Replays
can differ because the model and provider are not bitwise deterministic.

## Reproduction scope and deviations

- Phase 1 covers one fixed group and execution scoring. It does not reproduce
  the paper's stateful critic, multi-stream routing, reservoir replay, or
  benchmark-specific schedules in Appendices B–C.
- The initial playbook is frozen EOAI v3. The shared proposal prompt adapts
  the paper's strategy for EOAI text fields. A proposal adds one bounded rule.
  The controller renders its `[iff condition]` and compiles exact replacement
  only when repairing that rule. The
  original paper allows full playbook rewrites and some executable skills.
  GUARD cannot write or run a script through an EOAI candidate.
- EOAI uses the same verified Qwen artifact for reviewer, proposer,
  contrast and solver. The paper's experiments use other models. Only role
  generation uses stochastic sampling. Per-call seeds are recorded.
- The gate uses one fixed replay extension. Repaired candidates use the
  same hurt budget and zero margin. The paper does not specify an
  executable predicate for its prose phrase “solely due to isolated
  regressions.” This implementation requires a rejected winner with one
  to `repair_budget` hurt tasks and a positive net gain after removing those
  qualifying drops. Reward ties favor continuous score, then fewer hurt
  tasks, then earlier candidate index.
- Checklist schema projections, schema-constrained selection from literal
  public task clauses, bounded trace evidence, single-criterion semantic
  requests, and one checklist format retry are local choices. The clause
  splitter and model selection can omit or combine requirements. The
  independent rubric measures that gap after selection. Semantic criteria
  receive canonical citations when a decision or action references them.
  Invalid tool executions, corrupt evidence and incomplete tasks can lower
  scores. An orchestration error stops the experiment before promotion.
- Deterministic gate tests use constructed trajectories to exercise accept,
  repair and reject cases. These tests are implementation evidence. They
  have no role in the live empirical result.
- The final comparison with naive RSI, repeated experiments and held-out
  evaluation is deferred. The development pilot can diagnose behavior and
  gate decisions. It cannot establish a generalization or forgetting advantage.

## Observed Phase 1 development pilot

The public-only run is `runs/guard-phase1/pilot-06`. Its exact implementation
commit is `5a298f9e5771eac22571f766ae37f6173070fcdb`. The full suite passed
**328 tests** with the real macOS checks enabled. The measured runtime matches
the frozen runtime manifest. All four role profiles passed 14 live boundary
checks. The 16 archived Long Acme and Harborline files still match their
original checksums.

Self-review scores are continuous / strict on a 0–100 scale. The final column
shows the independent scorer's whole-task verdict. The selected candidate
ran again on fresh snapshots after promotion.

| Task | Frozen v0 | Candidate 0 at gate | Fresh post-selection | Independent v0 → post |
|---|---:|---:|---:|---|
| Long Acme | 44.44 / 0 | 44.44 / 0 | 55.56 / 0 | Fail → fail |
| Acme account escalation | 90.00 / 45.00 | 90.00 / 45.00 | 80.00 / 40.00 | Pass → pass |
| Harborline | 81.82 / 40.91 | 100.00 / 100.00 | 90.91 / 45.45 | Fail → fail |
| Acme list | 100.00 / 100.00 | 100.00 / 100.00 | 100.00 / 100.00 | Pass → pass |
| Meridian conflict | 87.50 / 0 | 75.00 / 0 | 75.00 / 0 | Pass → pass |

The model generated two instruction edits under identical first-round prompts.
Both scored 67.22 continuous and 22.50 strict on the training pair, giving
each a reward of 39.3056 and standardized fitness of zero. The configured
tie-break chose candidate 0. Both proposed conditions depended on unavailable
reviewer feedback and invented invoice-assessment fields. Both also targeted
escalation instead of the observed Long Acme customer-pagination failure.
The edits and their exact diffs are preserved in `r1-candidate-0.json` and
`r1-candidate-1.json`.

On the five-task gate, Harborline gained 18.18 points and Meridian lost 12.50.
The Meridian loss is retained at criterion level. It falls within the
paper's 15-point task noise floor and added no hurt penalty. The gate
accepted candidate 0 with +1.1364 mean continuous points and a threshold of
zero. A live scope repair was ineligible under this gate decision. The
deterministic tests cover both accepted and rejected repairs and rollback.
Best/worst contrast generated the two next-round prompts. This pilot ran
one round, so it did not evaluate those prompts.

Independent correctness was **3/5 before and 3/5 after**. Harborline went
from three assessed invoices to six at the gate and again on the fresh
post-selection run. None of those three trials created the required ticket.
The self-review gave the gate rollout 100/100 because its checks did not
require a successful eligible write when no write was attempted. Long Acme
still failed customer coverage. Acme escalation created and verified one
INV-104 ticket for each evaluated arm. Meridian continued to meet its
independent business requirements, although its semantic check failed on
an invalid evidence-backed verdict. The fresh macro self-review moved from
80.75 to 80.29 continuous and from 37.18 to 37.09 strict. This pilot provides
no evidence of a net task-success improvement or advantage over naive RSI.

Seventeen solver trials used 190 model turns and 158 tool calls. Solver
usage was 820,076 input and 4,951 output tokens. The 39 role calls used
219,586 input and 5,323 output tokens. Per-call and per-run latency,
snapshot and trace digests, criterion evidence, and two paired trajectory
contrasts remain with the run. Earlier attempts exposed checklist formatting
and provider grammar issues. They ended before candidate promotion and
remain separate from the measured pilot.

### Independent audit entry points

- Read `math.py` alongside Eqs. 2–13. The deterministic fixtures explicitly
  test fractional obedience, threshold equality, regression penalties and
  controlled gate decisions.
- Read `loop.py` and the live `r1-decision.json` together. Check the equal
  candidate rewards, selected index, task deltas, and promotion inequality.
- Compare `reviews/r1-replay-compound-escalation-1.json` with the matching
  entry in `independent-development-audit.json`. This is the observed
  calibration failure. Review the completeness of eligibility-to-write
  checks before preregistering a comparative benchmark.
- Use `protocol.json`, candidate artifacts, `selection.json`,
  `post-selection.json`, and `trajectory-contrasts.json` for the public
  evidence package. The experiment reproduction above uses only development
  and replay cases.
