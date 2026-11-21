# GUARD vs Naive RSI vs Frozen EOAI — blind comparative experiment report

**Conclusion: GUARD SHOWS LIMITED OR CONDITIONAL BENEFIT** (pre-registered rule §9(a): Δ > 0, p ≥ 0.05).

In plain terms: after adaptation, GUARD's harness passed more independent tasks than frozen EOAI
(+8.9 points). The gain did not reach the pre-registered significance level, and nearly all of it came
from one task family (paged review). Naive self-improvement and GUARD-without-veto produced gains of
similar size. The experiment did not show that GUARD's distinctive mechanism, regret-guided selection
plus Soft-HURT gating, did better than plain self-improvement. The audited GUARD also could not run at
the pre-registered scale, and it aborted in 2 of its 6 arm-runs (counting the no-veto ablation).

Pins: experiment branch `experiment/guard-comparative`, based on PR #11 merge 9a17c5b. Pre-registration is
tag `guard-comparative-prereg-v1` (9577971). Amendments are tags `guard-comparative-amend1` (00ada9f) and
`guard-comparative-amend2` (8fd1ab3). GUARD core files are byte-identical to the audited revision and are
checked at every phase. Raw evidence is in `runs/comparative/` (git-ignored). Private fixtures and answers
are in `~/EOAI-private/comparative-v1`. The sealed held-out suite was never touched.

## 1. What was run

| Run | Buffer | Arms | Outcome |
|---|---|---|---|
| Original trial 1 (`runs/comparative/trial-1`) | 20 adaptation + 10 gate | GUARD, GUARD-no-veto, Naive | Both GUARD arms aborted in round 1: the regret payload (≈156 KB) exceeds the 128 KiB role cap. Naive completed. Supplementary. |
| Amendment-1 trial 1 (`amend1/trial-1`) | 6 + 4 | GUARD | GUARD aborted in round 2: the 90 s role timeout was exceeded (`ModelError`, not retried). Stopped. |
| **Amended trials 1–3 (`amend2/`)** | 6 + 4, 300 s role timeout | all four arms | **Primary.** All 3 trials evaluable. |

Dev-eval: 30 fresh cases (5 per family × 6 families) per trial, each distinct final harness once, scored by
the independent deterministic oracle. Splits are disjoint from each other and from pilot-06 and its gate in
IDs, wording and SQLite lineage (`validate_disjoint`). Every expected decision was cross-checked against the
frozen policy engine.

Reviewer readiness: calibration on the 6 known catalog fixtures (12 rollouts) gave 0/22 unresolved semantic
judgments and no retries. Self-review vs oracle on those fixtures: 10/12 agree, 0 false positives,
2 false negatives (Meridian).

## 2. Primary results (amended trials, 30 dev-eval tasks; task is the unit, averaged over 3 trials)

| Arm | Pass rate | Trial 1 | Trial 2 | Trial 3 |
|---|---|---|---|---|
| A Frozen EOAI | **76.7 %** | 76.7 | 73.3 | 80.0 |
| B Naive RSI | 82.2 % | 80.0 | 80.0 | 86.7 |
| C GUARD (ITT) | **85.6 %** | 76.7 *(aborted → frozen)* | 90.0 | 90.0 |
| C′ GUARD without veto (ITT) | 84.4 % | 90.0 | 73.3 *(aborted → frozen)* | 90.0 |

| Paired comparison | Mean diff | Exact sign-flip p | Bootstrap 95 % CI |
|---|---|---|---|
| GUARD − Frozen | **+8.9 pp** | **0.094** | [+1.1, +17.8] |
| GUARD-no-veto − Frozen | +7.8 pp | 0.063 | [+2.2, +14.4] |
| Naive − Frozen | +5.6 pp | 0.125 | [+1.1, +11.1] |
| GUARD − Naive | +3.3 pp | 0.56 | [−3.3, +11.1] |
| GUARD − GUARD-no-veto | +1.1 pp | 1.0 | [−4.4, +7.8] |

Per-family pass rate (mean over trials):

| Family | Frozen | GUARD | No-veto | Naive |
|---|---|---|---|---|
| inventory, review, conflict, escalate_one | 100 | 100 | 100 | 100 |
| escalate_account | 53.3 | 60.0 | 53.3 | 53.3 |
| **paged_review** | **6.7** | **53.3** | **53.3** | **40.0** |

