# GUARD Phase 1 - Final Remediation Re-Audit (PR #11, head 79c120d)

## VERDICT: APPROVED FOR COMPARATIVE EXPERIMENTS
Conditional on the pre-registration items in "Required protocol conditions". None needs a code change.
Every blocker from the first audit that I could test offline is resolved. Remaining risks affect how
results are interpreted, not whether the experiment is valid.

## Not verified (read this first)
- Ollama was unreachable, so I did NOT replay the live bounded reviewer. The PR's "18 of 18 semantic
  judgments on first attempt" is unverified by me. Semantic accuracy is unvalidated even if it holds.
- The approval.request obligation branch (`_required_actions`) has no test in the PR or in my cases.
  The sealed rubric excludes approval writes, so it is likely out of scope; confirm for the new fixtures.
- Sealed held-out suite was neither read nor run. PR not merged.

## What I reproduced
| Check | Result |
|---|---|
| Full suite on PR head | 412 passed, 0 skipped (-rs); Seatbelt tests ran, not skipped |
| Suite + my 25 new cases | 437 passed |
| PR's 18 adversarial tests | 18 passed |
| Frozen paths vs `eoai-baseline-v0` (src, uv.lock, pyproject) | no diff |
| Files changed outside experiments/guard, tests/test_guard*, docs | none (eoai_eval, runner, tools untouched) |
| Adaptation path imports scorer/datasets/expectations | none; `audit.py` is post-selection only, not imported by `__main__` |
| Archive counterfactual (`remediation_audit`, run read-only on pilot-06) | matches PR: archive unchanged, 17 trials, 6 comparable, 12/31 legacy-invalid -> UNKNOWN |
| Harborline re-grade | 3/3 trials flagged "Missing required persisted action: ticket.create for INV-H01"; gate run 100 -> 91.67/45.83; independent grader also fails all 3 |
| Original Soft-HURT, hand-recomputed from recorded per-task scores (no GUARD code) | helped=[compound], hurt=[], net +1.1364 > 0 -> promote. Matches recorded gate exactly |

