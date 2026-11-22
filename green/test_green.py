"""Tests for ORCA-Green. Run: python -m pytest test_green.py -q"""
import app as g


def test_demo_run_improves_compliance():
    r = g.run_loop(g.demo_building(), "green_mark_sg")
    assert r["compliance_after"]["pass"] > r["compliance_before"]["pass"]
    assert r["after"]["simulation"]["eui_kwh_m2_yr"] < r["observe"]["simulation"]["eui_kwh_m2_yr"]
    assert r["verified_fixes"], "expected at least one verified fix"


def test_every_returned_fix_is_verified_no_regression():
    r = g.run_loop(g.demo_building(), "green_mark_sg")
    base_carbon = r["observe"]["carbon"]["total_kgco2e"]
    for v in r["verdicts"]:
        if v["verified"]:
            assert not v["regressed_rules"], v
            # Two tracks: fixes >=1 failing rule, OR carbon track (>=1% cut)
            carbon_track = v["carbon_delta_kgco2e"] <= -0.01 * base_carbon
            assert v["fixed_rules"] or carbon_track, v
            assert v["eui_delta"] <= 0, v
    # The applied bundle itself must regress no originally-passing rule.
    before = {c["key"]: c["pass"] for c in r["observe"]["checks"]}
    after = {c["key"]: c["pass"] for c in r["after"]["checks"]}
    for key, was_pass in before.items():
        if was_pass:
            assert after[key], f"bundle regressed {key}"
    # verified_fixes list must match verdicts exactly
    assert set(r["verified_fixes"]) == {v["id"] for v in r["verdicts"] if v["verified"]}


def test_low_carbon_swap_reduces_carbon():
    base = g.demo_building()
    low = g.apply_changes(base, {"concrete_choice": "concrete_ggbs50",
                                 "steel_choice": "steel_eaf_recycled"})
    assert g.carbon(low)["total_kgco2e"] < g.carbon(base)["total_kgco2e"]


def test_all_four_codes_load_and_run():
    assert set(g.CODES) == {"green_mark_sg", "estidama_uae", "mostadam_ksa", "greenship_id"}
    for code_id in g.CODES:
        r = g.run_loop(g.demo_building(), code_id)
        assert r["compliance_after"]["total"] == len(g.CODES[code_id]["rules"])
        assert r["compliance_after"]["pass"] >= r["compliance_before"]["pass"]


def test_parse_fallback_on_non_ifc_file(tmp_path):
    junk = tmp_path / "not_an_ifc.txt"
    junk.write_text("this is not an IFC file")
    result = g.parse_ifc(str(junk))
    assert result["parsed"] is False
    assert "note" in result
    # Caller fallback yields the demo building unchanged
    b = g.building_from_ifc(result)
    assert b.gfa_m2 == g.demo_building().gfa_m2
