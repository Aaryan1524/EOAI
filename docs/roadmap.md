# Roadmap

What the first study could not answer, and how the next ones will be
run. Every item below becomes its own pre-registered experiment. The
locked study in [`comparative-report.md`](comparative-report.md) is
never edited to include them.

## What the first study left open

1. **Is the small model the bottleneck?** Every GUARD role ran on
   `qwen3:4b-instruct`. The self-reviewer failed 32% of runs the oracle
   judged correct, and the proposer converged on near-identical edits
   by round 3.
2. **Does GUARD's gate prevent real regressions?** On the full 20-task
   buffer the naive arm broke account escalation (3/5 → 0/5), but
   GUARD could not run at that size, so its protection was never
   tested.
3. **Is the write veto useful?** It never decided a gate call on its
   own. As a shadow decision it would have blocked 2 promotions that
   helped.
4. **Is the gain more than one fix?** Nearly all improvement came from
   paged review, which every adaptive arm learned.

## Planned experiments

### v2: stronger model in GUARD's roles

- Keep the solver fixed; replace the reviewer, proposer and distiller
  with a stronger model. Give the naive arm the same model, so the
  comparison measures GUARD rather than the upgrade.
- Requirements for the model: context of at least 40k tokens, seeded
  sampling, and local inference (or an explicit decision to send
  private fixtures to an API).
- Measure self-review false-negative rate first. If it does not drop,
  the reviewer, not the loop, is the limit.

### v2b: GUARD with scaling fixes (labelled variant)

Run alongside unchanged GUARD, never in place of it.

- Bound the regret report: rank failures and cap per-task evidence, so
  prompt size stops growing with the buffer.
- Replace abort-on-repeat with skip-the-slot, and keep earlier
  promotions when a later round fails.
- Re-run the pre-registered 20 + 10 buffer.

### Power

- About 100 dev-eval tasks and 5 trials, so differences of roughly
  5 points can reach significance. The required n is fixed in the
  pre-registration from the first study's variance.
- More task families, so gains cannot come from one fix.

### Final sealed suite

Run once, at the end of all development, on the single harness each
arm commits to. Never used to choose between variants.

## Contributions that help

- An independent run of the study on different hardware or Ollama
  versions, reported with provenance.
- New task families with oracle expectations. See
  [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the rules on keeping answers
  private.
- A comparison against the GUARD authors' code, if it is released.
