# Configuration reference

Each project has a `spendcoder.toml`, created by `spendcoder init`. spendcoder finds it in the folder you point at, or in that folder's parents. Use the global `--project PATH` flag to select a project explicitly.

Paths in the file are resolved relative to the folder that holds `spendcoder.toml`. Absolute paths and `~` also work.

Defaults below are the built-in values. Keys marked **required** have no default.

## [project]

| Key | Default | Meaning |
|---|---|---|
| `name` | folder name | Display name for the project. |

## [dataset]

| Key | Default | Meaning |
|---|---|---|
| `path` | **required** | Card export to code (CSV). |
| `labeled_path` | none | Labeled export with known correct GL codes. Used by `spendcoder eval` and `spendcoder onboard`. |
| `amount_sign` | `1.0` | Multiplier applied to every parsed amount. Use `-1.0` if the export records charges as negative numbers (Airbase does; the `blank` template already sets it). |

Amount parsing removes `$`, commas, and spaces. A value in parentheses, such as `(12.50)`, is negative. Unparseable amounts stop the run and report the line numbers.

The delimiter is detected from comma, semicolon, tab, and pipe. Files must be UTF-8. A byte order mark is accepted.

### [dataset.columns]

Maps spendcoder fields to the header names in your export. Values must match the headers exactly.

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique transaction id. Empty or duplicate ids stop the run. |
| `description` | yes | Merchant descriptor as printed on the statement. |
| `amount` | yes | Transaction amount. |
| `date` | no | Transaction date. Kept for reference and written to the output. Not sent to models. |
| `note` | no | Cardholder memo. Sent to models. |
| `department` | no | Department code. Used by rules. |

Mapped headers must exist in the file. Unknown field names are rejected.

## [policy]

| Key | Default | Meaning |
|---|---|---|
| `categories` | **required** | Path to `categories.csv`. |
| `rules` | **required** | Path to `rules.csv`. |
| `vendor_overrides` | none | Path to `vendor_overrides.csv`. A path to a missing file is silently ignored. |
| `vendors` | none | Path to `vendors.txt`. A path to a missing file is silently ignored. |

### [policy.note_glossary]

A table of shorthand to plain-language meaning, for example program or project codes that appear in cardholder notes. Both keys and values are strings.

### [policy.keyword_aliases]

A table from a keyword to a list of alternate terms that count as a match for that keyword. Keys are lowercased on load.

```toml
[policy.keyword_aliases]
conference = ["summit", "symposium"]
```

## [llm]

| Key | Default | Meaning |
|---|---|---|
| `backend` | `"openrouter"` | `openrouter`, `agent-files`, or `mock`. The `--backend` flag overrides this for one run. |
| `chat_model` | `"openai/gpt-5.6-terra"` | Model for chat-style calls such as merchant normalization and review. |
| `decision_model` | `"typesafe/jev-1.13"` | Model for calibrated decisions such as categorize, select, and review flags. |
| `reflection_model` | `"openai/gpt-5.6-terra"` | Model used for GEPA reflection during onboarding. |
| `reasoning_effort` | `"low"` | Reasoning effort requested from OpenRouter models. |
| `max_concurrency` | `8` | Maximum parallel model calls. |
| `timeout_sec` | `120` | Timeout for one OpenRouter request. |
| `max_retries` | `4` | Retries for a failed or invalid OpenRouter answer before the call fails. |
| `normalize_batch_size` | `40` | Merchant descriptors per normalization call. |
| `agent_work_dir` | `"runs/agent_queue"` | Folder for agent-files requests, answers, and rejected answers. |
| `agent_timeout_sec` | `7200` | Seconds to wait for one agent answer before the run stops. |

OpenRouter responses are cached under `[paths] cache`, so a rerun does not pay for the same call twice. Run `spendcoder models` to list model ids.

## [search]

| Key | Default | Meaning |
|---|---|---|
| `provider` | `"none"` | `parallel` or `none`. `parallel` needs `PARALLEL_API_KEY`. |
| `mode` | `"fast"` | Search mode sent to the provider. |
| `max_results` | `3` | Results requested per merchant. |
| `max_chars_per_result` | `600` | Maximum excerpt length kept per result. |
| `only_unknown_merchants` | `true` | Search only merchants that spendcoder cannot yet identify. |

Search results are web content. They are passed to models as untrusted evidence.

## [gates]

| Key | Default | Meaning |
|---|---|---|
| `category_min_confidence` | `0.55` | A category decision below this is not accepted. The row is treated as Unknown (method `gated`). |
| `select_min_confidence` | `0.50` | A rule decision below this is not accepted. The row is treated as not decided (method `gated`). |
| `max_error_rate` | `0.05` | Largest share of model errors tolerated before the run fails. |

## [review]

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Run the review stage. |
| `low_confidence` | `0.5` | Rows whose final confidence is below this are sent to review. |

