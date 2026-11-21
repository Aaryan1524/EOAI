# GUARD Phase 1 — Independent Adversarial Audit (PR #10, head ec03a55)

## VERDICT: CHANGES REQUIRED

The pilot's only promotion was a false promotion. The gate accepted candidate 0 solely because of a
+18.18 "help" on Harborline, and that rollout was a false 100/100. Remove that credit and the mean
paired delta is -2.5, so the gate rejects. The reviewer cannot yet be trusted as the reward signal
for a comparative experiment.

Scope: public development artifacts, GUARD source, paper (GUARD.pdf: Alg. 1, Eqs. 2-13, App. B).
Held-out suite, grader credentials and private answers were not accessed. Ollama was unreachable, so a
live reviewer re-calibration was NOT possible; all calibration below uses archived pilot-06 artifacts.
Baseline tag, main and PR #10 were not modified. Tests live in an untracked file only.

## Reproduction
- `pytest` on PR head: 328 passed (matches claim). With the new audit file: 332 passed, 6 xfailed.
- Pilot-06 artifacts re-read, not re-run (no model server).
- Audit tests: `audit/guard-phase1/test_guard_adversarial_audit.py` (copy to `tests/`, run with
  `PYTHONPATH=src:.:tests`). Each xfail(strict) states the CORRECT behaviour and flips to an error when fixed.

## Findings

### F1 CRITICAL - Required writes carry no obligation; omission is never penalised
- Evidence: `r1-replay-compound-escalation-1`: tools show INV-H01 and INV-H02 assessed `eligible`;
  the solver's own output lists INV-H01 action `not_attempted`, missing_information `ACTION_NOT_ATTEMPTED`,
  status `partial`, review_required true. GUARD scored 100.0 / 100.0. Independent grader: fail
  (exact_ticket_writes, ticket_readback, reported_ticket_actions, pending_invoices, tenant_revisions).
- Root cause (`review.py`): with zero writes, `_scoped_audit` (L521), `_readback` (L579) and `_idempotent`
  (L623) are vacuously true (empty audits = empty receipts = empty confirmed). Writes can only LOSE points
  in these hooks and never gain any, so omitting a write is weakly dominant. No fixed hook asserts
  "every eligible, authorised, un-ticketed invoice has a read-back-verified action" (`_fixed_checks` L123).
- The semantic selector cannot compensate: `_CHECKLIST` (L22) says "Omit requirements
  deterministic_criteria already covers" and "An empty list is valid"; the readback description sounds like
  it covers ticket creation. Observed: the write clauses were dropped in both escalate checklists
  (Acme escalation s5, Harborline s6/s7), and all 5 checklists kept only the first one or two options.
- Expected vs actual: unwritten-but-eligible should score < 100 and below a correct write; actual 100 == 100.
- Fix (reference-free, uses only the solver's own trace/output + public context): for escalate tasks with an
  authorised capability, add a deterministic `required_action_completion` criterion: every invoice the
  solver's own `invoice.assess` marked eligible and with no pre-existing active ticket must have a new
  audit + receipt + later read-back; `not_attempted` for such an invoice fails it. Do not rely on the selector.
- Tests: `test_assessed_eligible_authorized_but_unwritten_is_not_perfect`,
  `test_omitted_write_scores_strictly_below_correct_write`,
  `test_write_obligation_exists_even_when_selector_returns_nothing` (all xfail now; the control
  `test_control_correct_write_scores_100` passes; `test_solver_self_reported_not_attempted_still_scores_100`
  passes and documents the mechanism).
- Caveat: in the Harborline trace INV-H01 and INV-H02 were eligible and H02 already had a ticket. H01 is
  the unwritten required ticket, consistent with the independent grader's `exact_ticket_writes`.
  The new hook would have caught it only if H01 was eligible AND had no active ticket in the seed;
  the trace and the independent verdict both point that way, but I did not execute the proposed hook on it.

### F2 CRITICAL - The gate promoted on a false help (systematic, not a one-off)
- `r1-decision.json`: helped = [compound-escalation] (+18.18), Meridian -12.5 (inside noise floor),
  net +1.136 > required 0 -> promote. Replacing the false +18.18 with 0 gives mean -2.5 -> reject.
- Independent result before/after promotion: 3/5 -> 3/5. Fresh post-selection self-score for the same
  task dropped to 90.9 while the independent verdict stayed fail: the "gain" was not stable.
- Given F1 the bias is directional: any candidate that does fewer writes is rewarded by the gate.
- Test: add an end-to-end `run_rounds` fixture where the replay candidate omits the write and assert
  no promotion (not written here; needs the F1 fix to be meaningful).

### F3 MAJOR - Reviewer unreliability swamps the 15-point noise floor
- 12/31 (39%) semantic verdicts were "Reviewer failed to return a valid evidence-backed verdict."
  On 9-11 criterion checklists one invalid verdict moves a task 9-12.5 points; two flips exceed 15.
- The +18.18 was two flips: assessment_coverage (real) and req_1 invalid -> valid (reviewer noise).
- Raw reviewer replies are not retained (digests only), so the failure cause is undeterminable.
  Candidate cause (untested): `_text` (L108) rejects any `why` containing a newline.
- Fix: retain raw replies; retry once on format failure; treat reviewer-invalid as UNGRADED (Eq. 2: ungraded
  criteria are excluded), not failed; report the invalid rate per run and abort above a threshold.
- Paper note: "unparseable verdict counts as failed" (Sec 4.1) is followed literally, but the paper pairs it
  with deterministic fallback; here nothing falls back, so noise becomes signal.

### F4 MAJOR - Failure evidence given to the proposer is uninformative / mis-attributed
- Deterministic hooks return an identical rationale string on pass and fail (test
  `test_deterministic_rationale_is_identical_for_pass_and_fail` passes = defect confirmed).
  Long Acme's "incumbent_failure_evidence" is "Requested invoice coverage checked against ..." and never
  "customer.search limit=1; next_cursor not followed".
- Round 1 had 0 trajectory contrasts (`r1-regret.json`: contrasts=[]), because history held a single
  rollout per task with replicates=1; Alg. 1 line 12 / Box B.3 assume the proposer diagnoses from traces.
  Excerpts exist but reached only the repair path.
- Fix: include failing-check-specific, trace-derived facts (tool, args, observed `next_cursor`) in the
  rationale; feed run excerpts to the proposer in round 1.

### F5 MAJOR - Reviewer-format failures are routed to the proposer as solver failures
- Both candidates conditioned on the literal text "[iff The reviewer failed to return a valid
  evidence-backed verdict ...]" - feedback that does not exist at deploy time. Unsolved-gap ties at
  priority 1.0 sort by task name, so `acme-account-escalation/req_2` (a reviewer failure) ranked first,
  ahead of Long Acme's real pagination gaps. This explains both candidates targeting escalation.
