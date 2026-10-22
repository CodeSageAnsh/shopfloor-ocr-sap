"""Unit tests for the pure validation layer (no OCR, DB or SAP needed)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from material_rows import match_material, normalize_materials
from shift_check import parse_cell, validate_shift_hours


# ── material label matching ────────────────────────────────────────────────
def test_handwritten_label_variants_map_to_one_key():
    assert match_material("HESSAIN") == "hessian"
    assert match_material("Sacking Wept") == "sacking_weft"
    assert match_material("12 LBS WARP") == "12lb_warp"
    assert match_material("Sale Yam") == "sale_yarn"


def test_hessian_warp_is_not_sacking_warp():
    assert match_material("Hessian Warp") == "hessian"


def test_longer_numbers_do_not_match_weight_rows():
    assert match_material("125 warp") is None


def test_unknown_label_warns_instead_of_guessing():
    parsed = {"rows": [{"label": "mystery row", "BC": {"M1": "2"}}]}
    _, warnings = normalize_materials(parsed)
    assert warnings, "an unrecognised row must produce a warning"


# ── cell parsing ───────────────────────────────────────────────────────────
def test_fractional_and_multi_machine_cells():
    assert parse_cell("15(1/2)", "BC") == [("030BC015", 4.0)]
    assert [c for c, _ in parse_cell("2, 28(1/2)", "IC")] == ["030IC002", "030IC028"]


def test_shed_prefix_keeps_machines_distinct():
    old, = parse_cell("o2", "BC")
    new, = parse_cell("n2", "BC")
    assert old[0] == "031BC002" and new[0] == "032BC002"


# ── shift-hour arithmetic ─────────────────────────────────────────────────
def _sheet(**bc):
    return {"materials": {"hessian": {"BC": bc}}}


def test_legitimate_split_across_two_materials_is_clean():
    sheet = {"materials": {"hessian":      {"BC": {"M1": "2(1/2)"}},
                           "sacking_warp": {"BC": {"M1": "2(1/2)"}}}}
    assert validate_shift_hours(sheet) == []


def test_same_machine_twice_in_one_section_is_flagged():
    assert validate_shift_hours(_sheet(M1="2(1/2)", M2="2(1/2)"))


def test_machine_over_one_shift_is_flagged():
    assert validate_shift_hours(_sheet(M1="7", M2="7"))
