"""Policy rules and keyword matching, on tiny policies written to tmp_path.

Nothing here depends on the acme template: each test states the rules it needs.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from spendcoder.config import PolicyConfig
from spendcoder.policy import PolicyError, descriptor_key, load_policy, parse_amount_range
from spendcoder.stages import keyword_hits

RULE_HEADER = ["rule_id", "category", "department", "excluded_departments",
               "amount_range", "keywords", "gl_code", "gl_name"]


def rule(rid: str, category: str = "Meals", department: str = "", excluded: str = "",
         amount: str = "", keywords: str = "", gl: str = "6100", name: str = "Meals - general") -> list[str]:
    return [rid, category, department, excluded, amount, keywords, gl, name]


def write_policy(folder: Path, categories: list[str], rules: list[list[str]], *,
                 header: list[str] = RULE_HEADER, aliases: dict[str, list[str]] | None = None):
    folder.mkdir(parents=True, exist_ok=True)
    cats = folder / "categories.csv"
    with cats.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["category", "description"])
        w.writerows([c, f"{c} description"] for c in categories)
    rules_path = folder / "rules.csv"
    with rules_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rules)
    cfg = PolicyConfig(categories=cats, rules=rules_path, vendor_overrides=None, vendors=None,
                       keyword_aliases=dict(aliases or {}))
    return load_policy(cfg)


def ids(rules) -> set[str]:
    return {r.rule_id for r in rules}


# --------------------------------------------------------------------------- #
#  eligibility
# --------------------------------------------------------------------------- #
def test_category_match_is_case_insensitive(tmp_path):
    pol = write_policy(tmp_path, ["Meals", "Fuel"], [rule("R1", "Meals"), rule("R2", "Fuel", gl="6200")])
    assert ids(pol.eligible("MEALS", "", 10)) == {"R1"}
    assert ids(pol.eligible(" meals ", "", 10)) == {"R1"}
    assert pol.eligible("Hotel", "", 10) == []


def test_department_restriction(tmp_path):
    pol = write_policy(tmp_path, ["Meals"], [rule("R-SALES", department="Sales"), rule("R-ANY")])
    assert ids(pol.eligible("Meals", "Sales", 10)) == {"R-SALES", "R-ANY"}
    assert ids(pol.eligible("Meals", "sales", 10)) == {"R-SALES", "R-ANY"}  # ignoring case
    assert ids(pol.eligible("Meals", "Field Ops", 10)) == {"R-ANY"}
    # a rule that names a department never applies to a row with no department
    assert ids(pol.eligible("Meals", "", 10)) == {"R-ANY"}


def test_excluded_departments(tmp_path):
    pol = write_policy(tmp_path, ["Meals"], [rule("R-NOT-ADMIN", excluded="Admin, People"), rule("R-ANY")])
    assert ids(pol.eligible("Meals", "Admin", 10)) == {"R-ANY"}
    assert ids(pol.eligible("Meals", "people", 10)) == {"R-ANY"}  # ignoring case
    assert ids(pol.eligible("Meals", "Sales", 10)) == {"R-NOT-ADMIN", "R-ANY"}
    # a blank department is not in the exclusion list, so the rule still applies
    assert ids(pol.eligible("Meals", "", 10)) == {"R-NOT-ADMIN", "R-ANY"}


@pytest.mark.parametrize("amount, expected", [
    (0, {"R-SMALL", "R-ANY"}),
    (99.99, {"R-SMALL", "R-ANY"}),           # inclusive upper bound
    (100, {"R-EXACT", "R-ANY"}),
    (-50, {"R-SMALL", "R-ANY"}),             # refunds match like charges
    (-100, {"R-EXACT", "R-ANY"}),
    (250.5, {"R-ANY"}),
])
def test_amount_range_inclusive_and_refunds_by_absolute_value(tmp_path, amount, expected):
    pol = write_policy(tmp_path, ["Meals"], [
        rule("R-SMALL", amount="0-99.99"),
        rule("R-EXACT", amount="100"),
        rule("R-ANY"),
    ])
    assert ids(pol.eligible("Meals", "", amount)) == expected


def test_nan_never_satisfies_a_ranged_rule(tmp_path):
    pol = write_policy(tmp_path, ["Meals"], [rule("R-RANGE", amount="0-100000"), rule("R-ANY")])
    assert ids(pol.eligible("Meals", "", math.nan)) == {"R-ANY"}


@pytest.mark.parametrize("text, expected", [
    ("", (None, None)),
    ("100", (100.0, 100.0)),
    ("0-99.99", (0.0, 99.99)),
    ("$1,000 - $2,500", (1000.0, 2500.0)),
])
def test_amount_range_grammar(text, expected):
    assert parse_amount_range(text, "R") == expected


@pytest.mark.parametrize("bad", ["about 100", "100-", "-5", "abc", "10..20", "50-10"])
def test_unreadable_amount_range_raises_policy_error(tmp_path, bad):
    with pytest.raises(PolicyError, match="R-BAD"):
        write_policy(tmp_path, ["Meals"], [rule("R-BAD", amount=bad)])


def test_rule_row_with_blank_rule_id_is_not_silently_dropped(tmp_path):
    with pytest.raises(PolicyError):
        write_policy(tmp_path, ["Meals"], [rule("R1"), rule("", gl="6101", name="Orphan Meals")])


def test_duplicate_rule_id_raises(tmp_path):
    with pytest.raises(PolicyError, match="duplicate rule_id 'R1'"):
        write_policy(tmp_path, ["Meals"], [rule("R1"), rule("R1", gl="6101")])


def test_rule_with_unknown_category_raises(tmp_path):
    with pytest.raises(PolicyError, match="R9.*Hotel"):
        write_policy(tmp_path, ["Meals"], [rule("R9", "Hotel")])


def test_rule_category_must_match_ignoring_case(tmp_path):
    pol = write_policy(tmp_path, ["Meals"], [rule("R1", "meals")])
    assert pol.rules[0].category == "Meals"  # canonical spelling from categories.csv


def test_duplicate_category_raises(tmp_path):
    with pytest.raises(PolicyError, match="duplicate category"):
        write_policy(tmp_path, ["Meals", "meals"], [])


def test_rules_file_missing_a_column_raises(tmp_path):
    header = [c for c in RULE_HEADER if c != "gl_name"]
    with pytest.raises(PolicyError, match="missing columns"):
        write_policy(tmp_path, ["Meals"], [rule("R1")[:-1]], header=header)


# --------------------------------------------------------------------------- #
#  keyword matching (select stage)
# --------------------------------------------------------------------------- #
def test_keyword_match_is_literal_and_case_insensitive(tmp_path):
    pol = write_policy(tmp_path, ["Meals"], [rule("R1", keywords="team lunch, all hands")])
    r = pol.rules[0]
    assert keyword_hits(r, "Team Lunch with the crew", {}) == ["team lunch"]
    assert keyword_hits(r, "ALL HANDS meeting", {}) == ["all hands"]
    assert keyword_hits(r, "lunch with team", {}) == []   # no reordering, no fuzzy match
    assert keyword_hits(r, "", {}) == []


def test_keyword_alias_is_an_explicit_expansion(tmp_path):
    pol = write_policy(tmp_path, ["Meals"], [rule("R1", keywords="happy hour")],
                       aliases={"happy hour": ["HH"]})
    r = pol.rules[0]
    assert pol.keyword_aliases == {"happy hour": ["HH"]}
    assert keyword_hits(r, "HH with the sales team", pol.keyword_aliases) == ["happy hour"]
    assert keyword_hits(r, "hh", pol.keyword_aliases) == ["happy hour"]
    assert keyword_hits(r, "happy hour", pol.keyword_aliases) == ["happy hour"]
    # without the alias configured, the abbreviation does not match
    assert keyword_hits(r, "HH with the sales team", {}) == []
    # an alias is a literal form, not a concept: "drinks after work" is not "happy hour"
    assert keyword_hits(r, "drinks after work", pol.keyword_aliases) == []


def test_short_keywords_are_word_bounded(tmp_path):
    pol = write_policy(tmp_path, ["Meals"], [rule("R1", keywords="hh")])
    r = pol.rules[0]
    assert keyword_hits(r, "shhh", {}) == []          # inside a longer word
    assert keyword_hits(r, "hh", {}) == ["hh"]
    assert keyword_hits(r, "HH team dinner", {}) == ["hh"]


def test_short_alias_is_word_bounded(tmp_path):
    pol = write_policy(tmp_path, ["Meals"], [rule("R1", keywords="meeting")], aliases={"meeting": ["mtg"]})
    r = pol.rules[0]
    assert keyword_hits(r, "MTG planning", pol.keyword_aliases) == ["meeting"]
    assert keyword_hits(r, "dmtgx", pol.keyword_aliases) == []


def test_several_keywords_can_hit_one_rule(tmp_path):
    pol = write_policy(tmp_path, ["Meals"], [rule("R1", keywords="team lunch, HH")])
    assert keyword_hits(pol.rules[0], "team lunch and HH", {}) == ["team lunch", "HH"]


# --------------------------------------------------------------------------- #
#  descriptor key (normalize grouping and the train/test split)
# --------------------------------------------------------------------------- #
def test_descriptor_key_collapses_store_numbers_and_prefixes():
    assert descriptor_key("SHELL #4567") == descriptor_key("SHELL 0021") == "SHELL"
    assert descriptor_key("SQ *HARBOR GRILL 0342") == descriptor_key("HARBOR GRILL #12") == "HARBOR GRILL"
    assert descriptor_key("SHELL #4567") != descriptor_key("CHEVRON 0045512")
