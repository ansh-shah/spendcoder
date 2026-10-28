# Acme Field Services (synthetic demo)

Demo project for spendcoder. Acme Field Services is an invented HVAC and plumbing company.
All data here is synthetic: names, memos, amounts and dates were generated.
Merchants are generic public brands or obviously fake local businesses.

Regenerate (seeded, deterministic): public\.venv\Scripts\python.exe public\scripts\make_synthetic_data.py

- spendcoder.toml: settings (columns, policy paths, LLM backend, gates, review flags, tuning)
- policy/categories.csv: categories the model may assign
- policy/rules.csv: category x department x amount to GL code; keywords break ties
- policy/vendor_overrides.csv: vendors that always map to one category
- policy/vendors.txt: reference vendor names for matching
- data/transactions.csv: 240 synthetic card transactions to code
- data/labeled.csv: 120 transactions with expected category and GL code, for onboarding
