# GUARD comparative experiment - preflight (NOT an experiment result)

Status: BLOCKED before pre-registration. No tasks were run, no scores observed, sealed suite untouched.

## Verified
- PR #10 merged 2026-11-20T17:04:39Z (merge 2415163). PR #11 merged 2026-11-20T18:15:07Z (merge 9a17c5b).
  origin/main head = 9a17c5b, so the audited PR #11 code (head 79c120d) is the pinned experiment commit.
- Frozen tag eoai-baseline-v0 -> commit 17f12c4; model qwen3:4b-instruct sha256 0edcdef3...; temp 0.0, seed 0, ctx 32768.
- Host: macOS, 11 CPUs, 18 GiB RAM, 63 GiB free disk.

## Blockers
1. Ollama server is not running (`could not connect to ollama server`); `ollama list` cannot be queried,
   so it is unknown whether the pinned model digest is installed. Live reviewer calibration (Step 2) cannot start.
2. Naive RSI arm (Arm B) does not exist anywhere on origin/main or remote branches. A minimal implementation
   needs its own separate review before any three-arm claim.
3. 20 adaptation + 10 gate + 30 dev-eval fresh tasks with disjoint IDs/wording/snapshot lineages do not exist;
   only 6 catalog tasks do (all in pilot-06 / its gate). They need authoring, plus oracle expectations kept
   outside adaptation components.
4. Local checkout is on branch development/guard-audit-remediation, not main; use a worktree of origin/main.

## Next steps (in order)
a. Start Ollama, confirm model digest. b. Reviewer calibration on known dev fixtures; fix UNKNOWN threshold (10%).
c. Author and validate fresh task splits. d. Get Naive RSI implemented and reviewed. e. Write and commit the
pre-registration before running anything. f. Run v0 on new fixtures first (veto feasibility, per re-audit condition 2).