Newly broken and recovered tasks vs frozen in the same trial:
- GUARD: t2 recovered 5, broke 0; t3 recovered 4, broke 1 (`e23`).
- Naive: t1 +2/−1, t2 +2/−0, t3 +3/−1.
- No-veto: t1 +5/−1, t3 +3/−0.

Every "newly broken" task (`e21`, `e23`, escalate_account) also flips across the frozen arm's own runs
(frozen ranges from 73.3 to 80.0 %). This is the solver's nondeterminism floor, not attributable forgetting.
No arm showed family-level forgetting in the amended trials.

**Interpretation.**
- All three adaptive methods mainly learned one fix: following invoice-search continuation cursors in
  paged review. The adaptation buffer contained one paged-review task, and GUARD's round-1 candidates
  target exactly that.
- The benefit is real in direction: every bootstrap CI vs frozen excludes 0. It is not significant under
  the pre-registered permutation test, and it is not specific to GUARD.

## 3. Answers to the six questions

**Q1. Did GUARD improve independently verified correctness?**
Directionally yes: +8.9 pp, p = 0.094. That is not significant at the pre-registered 0.05 level, and it is
concentrated in one family. The two completed GUARD trials reached 90 %. The aborted trial counts as frozen,
as pre-registered.

**Q2. Did GUARD prevent regressions that naive RSI introduced?**
Not demonstrated.
- In the amended trials Naive introduced no regression beyond the noise floor, so there was nothing to
  prevent.
- In the supplementary full-buffer trial Naive did regress: escalate_account fell from 3/5 to 0/5, with 6
  required tickets missed, and its net was −3.3 pp despite +40 pp on paged review. This is exactly the
  failure GUARD is designed to stop, but GUARD could not run at that buffer size (payload cap), so the
  protection was never tested.

**Q3. How often did GUARD reject potentially beneficial changes?**
- GUARD made 8 gate decisions over 3 trials: 4 accepts and 4 rejects (insufficient gain/help; the veto
  co-occurred twice).
- Oracle checks on the buffer tasks found no rejected winner with a positive oracle change (rejected:
  −1, −2, 0, 0). Every accept had a non-negative change (+3, +1, +1, +4).
- The no-veto arm made 8 decisions (5 accepts, 3 rejects; oracle +3, 0, +1, +3, +1 on accepts; −1, 0, −1
  on rejects), and it attempted one scope repair (t3 r3), which failed its gate.
- Counterfactually, the veto would have blocked 2 of the no-veto arm's 5 promotions (§Q4), both
  oracle-positive.
- The gate's direction agreed with the oracle on the buffer every time it decided. Its losses came from
  aborts, not from wrong rejections.

**Q4. Did the mandatory-write veto materially change results?**
No, measurably.
- **Actual veto behaviour.** The veto fired in 2 GUARD gate calls (t1 r2, t2 r3) on `a14`. Both were cases
  where the incumbent had passed that write and the candidate regressed it, and the paper's Soft-HURT also
  rejected. Paper-vs-actual disagreement: 0 of 8 GUARD gate calls.
- **Frozen v0 and the re-audit's M1 scenario.** Frozen v0 failed the required write on `a14` in all 3
  trials. That is the scenario re-audit M1 predicted would make GUARD unable to promote. It did not, because
  promoted candidates fixed `a14`.
- **Shadow decision.** In the no-veto arm, 2 promotions (t2 r1, t3 r1) had candidates still missing the
  `a14` write that v0 also missed. The veto would have blocked them, and both were oracle-positive on the
  buffer (+1, +3). Whether those changes would have survived in GUARD is unknowable, because the
  trajectories differ through solver nondeterminism.
- **Dev-eval.** GUARD vs no-veto is +1.1 pp, p = 1.0.

**Q5. How accurately did self-review predict real correctness?**
- 404 unique adaptation/gate rollouts: accuracy 72.8 %, **false-positive rate 0 %** (0/63 oracle-failed
  runs certified), false-negative rate 32.3 % (110/341).
- All 110 false negatives have one cause. The model's final JSON choice was malformed (`invalid_final`),
  the frozen runner still produced a correct report, the oracle passed it, and GUARD's review treats it as
  a blocking error (strict score 0). The FNs fall on 4 tasks (`a04` 39/54, `a07` 51/54, `g04` 19/20,
  `g02` 1/20).
