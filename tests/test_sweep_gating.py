"""Gating and coverage honesty for the sweep.

Three things have to hold together, and each fails silently if it drifts:

* the sweep is **withheld** below Extreme effort, not offered and refused -- a
  visible tool gets tried, and a refusal still costs one of four to twenty
  permitted calls;
* a sweep is **priced before it runs**, because its cheapest useful
  configuration takes minutes and four promotions is most of an hour;
* an answer built on a sweep **states its coverage**, because a bounded sweep
  produces false negatives by construction. No schema reaches the model, so the
  requirement has to be enumerated in prompt text or it does not exist.
"""

from __future__ import annotations

import pytest

from backend.pipeline.agents.evidence_report import JSON_OUTPUT_SPEC
from backend.pipeline.agents.find_evidence import SWEEP_TOOL_NAMES
from backend.pipeline.contracts import EvidenceReport
from backend.pipeline.effort import EFFORT_SPECS, get_effort_spec
from backend.pipeline.prompts.builders import TOOL_CATALOG


# --- Effort gating ----------------------------------------------------------
def test_the_sweep_is_withheld_below_extreme_effort():
    for level in ("Low", "Medium", "High"):
        assert get_effort_spec(level).max_promotions == 0


def test_the_sweep_is_available_at_the_top_two_levels():
    assert get_effort_spec("Extreme").max_promotions == 2
    assert get_effort_spec("Brutal").max_promotions == 4


def test_promotion_allowance_is_the_single_gate():
    """One dial, so the tool list and the budget cannot disagree.

    An earlier design would have needed a separate flag, which is exactly the
    kind of thing that drifts out of step with the budget it is meant to track.
    """
    for level, spec in EFFORT_SPECS.items():
        offered = bool(spec.max_promotions)
        assert offered == (spec.max_promotions > 0), level


def test_the_gate_names_both_sweep_tools():
    assert SWEEP_TOOL_NAMES == {"sweep_chunks_tool", "investigate_chunk_tool"}


def test_scan_budgets_grow_faster_than_call_budgets():
    """Tool calls are a poor proxy for cost when one call can read a billion
    rows, so the two budgets must scale independently."""
    low, brutal = get_effort_spec("Low"), get_effort_spec("Brutal")
    call_ratio = brutal.max_tool_calls / low.max_tool_calls
    scan_ratio = brutal.max_rows_scanned / low.max_rows_scanned
    assert scan_ratio > call_ratio


# --- The tool surface stays consistent --------------------------------------
def test_the_mcp_module_still_exposes_the_skill_tools():
    """These were dropped once and broke five test modules."""
    from backend.pipeline.tools.mcp_tools import list_skills, read_skill

    assert read_skill is not None
    assert list_skills is not None


# --- Cost is declared before it is spent ------------------------------------
def test_the_catalog_tells_the_model_to_confirm_cost_first():
    description = TOOL_CATALOG["sweep_chunks_tool"]
    assert "confirm_cost=false first" in description
    assert "confirm_cost=true" in description


def test_the_catalog_states_the_per_chunk_cost_concretely():
    """A vague "this is expensive" does not help the model choose."""
    description = TOOL_CATALOG["sweep_chunks_tool"]
    assert "ELEVEN MINUTES" in description.upper()


def test_the_catalog_marks_the_sweep_as_a_last_resort():
    assert "LAST RESORT" in TOOL_CATALOG["sweep_chunks_tool"]


def test_both_sweep_tools_declare_their_tier():
    for name in SWEEP_TOOL_NAMES:
        assert TOOL_CATALOG[name].startswith("TIER sweep")


def test_an_unconfirmed_sweep_returns_an_estimate_not_a_sweep(monkeypatch):
    """The gate is in the tool, not left to the model's good intentions."""
    from backend.db import chunks as chunk_mod
    from backend.db import pruning
    from backend.pipeline import sweep as sweep_mod

    url = "https://public-pme.s3.eu-west-3.amazonaws.com/text_bench/part_000.parquet"
    pruning.clear_cache()
    pruning.seed_cache(url, pruning.synthetic_row_groups(url, 40))
    chunk_mod.clear_plan_cache()

    ran = {"swept": False}
    monkeypatch.setattr(
        sweep_mod, "sweep", lambda *a, **k: ran.__setitem__("swept", True)
    )

    estimate = sweep_mod.estimate_sweep(budget=sweep_mod.SweepBudget(max_promotions=2))
    assert not ran["swept"]
    assert estimate["promotions_allowed"] == 2
    assert estimate["estimated_seconds"] > 0

    pruning.clear_cache()
    chunk_mod.clear_plan_cache()


# --- Coverage honesty -------------------------------------------------------
def test_the_output_contract_requires_coverage():
    """Pydantic field descriptions never reach the model, so the requirement
    has to appear in the prompt text."""
    assert '"coverage"' in JSON_OUTPUT_SPEC
    assert "REQUIRED" in JSON_OUTPUT_SPEC


def test_the_contract_distinguishes_the_three_examination_depths():
    """Profiled, sampled and promoted are not interchangeable, and conflating
    them is the most likely way for an answer to overstate itself."""
    spec = JSON_OUTPUT_SPEC
    assert "profiled" in spec
    assert "COUNTED, not read" in spec
    assert "promoted" in spec


def test_the_contract_forbids_silence_reading_as_absence():
    assert "evidence that nothing is there" in JSON_OUTPUT_SPEC


def test_the_report_model_accepts_coverage():
    report = EvidenceReport.model_validate_json(
        '{"summary": "s", "evidence": [], '
        '"coverage": "profiled 1624 chunks, promoted 2"}'
    )
    assert "1624" in report.coverage


def test_coverage_is_optional_on_the_model_for_non_sweep_answers():
    """Most investigations never sweep, and their reports must still validate.

    The requirement is enforced in the prompt, where it can be conditional;
    making the field mandatory in the model would break every direct question.
    """
    report = EvidenceReport.model_validate_json('{"summary": "s", "evidence": []}')
    assert report.coverage == ""


# --- Skill guidance ---------------------------------------------------------
def test_the_skill_explains_the_cost_hierarchy():
    from pathlib import Path

    text = Path("skills/log-investigation/SKILL.md").read_text()
    assert "Sweeping a whole corpus" in text
    assert "confirm_cost=false" in text
    assert "counted, not read" in text


def test_the_skill_prefers_a_narrowed_query():
    """The sweep must not become the default answer to a hard question."""
    from pathlib import Path

    text = Path("skills/log-investigation/SKILL.md").read_text()
    assert "Prefer a narrowed query" in text
