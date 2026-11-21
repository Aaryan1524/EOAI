# Independent review of the comparative-experiment code (before lock)

A fresh-context reviewer (no shared working context) examined `experiments/comparative/{naive,runner,fixtures}.py`
and `tests/test_comparative.py` at the pre-lock state. Full suite: 416 passed. `git diff origin/main -- experiments/guard`
empty. No BLOCKER.

| Finding | Severity | Resolution |
|---|---|---|
| Role seeds `10t+k` overlapped between arms (guard/noveto shared one round-1 seed by accident) and across trials | MAJOR | Widely spaced streams; GUARD and no-veto share one stream *deliberately* (common random numbers); recorded in prereg §4 |
| Aborted arms silently dropped from deveval; non-caught exceptions left no `completed` key; `--arms` subset crashed deveval | MAJOR | All exceptions recorded; missing/aborted arms scored as frozen (ITT) and abort rate reported (§7) |
| GUARD gets extra budget (dedupe regenerations, replay, repair, contrast) and ≈50 % more proposer evidence | MAJOR (interpretation) | Naive given within-pool dedupe and traces of all training tasks; remaining differences declared GUARD mechanism/overhead and reported (§4) |
| Naive used different initial strategy text | MINOR | Naive now uses GUARD's `SHARED_STRATEGY`, never evolved |
| Naive compares candidates over graded criteria only | MINOR | Stated in §4 |
| `__main__.py`/`audit.py` hashed but not pinned | MINOR | Pinned |
| deveval lacked harness-render, stored-digest and trial-protocol checks | MINOR | Added |
| trial process does not check deveval disjointness | MINOR | `validate` is now step 0 of `run_all.sh` |
| Low power, deterministic solver/reviewer | MINOR | Stated in §5 and §9 |

Confirmed OK: naive carries no regret/hurt/gap/mastered/contrast/ledger information; `no_veto_gate` equals
`promotion_gate` minus the veto and the patch reaches both gate call sites and is restored; `Solver` matches
GUARD `solve`; no path from expectations or the deveval split into any adaptation component.
