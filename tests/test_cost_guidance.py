"""Cost guidance given to the agent, and the per-effort scan budget.

Offline. These assert that the guidance the model receives actually describes
the bounds the code enforces, so the two cannot drift apart silently.
"""

from __future__ import annotations

import pytest

from backend.pipeline import tools
from backend.pipeline.effort import EFFORT_SPECS, get_effort_spec
from backend.pipeline.prompts.builders import (
    COST_DISCIPLINE,
    QUERYABLE_TARGETS,
    TOOL_CATALOG,
    dataset_catalog,
)

EFFORT_ORDER = ["Low", "Medium", "High", "Extreme", "Brutal"]


# --- The catalog contract ---------------------------------------------------
def test_tool_catalog_matches_the_tools_that_exist():
    """Asserted elsewhere too; repeated here because this file edits it."""
    assert set(TOOL_CATALOG) == set(tools.__all__)


def test_every_tool_declares_which_tier_it_reads():
    """Cost differs by orders of magnitude between tiers, so it must be stated."""
    for name, description in TOOL_CATALOG.items():
        assert "TIER" in description, name


def test_the_catalog_names_the_dataset_parameter_not_a_table():
    blob = " ".join(TOOL_CATALOG.values())
    assert "dataset" in blob
    assert "table_name" not in blob
    assert "table" not in blob.replace("TIER", "")


def test_search_is_described_as_index_only():
    """The agent must know text search cannot run on the raw remote corpus."""
    description = TOOL_CATALOG["search_logs_tool"]
    assert "index ONLY" in description
    assert "refused" in description


def test_the_cheapest_aggregate_is_identified_as_such():
    assert "CHEAPEST" in TOOL_CATALOG["count_by_category_tool"]


# --- Cost discipline --------------------------------------------------------
@pytest.mark.parametrize(
    "idea",
    [
        "time window",       # windows are the cost control
        "cheap to expensive",
        "Narrow before searching",
        "refusal",           # refusals are usable, not failures
        "APPROXIMATE",       # approximation must be disclosed
        "query_stats",       # cost is reported, not estimated by the model
    ],
)
def test_cost_discipline_covers_each_rule(idea):
    assert idea in COST_DISCIPLINE


def test_cost_discipline_tells_the_model_the_corpus_scale():
    # A concrete number beats "it is big".
    assert "560,000 rows" in COST_DISCIPLINE


# --- Dataset catalog --------------------------------------------------------
def test_scope_targets_include_the_dataset_dimension():
    joined = " ".join(QUERYABLE_TARGETS)
    assert "dataset" in joined
    assert "time window" in joined


def test_dataset_catalog_describes_each_corpus_without_identifiers():
    catalog = dataset_catalog()
    assert {"openstack", "otel_s3", "otel_slice"} <= set(catalog)

    for entry in catalog.values():
        assert entry["description"]
        assert "exact_counts" in entry
        assert "severity_values" in entry

    blob = repr(catalog)
    for identifier in ("log_timestamp", "log_idx", "pid_text", "SeverityText"):
        assert identifier not in blob, identifier


def test_the_catalog_distinguishes_exact_from_approximate_corpora():
    catalog = dataset_catalog()
    assert catalog["openstack"]["exact_counts"] is True
    # The remote tier cannot promise exact counts, and says so.
    assert catalog["otel_s3"]["exact_counts"] is False


def test_each_corpus_advertises_its_own_severity_casing():
    catalog = dataset_catalog()
    assert "ERROR" in catalog["openstack"]["severity_values"]
    assert "error" in catalog["otel_s3"]["severity_values"]


# --- Per-effort budgets -----------------------------------------------------
def test_every_effort_level_has_a_scan_budget():
    for name in EFFORT_ORDER:
        assert get_effort_spec(name).max_rows_scanned > 0


def test_scan_budget_increases_with_effort():
    budgets = [get_effort_spec(name).max_rows_scanned for name in EFFORT_ORDER]
    assert budgets == sorted(budgets)


def test_tool_calls_alone_do_not_bound_cost():
    """A scan budget exists because call count is a poor proxy at this scale.

    One careless wide aggregate can read more than a hundred careful narrow
    ones, so the two budgets are independent.
    """
    low, brutal = get_effort_spec("Low"), get_effort_spec("Brutal")
    call_ratio = brutal.max_tool_calls / low.max_tool_calls
    scan_ratio = brutal.max_rows_scanned / low.max_rows_scanned
    assert scan_ratio > call_ratio


def test_chunk_promotion_is_reserved_for_the_top_effort_levels():
    """Promotion is the only operation that builds an index, and builds are the
    fragile operation on this host."""
    for name in ("Low", "Medium", "High"):
        assert get_effort_spec(name).max_promotions == 0
    for name in ("Extreme", "Brutal"):
        assert get_effort_spec(name).max_promotions > 0


def test_unknown_effort_falls_back_to_medium():
    assert get_effort_spec("Nonsense") == EFFORT_SPECS["Medium"]
    assert get_effort_spec(None) == EFFORT_SPECS["Medium"]


# --- The skill --------------------------------------------------------------
def test_the_skill_teaches_the_cheap_before_expensive_workflow():
    from pathlib import Path

    text = Path("skills/log-investigation/SKILL.md").read_text(encoding="utf-8")
    assert "Working at scale" in text
    assert "Start with a count, not a search" in text
    assert "Refusals are directions" in text
    assert "Approximate results must be labelled" in text
    assert "Report what you actually covered" in text