- So self-review is safe (never certifies a wrong run) but noisy: about a third of the buffer's apparent
  failures are not real. Proposals aimed at them waste budget.
- UNKNOWN rate: 0 of 714 semantic judgments in the amended trials (threshold 10 %). The re-audit's
  UNKNOWN-availability risk (M5) did not materialise.

**Q6. Was the extra computation and orchestration justified?**
Not on this evidence.
- **Cost per amended trial:**

  | | GUARD | Naive |
  |---|---|---|
  | Role calls | ≈93 | ≈75 |
  | Role tokens | 0.44–0.48 M | 0.35–0.38 M |
  | Solver rollouts | 48 | 36 |
  | Wall-clock | 25–27 min | 19–21 min |

  That is about 1.25–1.3× Naive.
- **Return.** +3.3 pp over Naive, p = 0.56.
- **Operational cost.** GUARD needed two protocol amendments to run at all: a 3× smaller buffer to fit
  its 128 KiB role cap, and a 3.3× longer role timeout. It aborted in 2/6 amended arm-runs on its
  duplicate-candidate check (`loop.py:361`) and in 3/3 earlier runs on payload or timeout. Naive aborted
  0/4.
- **Safety.** Zero unwarranted ticket writes in any arm and any run. No source tables were modified. All
  write errors are missed required tickets.

## 4. Failures and negative findings (kept in the record)

1. **Scale ceiling.** GUARD's regret grows by about 7 KB per buffer task (dominated by "mastered"
   criteria). At 30 tasks it exceeds the role relay's 128 KiB cap and the arm aborts before proposing.
   At 6+4 the worst-case repair payload is ≈113 KB.
2. **Latency ceiling.** GUARD's proposal prompts take 70–140 s on this host. The default 90 s timeout
   aborts by round 2, and a provider timeout is not retried.
3. **Duplicate-candidate abort.** When the proposer repeats a rejected harness 3 times, `run_rounds`
   raises and discards the whole arm, including earlier promotions (t1 GUARD lost a round-1 promotion;
   t2 no-veto likewise). See §5 for what those promotions were worth.
4. **Self-review false negatives** from `invalid_final` (§Q5).
5. **Low power.** 30 tasks and 3 trials. Trials differ only in role sampling, since the solver and reviewer
   run at temperature 0, so they are repeated measurements of the adaptation process, not new tasks.
6. **Confound.** GUARD's proposer sees replay-task evidence that Naive does not.

## 5. Exploratory (not pre-registered)

Dev-eval of the last promoted harness of each aborted arm (`experiments/comparative/exploratory.py`):

| Aborted arm | Last promoted harness | Dev-eval | Frozen, same trial | Recovered / broken |
|---|---|---|---|---|
| GUARD, trial 1 | `f1330819…` (round-1 accept) | 83.3 % | 76.7 % | +3 paged_review / −1 (`e21`, noise-level) |
| GUARD-no-veto, trial 2 | `68c1e563…` (round-1 accept) | 83.3 % | 73.3 % | +3 paged_review / −0 |

The aborts discarded real gains. If aborted arms had kept their last incumbent instead of the
pre-registered ITT fallback, GUARD's mean would be ≈87.8 % (vs 85.6 % ITT). This is descriptive only:
no test was run, and it does not change the pre-registered conclusion. It does suggest that GUARD's
duplicate-candidate abort, which discards the whole run rather than ending it with the current
incumbent, costs measurable accuracy.

## 6. Deviations from pre-registration

Amendment 1 (buffer 20+10 → 6+4, score-blind rule, sized offline) and Amendment 2 (role timeout 90 → 300 s)
are recorded with their triggers in `docs/comparative-preregistration.md` §Deviations. In both cases only
abort messages, latencies and payload sizes had been seen, and no oracle or dev-eval result. Analysis code
fixes after results: null-token handling in cost aggregation, and including the gate audit for aborted arms.
Neither changes any statistic's definition.

## 7. Reproduce

```sh
PYTHONPATH=src:. .venv/bin/python -m experiments.comparative.fixtures --out <private>/comparative-v1   # seed 20261120
./experiments/comparative/run_all.sh 3 amend1 runs/comparative/amend2 300
PYTHONPATH=src:. .venv/bin/python -m experiments.comparative.analyze --trial runs/comparative/amend2/trial-{1,2,3} \
  --deveval runs/comparative/amend2/deveval-{1,2,3} --out runs/comparative/amend2/analysis.json
```