- Test: `test_reviewer_invalid_verdict_is_not_routed_as_solver_failure` (xfail).
- Fix: exclude reviewer-invalid verdicts from `_diagnostics`; tie-break by deterministic-check failures first.

### F6 MAJOR - Candidate edits are not grounded in tool declarations or runtime facts
- The `candidate-generator` payload (`roles.py` `_PURPOSES`) contains no tool declarations, so invented
  arguments (`due_date_threshold`, `contractual_basis`, `due_date_evidence`) were unavoidable.
- `Edit.parse/from_proposal` accept rules that embed criterion/task IDs (candidate 1 cites `req_2`,
  `acme-account-escalation`, `compound-escalation`) or undeclared arguments; only `_evolve` rejects task IDs.
- Tests: `test_candidate_rule_may_not_embed_criterion_or_task_ids`,
  `test_candidate_rule_may_not_invent_tool_arguments` (xfail).
- Fix: pass compact declarations to the generator; validate any `name(...)`/JSON argument against them;
  reject IDs and reviewer vocabulary ("reviewer", "criterion", "verdict").

### F7 MAJOR - Experimental isolation caveat for the comparative stage
- Adaptation path never imports the scorer, rubrics or dataset expectations (grep clean); `audit.py` is
  post-selection only. Good.
- But `compound-escalation` is the sole `gate` split case and is in GUARD's replay buffer, and the
  pilot's training/replay set is the whole public development suite. The doc itself says the pilot cannot
  show generalisation. If comparative development testing uses the gate split, it is contaminated.
  Designate a fresh development split for comparison.

### F8 MINOR - Fidelity notes (no defect)
- Eqs. 2-5, 7-9, 11-13, pinning and rejected-winner hurt anchoring match `math.py`. Verified by reading;
  existing tests cover boundaries.
- Eq. 10: implementation uses `pool_size // 2` for the exploit half. Paper floor/ceil glyph is ambiguous in
  the PDF extraction; matches a floor reading. Only matters for odd pool sizes.
- Documented deviations (single appended rule, no scripts, same model for all roles, one fixed replay
  extension) are acknowledged in `docs/guard-phase-1.md`; not counted as bugs.
- Gate treats zero-help-with-hurt as reject (matches Eq. 13); repair uses margin 0 (matches).
- Rollback/conditional repair were not exercised live; only synthetic tests (`test_guard_loop.py`).
- Paired `evaluated` candidates run on training only; replay is run for the winner only. Documented, and
  consistent with Alg. 1 (candidates scored on Q_R).

## Calibration (archived pilot-06, 17 trials; no oracle given to GUARD)
| Metric | Result |
|---|---|
| Self strict = 100 but independently failed | 1 / 4 (25%) |
| Self continuous >= 90 but independently failed | 2 / 8 |
| Escalate with eligible invoice and no write, self-scored | 3 / 3 independently failed; self continuous 81.8, 100.0, 90.9 |
| Independent pass but self strict = 0 (false fail) | 3 (all Meridian) |
| Pagination incompleteness (Long Acme) | NOT a false pass: self and independent agree on fail |
| Semantic verdicts invalid | 12 / 31 (39%) |
Small n; treat rates as indicative. A live false-pass measurement on synthetic fixtures was not run
(no model server); the deterministic-layer tests above are the substitute.

## Negative results
- Pagination: stopping after page 1 correctly fails `invoice_coverage` (test passes).
- Read-only success cannot pass a write task via `read_only` (that hook is only in non-escalate tasks);
  the gap is solely the missing completion obligation (F1).
- No path from independent grader to the controller was found.

## Minimum required before APPROVED
1. Fix F1 (+ regression tests above flipped to passing) and rescore the archived Harborline rollouts
   offline to show the gate would have rejected.
2. Fix F3/F5 so reviewer errors are ungraded and never reach the proposer.
3. Fix F4/F6 so proposals are grounded.
4. Re-run the pilot on a dev split not reused for comparison (F7).