## Blocker status
| First-audit finding | Status | Evidence |
|---|---|---|
| F1 omitted required write scores 100 | RESOLVED | New `eoai_required_action_completion`, derived from the trusted task, write scope, and seed-state policy (not the solver's own claims). 18 tests + my cases pass |
| F2 false promotion | RESOLVED, with a caveat below | Recorded gate re-derived; corrected gate rejects |
| F3 reviewer noise (39% invalid) | RESOLVED SAFELY, accuracy unverified | UNKNOWN is excluded from denominators; any UNKNOWN anywhere makes the pair unreliable and blocks promotion (verified end to end in `run_rounds`) |
| F4/F5 uninformative or mis-attributed feedback | RESOLVED | Failure text is trace-derived ("Missing required persisted action..."); UNKNOWN and legacy invalid verdicts excluded from regret, contrasts, and the ledger; round-1 observed traces supplied |
| F6 ungrounded edits | PARTIALLY RESOLVED | Structured forms caught; paraphrases slip (see M2) |
| F7 split contamination | NOT YET DONE (by design) | PR states fresh fixtures are a later step; condition 1 below |

## My new cases (not in PR #11) - `test_guard_reaudit_new.py`
All pass; they confirm correct behaviour:
- Two required writes, solver completes one -> `required_action_completion` FAIL, rationale names the missing invoice; control completing both -> PASS.
- Forged ticket row for a disputed invoice after a correct run -> FAIL. Deleted ticket after claimed completion -> FAIL.
- Retried identical write attempt -> penalised only via `valid_tool_use`; no false required-action failure.
- Review refuses: authorized run without trusted task; a broader task than the one run; a different seed snapshot (each raises ValueError).
- Incumbent contains an UNKNOWN while the candidate looks better -> `run_rounds` rejects; final harness == baseline.
- Note: `audit_events` is append-only at the DB layer, so I could not forge audit rows at all.

## Remaining risks
**M1 (MAJOR for interpretation): the required-action veto is absolute, not relative to the incumbent.**
`promotion_gate` blocks if ANY task in the candidate's buffer fails `required_action_completion`, even when the
incumbent fails it identically. My test: candidate improves task A, ties task B (both miss the same write);
the paper's Eq. 13 promotes, the controller cannot (`paper promotes: True, controller promotes: False`).
Consequence: if a buffer contains any write task the frozen 4B model cannot do (Harborline was one,
3/3 missing), GUARD can never promote and is equivalent to v0 by construction. That would measure the
fixture, not GUARD. The veto is also load-bearing: with deterministic criteria only, the paper's arithmetic still
promotes the historical candidate at +2.00. Only the veto rejects it.

**M2 (MINOR for validity, real for efficiency): grounding is a lexical pre-filter.** 9 of 10 paraphrase probes
were accepted, e.g. "Pass a due_date_threshold of 30 when calling invoice.assess", "Call the escalate_all
helper", "invoice 104", "If the checker says...". The PR doc acknowledges this. The frozen gateway still
rejects invalid calls, so these waste candidates and show up as `valid_tool_use` failures; they do not
reach held-out data. But it is the same failure class as the original Long Acme candidates.

**M3: GUARD's write reviewer is stronger than the paper's reference-free reviewer.** It runs the frozen
policy engine on the seed to decide what must be written. That uses no held-out answers, but the independent
scorer shares that policy, so write-related gains are not independent evidence. Failure text also tells the
proposer which invoice should have been written.

**M4: citation validation proves presence, not support.** A PASS only needs an existing evidence key with a
consistent label. The reviewer can still false-pass semantic criteria; the solver's own summary is among the
evidence. Semantic criteria are a minority of each checklist.

**M5: UNKNOWN is a silent availability sink.** One UNKNOWN anywhere rejects the whole round with no abort
threshold, so a high unresolved rate yields a null GUARD arm that looks like "GUARD rejected everything".

## Paper-faithful vs added constraints
Faithful (unchanged `math.py` numerics): Eq. 2 scoring, Eqs. 3-4 obedience/hurt/gap, Eq. 5 contrasts, Eqs. 8-9
fitness, Eqs. 11-13 Soft-HURT arithmetic, noise floor 15, hurt budget 1, margin 1.5.
Added by this work (not in the paper; keep them labelled in results):
1. Required-action veto (absolute) - M1.
2. Paired-completeness gate: any UNKNOWN or criterion mismatch blocks promotion.
3. Deterministic required-action criterion from seed policy state - M3.
4. Grounding validator and evaluation-feedback/identifier filter - M2.
5. Bounded PASS/FAIL/UNKNOWN schema with citation, one retry, replacing the paper's free-form JSON verdict.
6. Unreliable-review rejections hidden from the ledger and regret feedback.
Pre-existing documented deviations (single appended rule, no scripts, same model for all roles, fixed replay
extension) are unchanged.

## Required protocol conditions (pre-register before running)
1. Use fresh development fixtures with disjoint case IDs, task text, and snapshot lineage, checked with the split validator. Do not reuse pilot-06's buffer or the gate split.
2. Before the experiment, run frozen v0 once on the new fixtures and record per-task `required_action_completion`. If v0 fails it on any fixture, either drop that fixture or run GUARD with the veto reported as a shadow decision, because GUARD cannot promote there.
3. Report per arm: promotions, rejections by reason (veto, unreliable, insufficient gain), UNKNOWN rate, and the paper-arithmetic decision alongside the actual one. Add a "GUARD without veto" ablation if budget allows.
4. Give Naive RSI the same trace-derived failure text and the same grounding filter, or state plainly that GUARD's edge may come from feedback richness.
5. Set a pre-registered UNKNOWN-rate threshold above which a GUARD run is reported as "unevaluable", not "rejected".
6. Measure live reviewer behaviour on the first run (I could not); do not treat the PR's 18/18 as validated.

## Files
- `audit/guard-phase1/test_guard_reaudit_new.py` - my 25 new cases (copy into `tests/`, run with `PYTHONPATH=src:.:tests`)
- `audit/guard-phase1/reaudit-archive-reproduction.json` - my independent archive replay output
