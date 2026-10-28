# Contributing to spendcoder

spendcoder requires Python 3.11 or newer.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Tests

```bash
pytest -q
```

Tests run offline. CI runs them on Python 3.11, 3.12, and 3.13, then runs a smoke test with the mock backend.

## Pull request checklist

- [ ] `pytest -q` passes locally.
- [ ] The mock smoke run works in a fresh project: `spendcoder init`, `doctor`, `onboard --backend mock`, `run --backend mock`.
- [ ] Core code stays standard library only. New dependencies go under `[project.optional-dependencies]`.
- [ ] New config keys are added to `src/spendcoder/config.py` and documented in `docs/configuration.md`, with matching defaults.
- [ ] Model answers are validated before use. Eligibility stays in `policy.py`.
- [ ] The diff has no real transaction data, real company or person names, API keys, `.env` files, or `runs/` or cache output.

## Never commit real transaction data

Card exports, labeled data, run outputs, caches, and `.env` files stay out of the repository. Generate test fixtures with `scripts/make_synthetic_data.py`.