### [[review.flags]]

Each flag is a yes/no question the model judges for every transaction.

| Key | Default | Meaning |
|---|---|---|
| `name` | **required** | Flag name. Written to the `sc_flags` column. |
| `question` | **required** | Statement the model rates as true or false for each transaction. |
| `threshold` | `0.5` | The flag fires when the model's probability that the statement is true reaches this value. |
| `action` | `"review"` | `review`: the row goes to the reviewer. `hold`: the row is always unresolved and needs a person. |

```toml
[[review.flags]]
name = "dispute"
question = "Does the note or description suggest the charge is disputed or was not authorized?"
threshold = 0.5
action = "hold"
```

## [tune]

Settings for GEPA prompt tuning during onboarding. Requires the `tune` extra.

| Key | Default | Meaning |
|---|---|---|
| `components` | `["categorize_instructions", "category_guide", "select_instructions"]` | Text components GEPA may change. Valid names: `normalize_system`, `categorize_instructions`, `category_guide`, `select_instructions`, `review_system`. |
| `max_metric_calls` | `300` | Evaluation budget for GEPA. |
| `minibatch_size` | `6` | Examples per GEPA minibatch. |
| `split` | `[0.5, 0.25, 0.25]` | Train, validation, and test fractions of the labeled data. Must sum to 1. |
| `seed` | `7` | Random seed for the split and the optimizer. |

When onboarding promotes tuned text, it is saved to `prompts/components.json`. The `category_guide` may be reworded and must name every category exactly once, so tuning cannot add or drop categories.

## [paths]

| Key | Default | Meaning |
|---|---|---|
| `prompts` | `"prompts"` | Folder for `components.json` (tuned wording). |
| `runs` | `"runs"` | Run outputs and the agent queue. Gitignored. |
| `cache` | `".spendcoder_cache"` | Response cache for OpenRouter calls. Gitignored. |

## Policy files

### categories.csv

Columns: `category,description`.

- Category names must be unique, ignoring case.
- `description` is plain language. The model uses it to choose a category.
- `Unknown` is reserved. spendcoder adds it itself, so do not list it.

```csv
category,description
Coffee shop,Coffee and tea bought at cafes and kiosks.
Office supplies,"Paper, toner, pens, and other consumables."
```

### rules.csv

Columns: `rule_id,category,department,excluded_departments,amount_range,keywords,gl_code,gl_name`

| Column | Required | Meaning |
|---|---|---|
| `rule_id` | yes | Unique rule id. |
| `category` | yes | Must exist in `categories.csv` (ignoring case). |
| `department` | no | Blank means any department. Otherwise the transaction's department must match, ignoring case. |
| `excluded_departments` | no | Comma-separated departments this rule never applies to. |
| `amount_range` | no | Blank means any amount. `100` means exactly 100. `0-99.99` means an inclusive range. Matching uses the absolute amount, so refunds match like charges. Commas and `$` are ignored. Unreadable values are rejected. |
| `keywords` | no | Comma-separated terms. When several rules match, the selector uses keywords to break ties by matching the note literally. |
| `gl_code` | yes | GL account code to post. |
| `gl_name` | yes | GL account name. |

Commas inside a value must be quoted in the CSV.

```csv
rule_id,category,department,excluded_departments,amount_range,keywords,gl_code,gl_name
R-100,Coffee shop,,,,,6100,Meals - general
R-101,Coffee shop,,,0-25,"team, client",6105,Meals - small team
```

### vendor_overrides.csv

Columns: `vendor,category`.

Forces a category for a vendor name after normalization. The category must exist in `categories.csv`.

```csv
vendor,category
Example Cafe,Coffee shop
```

### vendors.txt

One reference vendor name per line. Blank lines are ignored. Used as spelling hints during normalization and to recognize known merchants.

## Environment variables and .env

| Variable | Used by |
|---|---|
| `OPENROUTER_API_KEY` | `openrouter` backend |
| `PARALLEL_API_KEY` | `[search] provider = "parallel"` |

spendcoder reads `.env` from the project folder at startup. Variables already set in the environment take precedence over `.env`. Values are never printed. `.env` is gitignored. Start from `.env.example`.

## Complete example

```toml
[project]
name = "Example card program"

[dataset]
path = "data/export.csv"
amount_sign = 1.0

[dataset.columns]
id = "Transaction ID"
description = "Merchant"
amount = "Amount"
date = "Posted Date"
note = "Memo"
department = "Dept"

[policy]
categories = "policy/categories.csv"
rules = "policy/rules.csv"
vendor_overrides = "policy/vendor_overrides.csv"
vendors = "policy/vendors.txt"

[llm]
backend = "openrouter"

[search]
provider = "none"

[gates]
category_min_confidence = 0.55
select_min_confidence = 0.50
max_error_rate = 0.05

[review]
enabled = true
low_confidence = 0.5
```
