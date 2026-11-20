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
  the paper's strategy for EOAI text fields. Proposers return one bounded
  append or exact replacement with an explicit `[iff condition]`. The
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
- Checklist schema projections, bounded trace evidence, single-criterion
  semantic requests, and one checklist format retry are local choices.
  Invalid tool executions, corrupt evidence and incomplete tasks can lower
  scores. An orchestration error stops the experiment before promotion.
- Deterministic gate tests use constructed trajectories to exercise accept,
  repair and reject cases. These tests are implementation evidence. They
  have no role in the live empirical result.
- The final comparison with naive RSI, repeated experiments and held-out
  evaluation is deferred. The development pilot can diagnose behavior and
  gate decisions. It cannot establish a generalization or forgetting advantage.
