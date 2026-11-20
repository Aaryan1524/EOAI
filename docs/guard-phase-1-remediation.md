# GUARD Phase 1 audit remediation

The Phase 1 comparison remains Frozen v0, Naive RSI, and GUARD. This work
changes how GUARD measures and accepts edits on development data. The frozen
model, runner, tools, business policy, independent grader, and sealed suite
retain their previous versions. The original pilot is historical evidence.

## Fixes

- **Required actions.** The reviewer binds the public assignment, authorized
  actor, write scope, and source snapshot to the run manifest. It evaluates
  every in-scope invoice against the frozen eligibility policy and pre-run
  SQLite state. It accounts for existing tickets, authorization, exclusions,
  and approval prerequisites. A required action passes only with the new
  persisted record, matching audit and receipt, and later read-back. This also
  catches an invoice the solver never assessed. The evidence distinguishes
  no new action required, a verified action, a denied or unrequired write, and
  a missing or unverified action.
- **Semantic reliability.** A model verdict must match a bounded schema and
  select an existing public evidence reference with a consistent stance.
  Python resolves that reference to the observed value for the stored rationale.
  The reviewer retries once on invalid or indeterminate output. Unresolved
  results are UNKNOWN. The raw assistant response and exact parse or validation
  error are retained in development review artifacts. A model transport or
  artifact integrity failure stops the affected calculation. UNKNOWN results
  are excluded from the grade denominator and from solver failure feedback.
- **Grounding.** The proposer and repairer receive the actual public tool
  declarations for the active profiles and bounded incumbent traces. A
  deterministic validator rejects undeclared tools or arguments in recognized
  call forms, named argument references, evaluation-only feedback, and case
  literals or criterion IDs. Failure text points to observed coverage,
  continuations, audit discrepancies, and omitted work. The first round can
  show observed traces without claiming a contrast when it has only one
  rollout per task.

## Gate and calibration

The 17 original solver trajectories were verified and regraded from their
saved traces and SQLite files. The historical Harborline gating run omitted
the required ticket for its eligible invoice. Its original 100/100 becomes
**91.67 continuous / 45.83 strict** when the new deterministic obligation is
added and its historical valid semantic verdicts are retained.

| Archived measure | Original pilot | Corrected archive replay |
|---|---:|---:|
| Trials reviewed | 17 | 17 |
| Historical malformed verdicts | 12 of 31 counted as failed | 12 of 31 treated as UNKNOWN |
| Trials with complete recorded semantic evidence | 17 scored | 6 comparable |
| Independent failures among comparable perfect reviews | 1 of 4 | 0 of 3 |
| Harborline runs with required action missing | No completion criterion | 3 of 3 caught |

The original gate **accepted** at +1.1364 mean continuous points. The corrected
gate **rejects** with `required_action_incomplete` and `unreliable_review`.
A diagnostic rerun using deterministic criteria only reaches +2.00 points in
the unmodified paper formula. It still **rejects** under the required-action
check. That distinction matters. Meaningful assessment coverage improved in
the historical candidate run, so simply adding another averaged binary check
would still allow missing a required write to be offset by other gains.
Both historical candidate edits also fail the new grounding check.

### Live semantic calibration on fixed public evidence

The same ten archived gate runs contain 18 semantic judgments. The original
protocol produced six invalid results on those pairs. An intermediate
quote-and-explanation contract left all 18 UNKNOWN after retry. The raw
responses show budget truncation and unsupported copied citations. Its
artifacts are preserved as `live-counterfactual.json`.

The bounded evidence-reference contract completed **18 of 18** on the first
attempt in `live-bounded-counterfactual.json`. Its full gate regrade yielded
**−0.1515 mean points** and rejects for both insufficient gain and incomplete
required action. Harborline's candidate gate rollout scored **75.00 / 37.50**
under these fresh judgments, including the failed deterministic obligation.
The independent outcome remains a failure. The pilot's before/after fingerprint
was identical. This sample establishes response reliability on archived
public evidence. It does not establish semantic accuracy on new cases.

The final native suite passed **412 tests**. All **18 adversarial tests**
passed with zero expected failures.

## Deviations and limits

- Equations 2–13 retain their numerical definitions. The comparative controller
  adds two engineering preconditions around them. Paired judgments must be
  complete and comparable, and the candidate must discharge required writes.
  These checks can block a promotion that would pass the paper's mean gate.
  A missing write or unresolved review cannot be overcome by additional
  points on other tasks or by a scope repair.
- Citation validation proves source presence and internal consistency. It does
  not prove the cited fact establishes the semantic conclusion. The independent
  scorer remains the external success measure. A conservative gate may retain
  an incumbent more often, so the comparison should report unknown rates,
  skipped comparisons, and promotions across arms.
- The grounding check covers structured tool calls, recognized named
  references, and detectable case-specific literals. It cannot infer every
  claim embedded in unconstrained natural language. Frozen gateway validation
  remains the final authority for actual tool calls.
- Pilot-06 and its replay buffer are exposed development data. The next
  comparison needs newly designated development fixtures with disjoint case
  IDs, task assignments, and snapshot lineages, verified with the existing
  split validator. Neither this archive regrade nor a repeated run on those
  fixtures measures generalization. This remediation does not run the
  three-arm experiment.

## Reproduce the audit

Run on a checkout of the remediation with its existing locked environment.

```sh
PYTHONPATH=src:.:tests .venv/bin/python -m pytest -q tests/test_guard_adversarial_audit.py
.venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m experiments.guard.remediation_audit \
  --out runs/guard-phase1/remediation/reproduced-archive.json
PYTHONPATH=src .venv/bin/python -m experiments.guard.remediation_audit \
  --live-review --out runs/guard-phase1/remediation/reproduced-live.json
```

The archive command verifies each trace and sandbox database against the
original run hashes and writes to a new location. Its before/after tree
fingerprint must match. The live option reruns only the semantic reviewer
on the ten archived paired gate trajectories. It uses the verified macOS
sandbox launcher and the original local Qwen digest. It never starts a solver
trial. The historical artifact lacks raw semantic replies, so the offline
command carries forward recorded valid grades for calibration observations
and does not manufacture verdicts for the invalid ones.
