"""End-to-end pipeline and CLI tests on the acme template, offline (mock backend)."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

from spendcoder import cli, pipeline
from spendcoder.config import load_config
from spendcoder.data import DataError, load_transactions, parse_amount
from spendcoder.policy import load_policy

SILENT = lambda *_: None  # noqa: E731 - pipeline log sink


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def input_rows(project: Path) -> list[dict[str, str]]:
    return read_csv(load_config(project).dataset.path)


@pytest.fixture
def run_t1(ctx) -> Path:
    return pipeline.run(ctx, run_id="t1", log=SILENT)


# --------------------------------------------------------------------------- #
#  run outputs and invariants
# --------------------------------------------------------------------------- #
def test_run_writes_every_output(run_t1):
    for name in ("coded.csv", "review_queue.csv", "audit.jsonl", "manifest.json", "summary.md"):
        assert (run_t1 / name).is_file(), name
    assert "spendcoder run `t1`" in (run_t1 / "summary.md").read_text(encoding="utf-8")


def test_coded_rows_keep_input_count_order_and_columns(run_t1, input_rows):
    coded = read_csv(run_t1 / "coded.csv")
    assert len(coded) == len(input_rows) == 240
    assert [r["Transaction ID"] for r in coded] == [r["Transaction ID"] for r in input_rows]
    # original columns pass through untouched
    original = list(input_rows[0].keys())
    for got, want in zip(coded, input_rows):
        assert {k: got[k] for k in original} == want
    assert "sc_final_gl_code" in coded[0]


def test_limit_codes_only_the_first_rows(ctx, input_rows):
    out = pipeline.run(ctx, run_id="lim", limit=5, log=SILENT)
    coded = read_csv(out / "coded.csv")
    assert [r["Transaction ID"] for r in coded] == [r["Transaction ID"] for r in input_rows[:5]]


def test_final_rule_is_eligible_and_gl_pair_matches_rules_csv(ctx, run_t1):
    rules = {r["rule_id"]: r for r in read_csv(ctx.cfg.policy.rules)}
    finals = 0
    for row in read_csv(run_t1 / "coded.csv"):
        rid = row["sc_final_rule_id"]
        if not rid:
            assert row["sc_final_gl_code"] == "" and row["sc_final_gl_name"] == ""
            continue
        finals += 1
        assert rid in row["sc_candidate_rules"].split("|"), row["Transaction ID"]
        assert (row["sc_final_gl_code"], row["sc_final_gl_name"]) == (rules[rid]["gl_code"], rules[rid]["gl_name"])
    assert finals > 0


def test_candidates_equal_deterministic_eligibility(ctx, run_t1):
    """The candidate list is exactly what policy.eligible returns; no model is involved."""
    cols = ctx.cfg.dataset.columns
    checked = 0
    for row in read_csv(run_t1 / "coded.csv"):
        cat = row["sc_category"]
        candidates = [c for c in row["sc_candidate_rules"].split("|") if c]
        if not cat:
            assert candidates == [], row["Transaction ID"]
            continue
        amount = parse_amount(row[cols["amount"]]) * ctx.cfg.dataset.amount_sign
        expected = [r.rule_id for r in ctx.policy.eligible(cat, row.get(cols.get("department", ""), ""), amount)]
        assert set(candidates) == set(expected), row["Transaction ID"]
        checked += 1
    assert checked > 0


def test_review_queue_rows_carry_a_reason(run_t1):
    queue = read_csv(run_t1 / "review_queue.csv")
    coded = read_csv(run_t1 / "coded.csv")
    assert queue, "acme should send some rows to review"
    assert all(q["reason"] for q in queue)
    unresolved = sum(1 for r in coded if r["sc_review_status"] == "unresolved" or not r["sc_final_gl_code"])
    assert len(queue) == unresolved


def test_audit_has_one_lineage_record_per_row(run_t1, input_rows):
    records = [json.loads(line) for line in (run_t1 / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in records] == [r["Transaction ID"] for r in input_rows]
    assert all({"vendor", "category", "eligible_rules", "selection", "review", "final"} <= r.keys() for r in records)


def test_manifest_records_inputs_policy_and_stats(run_t1, ctx):
    m = json.loads((run_t1 / "manifest.json").read_text(encoding="utf-8"))
    assert m["run_id"] == "t1" and m["backend"] == "mock"
    assert m["stats"]["rows"] == 240
    assert m["stats"]["coded"] > 0
    assert len(m["inputs"]["transactions"]["sha256"]) == 64
    assert m["policy"]["rules"] and m["policy"]["categories"]


def test_rerun_with_same_run_id_raises(ctx, run_t1):
    with pytest.raises(FileExistsError, match="t1"):
        pipeline.run(ctx, run_id="t1", log=SILENT)


def test_dispute_memos_are_held_unresolved(ctx, run_t1):
    cols = ctx.cfg.dataset.columns
    rows = read_csv(run_t1 / "coded.csv")
    disputed = [r for r in rows if "dispute" in r[cols["note"]].lower()]
    assert disputed, "acme should contain dispute memos"
    queue_ids = {q["id"] for q in read_csv(run_t1 / "review_queue.csv")}
    for r in disputed:
        assert "dispute" in r["sc_flags"].split("|"), r["Transaction ID"]
        assert r["sc_review_status"] == "unresolved", r["Transaction ID"]
        assert r["sc_final_rule_id"] == "" and r["sc_final_gl_code"] == "" and r["sc_final_gl_name"] == ""
        assert r["Transaction ID"] in queue_ids


def test_reviewer_illegal_rule_is_rejected_per_row(ctx, monkeypatch):
    """A reviewer may only pick a rule that is legal for the row; anything else is unresolved."""
    original = ctx.backend.chat_json

    def hostile_reviewer(**kw):
        if kw["purpose"] != "review":
            return original(**kw)
        groups = json.loads(kw["user"])["groups"]
        return {"reviews": [{"group_id": g["group_id"], "action": "change", "rule_id": "NOPE",
                             "reason": "forced"} for g in groups]}

    monkeypatch.setattr(ctx.backend, "chat_json", hostile_reviewer)
    out = pipeline.run(ctx, run_id="illegal", log=SILENT)
    rows = read_csv(out / "coded.csv")
    rejected = [r for r in rows if "reviewer answer rejected" in r["sc_review_reason"]]
    assert rejected, "expected at least one reviewed row"
    for r in rejected:
        assert r["sc_review_status"] == "unresolved"
        assert r["sc_final_rule_id"] == "" and r["sc_final_gl_code"] == ""
    assert not any(r["sc_review_status"] == "changed" for r in rows)
    assert not any("NOPE" in r["sc_final_rule_id"] for r in rows)


def test_unparseable_amount_stops_the_run(project):
    cfg = load_config(project)
    path = cfg.dataset.path
    path.write_text(path.read_text(encoding="utf-8").replace("256.73", "nan", 1), encoding="utf-8")
    with pytest.raises(DataError):
        load_transactions(cfg.dataset)


def test_doctor_on_acme_with_mock_is_ready(project):
    cfg = load_config(project)
    cfg.llm.backend = "mock"
    problems, notes = pipeline.doctor(cfg)
    assert problems == []
    assert any(n.startswith("policy:") for n in notes)


def test_doctor_reports_missing_openrouter_key(project, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    problems, _ = pipeline.doctor(load_config(project))  # acme defaults to the openrouter backend
    assert any("OPENROUTER_API_KEY" in p for p in problems)


def test_policy_on_acme_loads_without_error(project):
    pol = load_policy(load_config(project).policy)
    assert len(pol.rules) == 38 and len(pol.categories) == 24


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def test_cli_init_run_doctor_round_trip(tmp_path, capsys):
    target = tmp_path / "x"
    assert cli.main(["init", str(target)]) == 0
    assert "spendcoder doctor" in capsys.readouterr().out

    assert cli.main(["--project", str(target), "doctor", "--backend", "mock"]) == 0
    assert "ready" in capsys.readouterr().out

    assert cli.main(["--project", str(target), "run", "--backend", "mock", "--run-id", "s"]) == 0
    assert (target / "runs" / "s" / "summary.md").is_file()


def test_cli_init_refuses_a_non_empty_folder(tmp_path, capsys):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("mine", encoding="utf-8")
    assert cli.main(["init", str(busy)]) == 1
    assert "not an empty folder" in capsys.readouterr().err
    assert (busy / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_cli_init_blank_template(tmp_path):
    target = tmp_path / "blank"
    assert cli.main(["init", str(target), "--template", "blank"]) == 0
    assert (target / "spendcoder.toml").is_file() and (target / "policy" / "rules.csv").is_file()


def test_cli_rerun_same_run_id_is_a_clean_error(tmp_path, capsys):
    target = tmp_path / "x"
    cli.main(["init", str(target)])
    capsys.readouterr()
    assert cli.main(["--project", str(target), "run", "--backend", "mock", "--run-id", "dup"]) == 0
    capsys.readouterr()
    assert cli.main(["--project", str(target), "run", "--backend", "mock", "--run-id", "dup"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "already exists" in err and "Traceback" not in err


def test_cli_doctor_reports_problems_and_exits_1(project, capsys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert cli.main(["--project", str(project), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "  x " in out and "OPENROUTER_API_KEY" in out and "ready" not in out


def test_cli_unknown_project_is_a_clean_error(tmp_path, capsys):
    assert cli.main(["--project", str(tmp_path / "nowhere"), "doctor"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "spendcoder.toml" in err


def test_cli_eval_prints_metrics(project, capsys):
    assert cli.main(["--project", str(project), "eval", "--backend", "mock"]) == 0
    out = capsys.readouterr().out
    assert "labeled rows: 120" in out and "category accuracy:" in out and "GL accuracy:" in out


def test_cli_onboard_without_gepa_exits_2(project, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "gepa", None)  # makes `import gepa` raise ImportError
    assert cli.main(["--project", str(project), "onboard", "--backend", "mock"]) == 2
    assert "GEPA is not installed" in capsys.readouterr().err


def test_cli_queue_list_show_and_answer(project, capsys):
    work = load_config(project).llm.agent_work_dir
    (work / "pending").mkdir(parents=True)
    (work / "responses").mkdir(parents=True)
    rid = "categorize-0123456789abcdef"
    request = {
        "id": rid, "kind": "decision", "purpose": "categorize", "created_at": "2026-10-27T00:00:00+00:00",
        "state": {"description": "STAPLES 00129"},
        "questions": {"category": {"type": "choice", "instructions": "i",
                                   "criteria": {"Office supplies": "paper", "Unknown": "none"}}},
    }
    (work / "pending" / f"{rid}.json").write_text(json.dumps(request), encoding="utf-8")
    base = ["--project", str(project), "queue"]

    assert cli.main([*base, "list"]) == 0
    assert rid in capsys.readouterr().out

    assert cli.main([*base, "show", rid]) == 0
    assert json.loads(capsys.readouterr().out)["purpose"] == "categorize"

    bad = project / "bad.json"
    bad.write_text(json.dumps({"category": {"type": "choice", "choice": "Hotel"}}), encoding="utf-8")
    assert cli.main([*base, "answer", rid, str(bad)]) == 1
    assert "not an option" in capsys.readouterr().err
    assert not (work / "responses" / f"{rid}.json").exists()

    good = project / "good.json"
    good.write_text(json.dumps({"category": {"type": "choice", "choice": "Office supplies",
                                             "probabilities": {"Office supplies": 0.9, "Unknown": 0.1}}}),
                    encoding="utf-8")
    assert cli.main([*base, "answer", rid, str(good)]) == 0
    assert "accepted" in capsys.readouterr().out
    assert (work / "responses" / f"{rid}.json").is_file()


def test_cli_queue_empty_and_unknown_request(project, capsys):
    assert cli.main(["--project", str(project), "queue", "list"]) == 0
    assert "queue empty" in capsys.readouterr().out
    assert cli.main(["--project", str(project), "queue", "show", "nope"]) == 1
    assert "no request 'nope'" in capsys.readouterr().err


def test_cli_models_without_key_is_a_clean_error(project, capsys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert cli.main(["--project", str(project), "models"]) == 1
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err
