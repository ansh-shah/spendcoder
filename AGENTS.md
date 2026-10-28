# AGENTS.md

Instructions for AI coding agents contributing to spendcoder. Read this before changing code.

## Layout

```
pyproject.toml              package metadata (setuptools, src layout), pytest config
LICENSE
.gitignore                  ignores runs/, .spendcoder_cache/, private/, .env
src/spendcoder/
  config.py                 spendcoder.toml loader and dataclasses (defaults live here)
  data.py                   CSV load/write, Txn model, sc_* output columns, evidence_view()
  envfile.py                .env loader (never overwrites real environment variables)
  policy.py                 categories, rules, deterministic eligibility (Policy.eligible)
  prompts.py                tunable text components and the category guide
  stages.py                 normalize, categorize, match, select, review
  pipeline.py               open_project, run directory, doctor, labeled scoring
  cli.py                    argparse CLI (spendcoder ...)
  __main__.py               python -m spendcoder entry point
  tune/                     GEPA adapter and onboarding (optional `gepa` dependency)
  templates/                `spendcoder init` scaffolds (acme demo, blank)
  llm/
    base.py                 Backend protocol, Ledger, answer validators
    agent_files.py          agent-files backend and queue helpers
    cache.py                on-disk response cache around any backend
    factory.py              backend selection (make_backend)
    mock.py                 offline backend
    openrouter.py           OpenRouter backend
  search/
    __init__.py             web-evidence exports
    parallel.py             Parallel Search API client (cached, best-effort)
tests/                      offline tests (no network)
scripts/make_synthetic_data.py  regenerates the synthetic demo data
docs/configuration.md       user-facing configuration reference
skills/spendcoder/SKILL.md  agent skill for agent-files mode
```

## Run tests

```
pip install -e ".[dev]"
pytest -q
```

Tests must run offline. Use the mock backend or fake the HTTP layer with monkeypatch, as `tests/test_backends.py` does.

## Invariants (never break these)

1. Eligibility is deterministic and lives in `policy.py`. `Policy.eligible` is the only place that decides which rules a transaction may use. No model decides eligibility.
2. Every model answer is validated in `llm/base.py` before use, with `validate_chat` and `validate_decision`. Every backend, including the cache and agent-files, goes through these validators.
3. Review is checked per row. A review may change a row's rule only to a rule that is eligible for that row, not just for its group. Stay within `legal_options`.
4. Input is never modified, and original columns and row order are preserved. Output appends the `sc_*` columns (`data.py`, `OUTPUT_COLUMNS`).
5. The issuer's own category is never sent to a model. Models see only `Txn.evidence_view()`, so add fields there deliberately.
6. GEPA tuning changes wording only. Tuned components are text, defaulting to `DEFAULT_COMPONENTS` in `prompts.py` and saved in `prompts/components.json`. Tuning never changes categories, rules, GL codes, amounts, or eligibility.
7. No real data in the repo. Use `scripts/make_synthetic_data.py` for fixtures, and keep `runs/`, caches, `.env`, and `private/` gitignored.

## Style

- The core is standard library only (`dependencies = []` in `pyproject.toml`). Optional dependencies go under `[project.optional-dependencies]`.
- Type hints everywhere. Start modules with `from __future__ import annotations`.
- Small modules with one responsibility each. Use dataclasses for records. Each module defines its own error class (`ConfigError`, `PolicyError`, `DataError`, `BackendError`).
- Fail closed on bad input, with a message that names the file and the row or rule.
- Never print or log secret values.
- Prefer editing an existing module to adding a new one.

## Where to add things

- A backend: create `src/spendcoder/llm/<name>.py` implementing `chat_json`, `decide`, and `chat_text` (the `Backend` protocol in `base.py`). Validate every answer with `validate_chat` or `validate_decision`. Register the backend in `make_backend` in `llm/factory.py`. Add its name to the allowed list in `config.py` (the `llm.backend` check). Document it in `docs/configuration.md`.
- A stage: add one module under `src/spendcoder/` that reads and writes `Txn` fields. For a new output column, add a field to `Txn`, add the name to `OUTPUT_COLUMNS`, and set it in `txn_output_fields()` in `data.py`. Stages reach models only through `evidence_view()`.
- A review flag: flags are configured per project with `[[review.flags]]` in `spendcoder.toml`, so most new flags need no code. A new flag name is recorded in `Txn.flags` and the `sc_flags` column. Code changes are needed only for a new action type or new flag logic.
- A config key: add the dataclass field and its default in `config.py`, parse it in `load_config`, and document it in `docs/configuration.md`. Defaults in code and docs must match.
- A prompt component: add it to `DEFAULT_COMPONENTS` and `TUNABLE` in `prompts.py`, and to the valid names in `docs/configuration.md`.
