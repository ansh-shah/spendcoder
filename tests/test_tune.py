"""Offline tests for the GEPA onboarding module (mock backend, acme demo project)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import spendcoder
from spendcoder.pipeline import open_project
from spendcoder.policy import descriptor_key
from spendcoder.prompts import parse_category_guide
from spendcoder.tune import evaluate_components, load_labeled, onboard
from spendcoder.tune.adapter import MockProposer, SpendcoderAdapter
from spendcoder.tune.onboard import TuneError, split_by_merchant

pytest.importorskip("gepa")


@pytest.fixture()
def ctx(tmp_path):
    shutil.copytree(Path(spendcoder.__file__).parent / "templates" / "acme", tmp_path / "p")
    return open_project(tmp_path / "p", backend="mock")


@pytest.fixture()
def labeled(ctx):
    return load_labeled(ctx)


def test_split_has_no_merchant_leakage_and_covers_all_rows(ctx, labeled):
    parts = split_by_merchant(labeled, ctx.cfg.tune.split, ctx.cfg.tune.seed)
    assert all(parts)
    ids = [t.id for p in parts for t in p]
    assert sorted(ids) == sorted(t.id for t in labeled)
    keys = [{descriptor_key(t.description) for t in p} for p in parts]
    assert not (keys[0] & keys[1]) and not (keys[0] & keys[2]) and not (keys[1] & keys[2])
    # deterministic
    again = split_by_merchant(labeled, ctx.cfg.tune.split, ctx.cfg.tune.seed)
    assert [[t.id for t in p] for p in again] == [[t.id for t in p] for p in parts]


def test_split_needs_three_merchants(labeled):
    few = [t for t in labeled if descriptor_key(t.description) in
           {descriptor_key(labeled[0].description), descriptor_key(labeled[1].description)}]
    with pytest.raises(TuneError, match="at least 3"):
        split_by_merchant(few, (0.5, 0.25, 0.25), 1)


def test_evaluate_components_baseline(ctx, labeled):
    m = evaluate_components(ctx, labeled)
    assert m["rows"] == len(labeled)
    assert 0.1 < m["score"] < 0.7
    assert all(t.category == "" for t in labeled), "inputs must not be mutated"


def test_invalid_guide_scores_zero_without_raising(ctx, labeled):
    adapter = SpendcoderAdapter(ctx, ctx.comps)
    batch = labeled[:6]
    bad = "\n".join(ctx.comps["category_guide"].splitlines()[1:])  # drops Restaurant
    out = adapter.evaluate(batch, {"category_guide": bad}, capture_traces=True)
    assert out.scores == [0.0] * 6
    assert "Restaurant" in out.trajectories[0]["feedback"]
    invented = ctx.comps["category_guide"] + "\nCryptocurrency: coins"
    assert adapter.evaluate(batch, {"category_guide": invented}).scores == [0.0] * 6
    dup = ctx.comps["category_guide"] + "\nFuel: again"
    assert adapter.evaluate(batch, {"category_guide": dup}).scores == [0.0] * 6
    assert adapter.evaluate(batch, {"not_a_component": "x"}).scores == [0.0] * 6
    good = adapter.evaluate(batch, {"category_guide": ctx.comps["category_guide"]})
    assert sum(good.scores) > 0


def test_reflective_dataset_shape(ctx, labeled):
    adapter = SpendcoderAdapter(ctx, ctx.comps)
    comps = {k: ctx.comps[k] for k in ("category_guide", "select_instructions")}
    eb = adapter.evaluate(labeled[:30], comps, capture_traces=True)
    ds = adapter.make_reflective_dataset(comps, eb, ["category_guide", "select_instructions"])
    assert set(ds) == {"category_guide", "select_instructions"}
    recs = ds["category_guide"]
    assert recs and len(recs) <= 14
    for r in recs:
        assert {"Inputs", "Generated Outputs", "Feedback"} <= set(r)
        assert "category" not in r["Inputs"]
    failing = [r for r in recs if r["Feedback"] != "correct"]
    assert failing and all("one `Category: description` line per category" in r["Feedback"] for r in failing)
    assert any("allowed categories" in r["Feedback"] for r in failing)
    json.dumps(ds)  # must be JSON-serializable


def test_mock_proposer_output_parses_and_adds_tokens(ctx, labeled):
    adapter = SpendcoderAdapter(ctx, ctx.comps)
    comps = {"category_guide": ctx.comps["category_guide"]}
    eb = adapter.evaluate(labeled, comps, capture_traces=True)
    ds = adapter.make_reflective_dataset(comps, eb, ["category_guide"])
    proposer = MockProposer(ctx.policy)
    new = proposer(comps, ds, ["category_guide"], extra="ignored")
    assert new["category_guide"] != comps["category_guide"]
    parse_category_guide(new["category_guide"], ctx.policy.categories)
    assert len(new["category_guide"]) > len(comps["category_guide"])
    assert proposer(comps, ds, ["category_guide"]) == new, "must be deterministic"
    # other components pass through unchanged
    other = proposer({"select_instructions": "abc"}, {}, ["select_instructions"])
    assert other == {"select_instructions": "abc"}
    # an unparseable guide is returned unchanged rather than raising
    assert proposer({"category_guide": "Nope: x"}, ds, ["category_guide"]) == {"category_guide": "Nope: x"}


def test_onboard_apply_improves_and_writes_artifacts(ctx):
    path = ctx.cfg.components_path
    assert not path.exists()
    r = onboard(ctx, apply=True, budget=150, log=lambda *_: None)
    assert r["tuned"]["train"]["score"] > r["baseline"]["train"]["score"]
    assert (r["tuned"]["val"]["score"] > r["baseline"]["val"]["score"]
            or r["tuned"]["test"]["score"] > r["baseline"]["test"]["score"]
            or r["tuned"]["train"]["score"] > r["baseline"]["train"]["score"])
    run_dir = r["run_dir"]
    for name in ("report.md", "result.json", "components.tuned.json"):
        assert (run_dir / name).exists()
    res = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert res["num_candidates"] == r["num_candidates"] and res["metric_calls"] == r["metric_calls"]
    assert len(res["val_aggregate_scores"]) == r["num_candidates"]
    report = r["report_path"].read_text(encoding="utf-8")
    assert "```diff" in report and "Candidate lineage" in report
    tuned = json.loads((run_dir / "components.tuned.json").read_text(encoding="utf-8"))
    parse_category_guide(tuned["category_guide"], ctx.policy.categories)
    if r["promoted"]:
        assert path.exists() and json.loads(path.read_text(encoding="utf-8")) == tuned
        assert ctx.comps == tuned
        reloaded = open_project(ctx.cfg.root, backend="mock")
        assert reloaded.comps == tuned
    else:
        assert not path.exists()
    if r["promoted"]:
        assert r["tuned"]["test"]["score"] >= r["baseline"]["test"]["score"]
        assert r["tuned"]["val"]["score"] >= r["baseline"]["val"]["score"]


def test_onboard_without_apply_never_writes_components(ctx):
    before = dict(ctx.comps)
    r = onboard(ctx, apply=False, budget=60, components=["category_guide"], log=lambda *_: None)
    assert not ctx.cfg.components_path.exists()
    assert not r["promoted"] and "--apply" in r["reason"]
    assert ctx.comps == before
    assert (r["run_dir"] / "components.tuned.json").exists()


def test_onboard_backs_up_previous_components(ctx):
    ctx.cfg.components_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.cfg.components_path.write_text(json.dumps({"select_instructions": ctx.comps["select_instructions"]}),
                                       encoding="utf-8")
    r = onboard(ctx, apply=True, budget=80, log=lambda *_: None)
    if r["promoted"]:
        prev = json.loads((r["run_dir"] / "components.previous.json").read_text(encoding="utf-8"))
        assert set(prev) == {"select_instructions"}


def test_onboard_rejects_unknown_component(ctx):
    with pytest.raises(TuneError, match="not_a_thing"):
        onboard(ctx, components=["not_a_thing"], log=lambda *_: None)
