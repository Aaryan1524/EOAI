# Contributing

Issues and pull requests are welcome, especially independent
re-runs, new task families, and reviews of the GUARD implementation.

## Ground rules

- **Never commit answers.** Fixture expectations, held-out manifests
  and anything that reveals a correct outcome stay outside the repo
  (the study uses `~/EOAI-private/`). Committed analysis files must
  contain no expectations.
- **Keep the agent free of learning logic.** Nothing under
  `src/enterprise_ops` may import `experiments` or contain review,
  regret or gate code.
- **Do not edit `experiments/guard/` to improve results.** The study
  pins those files by digest. A changed GUARD goes in a new, clearly
  named variant with its own pre-registration.
- **Money, dates and permissions belong to code.** Do not move a check
  that can be written deterministically into a prompt.
- **Report what actually ran.** Results in issues or PRs include the
  provenance the runners write (commit, model digest, Ollama version).

## Development

```sh
uv sync --locked --extra test --no-install-project
PYTHONPATH=src:. .venv/bin/python -m pytest -q
```

All tests must pass without a model server. Tests that need macOS
Seatbelt skip on other platforms.

## Proposing an experiment

Open an issue with the question, the arms, the splits and their sizes,
seeds, budgets, the metric, and the decision rule, written before any
run. Once agreed, the pre-registration is committed and tagged first;
results follow in a separate commit.
