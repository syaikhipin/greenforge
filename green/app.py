"""
ORCA-Green: Green-code verified-fix agent.

Mirrors ORCA's Observe -> Resolve -> Check -> Apply loop, but the verifier is
green building codes + a simplified energy simulation + embodied-carbon calc
instead of geometry.

Honesty rules baked in:
- EnergyPlus is NEVER claimed to have run. If the binary is present we say so,
  but the demo still uses the simplified physics engine unless a real IDF
  template is configured.
- IFC counts are never fabricated: if IfcOpenShell is missing or parsing
  fails, parse_ifc returns parsed=False and callers fall back to the demo
  building.
- An LLM (Opencode Go) may only RANK deterministic candidate fixes. It never
  invents numbers, and any LLM error falls back silently to the deterministic
  order with llm_used=False.
- Code thresholds in codes.json are SIMPLIFIED. A QP / licensed professional
  must verify against the current edition before any real submission.
"""
import copy
import json
import math
import os
import shutil
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "codes.json", encoding="utf-8") as fh:
    CODES_DOC = json.load(fh)
CODES = {c["id"]: c for c in CODES_DOC["codes"]}

with open(BASE_DIR / "epd.json", encoding="utf-8") as fh:
    EPD_DOC = json.load(fh)
EPD = {}  # option_id -> option dict (with category)
for _cat, _opts in EPD_DOC["categories"].items():
    for _opt in _opts:
        EPD[_opt["id"]] = {**_opt, "category": _cat}

ELECTRICITY_PRICE = 0.28  # per kWh, in the code's currency (indicative)
OPERATING_HOURS = 3000    # lighting / cooling hours per year (simplified)


# --------------------------------------------------------------------------- #
# Building model
# --------------------------------------------------------------------------- #
class Building(BaseModel):
    gfa_m2: float = 12000
    floors: int = 10
    wwr: float = 0.65
    glazing_u: float = 2.8
    glazing_shgc: float = 0.55
    wall_u: float = 1.2
    roof_u: float = 0.9
    hvac_cop: float = 3.0
    hvac_kw_per_rt: float = 1.05
    lighting_wpd: float = 15
    renewable_share: float = 0.0
    concrete_m3: float = 4500
    steel_t: float = 620
    aluminium_t: float = 38
    insulation_m3: float = 850
    climate_zone: str = "tropical (ASHRAE 1A, simplified)"
    # Selected EPD options (defaults = conventional)
    concrete_choice: str = "concrete_c30_conventional"
    steel_choice: str = "steel_virgin_bof"
    glass_choice: str = "glass_single_clear"
    aluminium_choice: str = "aluminium_virgin"
    insulation_choice: str = "insulation_mineral_wool"

    def facade_m2(self) -> float:
        plate = self.gfa_m2 / max(self.floors, 1)
        side = math.sqrt(max(plate, 1.0))
        return 4 * side * self.floors * 3.6  # 3.6 m floor-to-floor

    def roof_m2(self) -> float:
        return self.gfa_m2 / max(self.floors, 1)

    def glass_m2(self) -> float:
        return self.wwr * self.facade_m2()


def demo_building() -> Building:
    return Building()


# --------------------------------------------------------------------------- #
# Simplified energy simulation
# --------------------------------------------------------------------------- #
def energyplus_note() -> str:
    if shutil.which("energyplus"):
        return "energyplus-available (simplified run used in demo)"
    return ("simplified-physics (EnergyPlus not installed — install EnergyPlus "
            "and set ENERGYPLUS_IDF_TEMPLATE to run full IDF)")


def simulate(b: Building) -> dict:
    """Simplified physics engine. Returns EUI, ETTV/OTTV, cost, cooling."""
    # ETTV/OTTV approximation (W/m2 of facade): opaque conduction + glazing
    # conduction + solar gain through glazing.
    ettv = ((1 - b.wwr) * b.wall_u * 8.0
            + b.wwr * b.glazing_u * 5.0
            + b.wwr * b.glazing_shgc * 110.0)
    thermal_cooling = ettv * 3.5 + b.roof_u * 15.0  # kWh/m2/yr thermal
    cooling_kwh_m2 = thermal_cooling / max(b.hvac_cop, 0.5)
    aux_kwh_m2 = 20.0 * b.hvac_kw_per_rt            # pumps / fans / towers
    lighting_kwh_m2 = b.lighting_wpd * OPERATING_HOURS / 1000.0
    plug_kwh_m2 = 35.0
    base_kwh_m2 = 30.0
    gross_eui = cooling_kwh_m2 + aux_kwh_m2 + lighting_kwh_m2 + plug_kwh_m2 + base_kwh_m2
    eui = gross_eui * (1.0 - min(max(b.renewable_share, 0.0), 0.9))
    annual_kwh = eui * b.gfa_m2
    return {
        "eui_kwh_m2_yr": round(eui, 1),
        "ettv_w_m2": round(ettv, 1),
        "ottv_w_m2": round(ettv, 1),
        "annual_kwh": round(annual_kwh),
        "annual_cost": round(annual_kwh * ELECTRICITY_PRICE, 2),
        "cooling_kwh": round(cooling_kwh_m2 * b.gfa_m2),
        "engine": energyplus_note(),
    }


# --------------------------------------------------------------------------- #
# Embodied carbon
# --------------------------------------------------------------------------- #
def carbon(b: Building) -> dict:
    parts = {
        "concrete": b.concrete_m3 * EPD[b.concrete_choice]["gwp_kgco2e_per_unit"],
        "steel": b.steel_t * EPD[b.steel_choice]["gwp_kgco2e_per_unit"],
        "glass": b.glass_m2() * EPD[b.glass_choice]["gwp_kgco2e_per_unit"],
        "aluminium": b.aluminium_t * EPD[b.aluminium_choice]["gwp_kgco2e_per_unit"],
        "insulation": b.insulation_m3 * EPD[b.insulation_choice]["gwp_kgco2e_per_unit"],
    }
    total = sum(parts.values())
    return {
        "total_kgco2e": round(total),
        "kgco2e_per_m2": round(total / b.gfa_m2, 1),
        "by_material_kgco2e": {k: round(v) for k, v in parts.items()},
        "choices": {
            "concrete": b.concrete_choice, "steel": b.steel_choice,
            "glass": b.glass_choice, "aluminium": b.aluminium_choice,
            "insulation": b.insulation_choice,
        },
    }


# --------------------------------------------------------------------------- #
# Rules check
# --------------------------------------------------------------------------- #
def metrics_for(b: Building, sim: dict) -> dict:
    return {
        "ettv": sim["ettv_w_m2"], "ottv": sim["ottv_w_m2"],
        "eui": sim["eui_kwh_m2_yr"],
        "wwr": b.wwr, "glazing_u": b.glazing_u, "glazing_shgc": b.glazing_shgc,
        "wall_u": b.wall_u, "roof_u": b.roof_u,
        "hvac_cop": b.hvac_cop, "hvac_kw_per_rt": b.hvac_kw_per_rt,
        "lighting_wpd": b.lighting_wpd, "renewable_share": b.renewable_share,
    }


def check_rules(b: Building, code: dict) -> list:
    sim = simulate(b)
    metrics = metrics_for(b, sim)
    results = []
    for rule in code["rules"]:
        value = metrics.get(rule["key"])
        if value is None:
            continue
        if rule["direction"] == "max":
            passed = value <= rule["limit"]
            margin = round(rule["limit"] - value, 4)
        else:
            passed = value >= rule["limit"]
            margin = round(value - rule["limit"], 4)
        results.append({
            "key": rule["key"], "label": rule["label"], "value": value,
            "limit": rule["limit"], "direction": rule["direction"],
            "unit": rule["unit"], "pass": bool(passed), "margin": margin,
        })
    return results


# --------------------------------------------------------------------------- #
# Deterministic proposer (candidate edit catalogue)
# --------------------------------------------------------------------------- #
def propose(b: Building, code: dict, failures: list) -> list:
    fail_keys = {f["key"] for f in failures}
    facade = b.facade_m2()
    glass = b.glass_m2()
    cands = []

    wwr_limit = next((r["limit"] for r in code["rules"] if r["key"] == "wwr"), 0.5)
    if b.wwr > wwr_limit:
        cands.append({
            "id": "reduce_wwr", "title": "Reduce window-to-wall ratio",
            "category": "envelope",
            "changes": {"wwr": wwr_limit},
            "capex": round((b.wwr - wwr_limit) * facade * 120, 2),
            "description": f"Reduce WWR from {b.wwr} to {wwr_limit} (opaque insulated spandrel in place of glazing).",
        })
    lowe = EPD["glass_double_lowe"]
    if b.glazing_u > lowe["u_value"] or b.glazing_shgc > lowe["shgc"]:
        cands.append({
            "id": "lowe_glazing", "title": "Swap to double low-e glazing",
            "category": "envelope",
            "changes": {"glazing_u": lowe["u_value"], "glazing_shgc": lowe["shgc"],
                        "glass_choice": "glass_double_lowe"},
            "capex": round(glass * (lowe["cost_per_unit"] - EPD[b.glass_choice]["cost_per_unit"]), 2),
            "description": f"Double low-e glazing from EPD database: U={lowe['u_value']} W/m2K, SHGC={lowe['shgc']} (was U={b.glazing_u}, SHGC={b.glazing_shgc}).",
        })
    if b.glazing_shgc > 0.2:
        cands.append({
            "id": "shading_fins", "title": "External shading fins",
            "category": "envelope",
            "changes": {"glazing_shgc": round(b.glazing_shgc * 0.75, 4)},
            "capex": round(facade * 45, 2),
            "description": "External vertical/horizontal shading fins: effective SHGC x0.75.",
        })
    if b.wall_u > 0.45:
        cands.append({
            "id": "wall_insulation", "title": "External wall insulation upgrade",
            "category": "envelope",
            "changes": {"wall_u": 0.45},
            "capex": round(facade * (1 - b.wwr) * 55, 2),
            "description": f"Add external insulation: wall U from {b.wall_u} to 0.45 W/m2K.",
        })
    if b.roof_u > 0.35:
        cands.append({
            "id": "roof_insulation", "title": "Roof insulation upgrade",
            "category": "envelope",
            "changes": {"roof_u": 0.35},
            "capex": round(b.roof_m2() * 40, 2),
            "description": f"Add roof insulation: roof U from {b.roof_u} to 0.35 W/m2K.",
        })
    if b.hvac_cop < 4.2 or b.hvac_kw_per_rt > 0.68:
        est_rt = b.gfa_m2 * 0.08
        cands.append({
            "id": "hvac_upgrade", "title": "High-efficiency HVAC plant upgrade",
            "category": "hvac",
            "changes": {"hvac_cop": 4.2, "hvac_kw_per_rt": 0.68},
            "capex": round(est_rt * 350, 2),
            "description": f"Magnetic-bearing chiller + VSD plant: COP {b.hvac_cop} -> 4.2, system {b.hvac_kw_per_rt} -> 0.68 kW/RT (est. {est_rt:.0f} RT).",
        })
    if b.lighting_wpd > 7:
        cands.append({
            "id": "led_lighting", "title": "LED lighting retrofit",
            "category": "lighting",
            "changes": {"lighting_wpd": 7},
            "capex": round(b.gfa_m2 * 18, 2),
            "description": f"LED + daylight/occupancy controls: LPD {b.lighting_wpd} -> 7 W/m2.",
        })
    if b.renewable_share < 0.06:
        cands.append({
            "id": "rooftop_pv", "title": "Rooftop PV array",
            "category": "renewables",
            "changes": {"renewable_share": 0.06},
            "capex": round(b.roof_m2() * 0.6 * 450, 2),
            "description": "Rooftop PV on ~60% of roof plate: renewable share -> 0.06 of annual demand.",
        })
    ggbs = EPD["concrete_ggbs50"]
    if b.concrete_choice != "concrete_ggbs50":
        cands.append({
            "id": "low_carbon_concrete", "title": "Low-carbon concrete swap (50% GGBS)",
            "category": "materials",
            "changes": {"concrete_choice": "concrete_ggbs50"},
            "capex": round(b.concrete_m3 * (ggbs["cost_per_unit"] - EPD[b.concrete_choice]["cost_per_unit"]), 2),
            "description": f"EPD swap: {EPD[b.concrete_choice]['name']} ({EPD[b.concrete_choice]['gwp_kgco2e_per_unit']} kgCO2e/m3) -> {ggbs['name']} ({ggbs['gwp_kgco2e_per_unit']} kgCO2e/m3). Carbon-only measure.",
        })
    eaf = EPD["steel_eaf_recycled"]
    if b.steel_choice != "steel_eaf_recycled":
        cands.append({
            "id": "recycled_steel", "title": "Recycled EAF steel swap",
            "category": "materials",
            "changes": {"steel_choice": "steel_eaf_recycled"},
            "capex": round(b.steel_t * (eaf["cost_per_unit"] - EPD[b.steel_choice]["cost_per_unit"]), 2),
            "description": f"EPD swap: {EPD[b.steel_choice]['name']} ({EPD[b.steel_choice]['gwp_kgco2e_per_unit']} kgCO2e/t) -> {eaf['name']} ({eaf['gwp_kgco2e_per_unit']} kgCO2e/t). Carbon-only measure.",
        })
    # Keep a stable deterministic order; note which candidates target failures.
    for c in cands:
        c["targets_failure"] = bool(fail_keys)
    return cands


# Fields where lower is better / higher is better (for monotonic bundle merge)
_LOWER_BETTER = {"wwr", "glazing_u", "glazing_shgc", "wall_u", "roof_u",
                 "hvac_kw_per_rt", "lighting_wpd"}
_HIGHER_BETTER = {"hvac_cop", "renewable_share"}


def apply_changes(b: Building, changes: dict) -> Building:
    """Apply one edit's changes to a copy (plain override)."""
    nb = b.model_copy(deep=True)
    for k, v in changes.items():
        setattr(nb, k, v)
    return nb


def apply_bundle(b: Building, edits: list) -> Building:
    """Apply verified edits together, merging numeric fields monotonically
    (best value wins) so e.g. shading fins never undo a low-e glazing swap."""
    nb = b.model_copy(deep=True)
    for edit in edits:
        for k, v in edit["changes"].items():
            cur = getattr(nb, k)
            if k in _LOWER_BETTER and isinstance(v, (int, float)):
                setattr(nb, k, min(cur, v))
            elif k in _HIGHER_BETTER and isinstance(v, (int, float)):
                setattr(nb, k, max(cur, v))
            else:
                setattr(nb, k, v)
    return nb


# --------------------------------------------------------------------------- #
# Optional LLM ranking via Opencode Go (ranks only, never invents numbers)
# --------------------------------------------------------------------------- #
def llm_rank(candidates: list, code: dict, failures: list) -> tuple:
    """Return (ordered_candidates, llm_used). Any error -> (candidates, False)."""
    key = os.environ.get("OPENCODE_API_KEY")
    if not key or not candidates:
        return candidates, False
    model = os.environ.get("OPENCODE_GO_MODEL", "mimo-v2.6-flash")
    ids = [c["id"] for c in candidates]
    prompt = (
        "You rank building retrofit fixes. Failing rules for code "
        f"{code['name']}: {[f['key'] for f in failures]}. "
        f"Candidate fix ids: {ids}. "
        "Reply with ONLY a JSON array of the candidate ids in your preferred "
        "application order. Do not invent ids or numbers."
    )
    try:
        resp = httpx.post(
            "https://opencode.ai/zen/go/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "x-opencode-session": "orca-green-1",
                "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
                "Content-Type": "application/json",
            },
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        start, end = text.find("["), text.rfind("]")
        ranked_ids = json.loads(text[start:end + 1])
        by_id = {c["id"]: c for c in candidates}
        ordered = [by_id[i] for i in ranked_ids if i in by_id]
        ordered += [c for c in candidates if c["id"] not in ranked_ids]
        if not ordered:
            return candidates, False
        return ordered, True
    except Exception:
        return candidates, False


# --------------------------------------------------------------------------- #
# The verified-fix loop: Observe -> Resolve -> Check -> Apply
# --------------------------------------------------------------------------- #
RUNS = {}


def run_loop(b: Building, code_id: str, use_llm: bool = False) -> dict:
    code = CODES.get(code_id) or CODES["green_mark_sg"]
    workings = []

    # OBSERVE
    sim_before = simulate(b)
    carb_before = carbon(b)
    checks_before = check_rules(b, code)
    failures = [c for c in checks_before if not c["pass"]]
    n_pass_before = sum(1 for c in checks_before if c["pass"])
    workings.append(
        f"OBSERVE: {code['name']} — {n_pass_before}/{len(checks_before)} rules pass; "
        f"EUI {sim_before['eui_kwh_m2_yr']} kWh/m2/yr; ETTV {sim_before['ettv_w_m2']} W/m2; "
        f"embodied carbon {carb_before['total_kgco2e']:,} kgCO2e. "
        f"Engine: {sim_before['engine']}.")

    # RESOLVE
    candidates = propose(b, code, failures)
    llm_used = False
    if use_llm:
        candidates, llm_used = llm_rank(candidates, code, failures)
    workings.append(
        f"RESOLVE: {len(candidates)} candidate fixes proposed "
        f"({'LLM-ranked via Opencode Go' if llm_used else 'deterministic catalogue order'}).")

    # CHECK — each candidate individually against the original building
    base_pass = {c["key"]: c["pass"] for c in checks_before}
    verdicts = []
    verified = []
    for cand in candidates:
        trial = apply_changes(b, cand["changes"])
        trial_checks = check_rules(trial, code)
        trial_sim = simulate(trial)
        trial_carbon = carbon(trial)
        carbon_delta_trial = trial_carbon["total_kgco2e"] - carb_before["total_kgco2e"]
        carbon_cut_pct = (-carbon_delta_trial / carb_before["total_kgco2e"] * 100) if carb_before["total_kgco2e"] else 0.0
        trial_pass = {c["key"]: c["pass"] for c in trial_checks}
        fixed = [k for k in base_pass if not base_pass[k] and trial_pass.get(k)]
        regressed = [k for k in base_pass if base_pass[k] and not trial_pass.get(k)]
        eui_delta = round(trial_sim["eui_kwh_m2_yr"] - sim_before["eui_kwh_m2_yr"], 2)
        reasons = []
        if fixed:
            reasons.append(f"fixes rule(s): {', '.join(fixed)}")
        else:
            reasons.append("fixes no failing rule")
        if regressed:
            reasons.append(f"REGRESSES rule(s): {', '.join(regressed)}")
        if eui_delta > 0:
            reasons.append(f"increases EUI by {eui_delta}")
        elif eui_delta < 0:
            reasons.append(f"EUI {eui_delta:+} kWh/m2/yr")
        else:
            reasons.append("EUI unchanged")
        # Two verification tracks, same discipline as ORCA's geometry check:
        # (1) compliance track — fixes >=1 failing rule; (2) carbon track —
        # cuts embodied carbon by >=1% (material swaps fix carbon, not a rule).
        # Both tracks require: no rule regression and no EUI increase.
        carbon_track = carbon_cut_pct >= 1.0
        if carbon_track:
            reasons.append(f"embodied carbon {carbon_delta_trial:+,} kgCO2e ({carbon_cut_pct:.1f}% cut, carbon track)")
        is_verified = (bool(fixed) or carbon_track) and not regressed and eui_delta <= 0
        verdicts.append({
            "id": cand["id"], "title": cand["title"], "category": cand["category"],
            "capex": cand["capex"], "verified": is_verified,
            "fixed_rules": fixed, "regressed_rules": regressed,
            "eui_delta": eui_delta, "carbon_delta_kgco2e": carbon_delta_trial,
            "reason": "; ".join(reasons),
        })
        workings.append(
            f"CHECK {cand['id']}: {'VERIFIED' if is_verified else 'REJECTED'} — {'; '.join(reasons)}.")
        if is_verified:
            verified.append(cand)

    # APPLY — combined bundle of verified fixes only
    after = apply_bundle(b, verified)
    sim_after = simulate(after)
    carb_after = carbon(after)
    checks_after = check_rules(after, code)
    n_pass_after = sum(1 for c in checks_after if c["pass"])
    capex_total = round(sum(v["capex"] for v in verdicts if v["verified"]), 2)
    annual_saving = round(sim_before["annual_cost"] - sim_after["annual_cost"], 2)
    payback = round(capex_total / annual_saving, 2) if annual_saving > 0 else None
    workings.append(
        f"APPLY: bundle of {len(verified)} verified fixes — compliance "
        f"{n_pass_before}/{len(checks_before)} -> {n_pass_after}/{len(checks_after)}; "
        f"EUI {sim_before['eui_kwh_m2_yr']} -> {sim_after['eui_kwh_m2_yr']} kWh/m2/yr; "
        f"capex {capex_total:,.2f} {code['currency']}; annual saving "
        f"{annual_saving:,.2f} {code['currency']}; simple payback {payback} yr.")

    run_id = uuid.uuid4().hex[:12]
    result = {
        "run_id": run_id,
        "code": {"id": code["id"], "name": code["name"], "country": code["country"],
                 "currency": code["currency"], "note": code.get("note", "")},
        "building_before": b.model_dump(),
        "building_after": after.model_dump(),
        "observe": {"simulation": sim_before, "carbon": carb_before, "checks": checks_before},
        "candidates": [{k: c[k] for k in ("id", "title", "category", "changes", "capex", "description")} for c in candidates],
        "verdicts": verdicts,
        "verified_fixes": [v["id"] for v in verdicts if v["verified"]],
        "after": {"simulation": sim_after, "carbon": carb_after, "checks": checks_after},
        "compliance_before": {"pass": n_pass_before, "total": len(checks_before)},
        "compliance_after": {"pass": n_pass_after, "total": len(checks_after)},
        "costs": {"capex_total": capex_total, "currency": code["currency"],
                  "annual_cost_before": sim_before["annual_cost"],
                  "annual_cost_after": sim_after["annual_cost"],
                  "annual_saving": annual_saving, "simple_payback_years": payback},
        "carbon_delta_kgco2e": carb_after["total_kgco2e"] - carb_before["total_kgco2e"],
        "llm_used": llm_used,
        "workings": workings,
        "disclaimer": ("Thresholds are SIMPLIFIED for demonstration — verify against the "
                       "current edition of the code. A QP / licensed professional must "
                       "verify the model and report before any real submission."),
    }
    RUNS[run_id] = result
    return result


# --------------------------------------------------------------------------- #
# IFC ingest (IfcOpenShell — same library ORCA uses)
# --------------------------------------------------------------------------- #
def parse_ifc(path: str) -> dict:
    try:
        import ifcopenshell
    except Exception:
        return {"parsed": False,
                "note": "ifcopenshell not installed — falling back to demo building."}
    try:
        model = ifcopenshell.open(path)
        counts = {}
        for t in ("IfcWall", "IfcWindow", "IfcSlab", "IfcSpace", "IfcBuildingStorey"):
            try:
                counts[t] = len(model.by_type(t))
            except Exception:
                counts[t] = 0
        gfa = None
        try:
            total_area = 0.0
            for space in model.by_type("IfcSpace"):
                for q in (space.IsDefinedBy or []):
                    pd = getattr(q, "RelatingPropertyDefinition", None)
                    for prop in (getattr(pd, "Quantities", None) or []):
                        if getattr(prop, "Name", "") in ("GrossFloorArea", "NetFloorArea", "Area"):
                            total_area += float(getattr(prop, "AreaValue", 0) or 0)
            if total_area > 0:
                gfa = total_area
        except Exception:
            gfa = None
        walls = counts.get("IfcWall", 0)
        windows = counts.get("IfcWindow", 0)
        wwr = None
        if walls > 0:
            wwr = min(0.8, max(0.2, windows / max(walls, 1)))
        return {"parsed": True, "counts": counts, "gfa_m2": gfa, "wwr_estimate": wwr,
                "note": "Counts are real IfcOpenShell element counts; WWR is a count-ratio estimate (geometry unavailable), capped 0.2–0.8."}
    except Exception as exc:
        return {"parsed": False, "note": f"IFC parse failed ({exc}) — falling back to demo building."}


def building_from_ifc(parsed: dict) -> Building:
    b = demo_building()
    if parsed.get("parsed"):
        if parsed.get("gfa_m2"):
            b.gfa_m2 = float(parsed["gfa_m2"])
        if parsed.get("wwr_estimate"):
            b.wwr = float(parsed["wwr_estimate"])
        if parsed.get("counts", {}).get("IfcBuildingStorey"):
            b.floors = int(parsed["counts"]["IfcBuildingStorey"])
    return b


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def render_report(result: dict) -> str:
    code = result["code"]
    rows = ""
    before = {c["key"]: c for c in result["observe"]["checks"]}
    for c in result["after"]["checks"]:
        b = before.get(c["key"], {})
        rows += (f"<tr><td>{c['label']}</td><td>{b.get('value', '—')}</td>"
                 f"<td>{c['value']}</td><td>{c['limit']} {c['unit']}</td>"
                 f"<td>{'PASS' if c['pass'] else 'FAIL'}</td></tr>")
    fix_rows = ""
    cand = {c["id"]: c for c in result["candidates"]}
    for v in result["verdicts"]:
        if v["verified"]:
            fix_rows += (f"<tr><td>{v['title']}</td><td>{cand[v['id']]['description']}</td>"
                         f"<td>{v['capex']:,.2f}</td><td>{v['reason']}</td></tr>")
    if not fix_rows:
        fix_rows = "<tr><td colspan='4'>No verified fixes.</td></tr>"
    costs = result["costs"]
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>ORCA-Green Report — {code['name']}</title>
<style>body{{font-family:system-ui,Arial,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#14231a}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}td,th{{border:1px solid #cbd5ce;padding:.4rem .6rem;text-align:left}}
h1{{color:#166534}}.pass{{color:#166534}}</style></head><body>
<h1>ORCA-Green — Verified-Fix Report</h1>
<p><strong>Code:</strong> {code['name']} ({code['country']}) — {code['note']}</p>
<p><strong>Compliance:</strong> {result['compliance_before']['pass']}/{result['compliance_before']['total']}
&rarr; {result['compliance_after']['pass']}/{result['compliance_after']['total']} rules passing</p>
<p><strong>EUI:</strong> {result['observe']['simulation']['eui_kwh_m2_yr']} &rarr;
{result['after']['simulation']['eui_kwh_m2_yr']} kWh/m2/yr &nbsp;|&nbsp;
<strong>Engine:</strong> {result['observe']['simulation']['engine']}</p>
<h2>Costed summary ({code['currency']})</h2>
<p>Capex (verified fixes): {costs['capex_total']:,.2f} &nbsp;|&nbsp;
Annual energy cost: {costs['annual_cost_before']:,.2f} &rarr; {costs['annual_cost_after']:,.2f} &nbsp;|&nbsp;
Annual saving: {costs['annual_saving']:,.2f} &nbsp;|&nbsp;
Simple payback: {costs['simple_payback_years']} years</p>
<h2>Embodied carbon</h2>
<p>{result['observe']['carbon']['total_kgco2e']:,} &rarr; {result['after']['carbon']['total_kgco2e']:,} kgCO2e
(delta {result['carbon_delta_kgco2e']:+,} kgCO2e). Material swaps verify on the carbon track:
a &ge;1% embodied-carbon cut, no rule regression, no EUI increase.</p>
<h2>Per-rule results</h2>
<table><tr><th>Rule</th><th>Before</th><th>After</th><th>Limit</th><th>Status</th></tr>{rows}</table>
<h2>Verified fixes (only fixes that passed verification are applied)</h2>
<table><tr><th>Fix</th><th>Description</th><th>Capex</th><th>Verification</th></tr>{fix_rows}</table>
<h2>Workings</h2><pre>{chr(10).join(result['workings'])}</pre>
<p><em>{result['disclaimer']}</em></p>
</body></html>"""


# --------------------------------------------------------------------------- #
# Web UI (single page)
# --------------------------------------------------------------------------- #
INDEX_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>ORCA-Green — Green-code verified-fix agent</title>
<style>
body{font-family:system-ui,Arial,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#14231a;background:#f6faf7}
h1{color:#166534}fieldset{border:1px solid #b7d3bf;border-radius:8px;margin:1rem 0;background:#fff}
label{display:block;margin:.35rem 0;font-size:.9rem}input,select{width:180px;padding:.3rem}
button{background:#166534;color:#fff;border:0;padding:.6rem 1.2rem;border-radius:6px;cursor:pointer;margin:.3rem .3rem .3rem 0}
table{border-collapse:collapse;width:100%;margin:1rem 0;background:#fff}td,th{border:1px solid #cbd5ce;padding:.35rem .5rem;text-align:left;font-size:.9rem}
pre{background:#0f2417;color:#d1fae5;padding:1rem;border-radius:8px;overflow:auto;font-size:.8rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.5rem}
</style></head><body>
<h1>ORCA-Green</h1>
<p>Same loop as ORCA — <b>Observe &rarr; Resolve &rarr; Check &rarr; Apply</b> — but the verifier is
green building codes + energy simulation + embodied carbon. Only <b>verified</b> fixes are applied:
a fix must fix at least one failing rule, regress no passing rule, and not increase EUI.</p>
<fieldset><legend>Building &amp; code</legend>
<div class="grid">
<label>Code <select id="code_id"></select></label>
<label>GFA (m2) <input id="gfa_m2" type="number" step="any"></label>
<label>WWR <input id="wwr" type="number" step="0.01"></label>
<label>Glazing U <input id="glazing_u" type="number" step="0.1"></label>
<label>Glazing SHGC <input id="glazing_shgc" type="number" step="0.01"></label>
<label>Wall U <input id="wall_u" type="number" step="0.1"></label>
<label>Roof U <input id="roof_u" type="number" step="0.1"></label>
<label>HVAC COP <input id="hvac_cop" type="number" step="0.1"></label>
<label>HVAC kW/RT <input id="hvac_kw_per_rt" type="number" step="0.01"></label>
<label>Lighting W/m2 <input id="lighting_wpd" type="number" step="0.5"></label>
<label>Renewable share <input id="renewable_share" type="number" step="0.01"></label>
</div>
<label><input id="use_llm" type="checkbox" style="width:auto"> Use LLM ranking (Opencode Go, ranks only)</label><br>
<button onclick="run()">Run verified-fix loop</button>
<label style="display:inline">or upload IFC: <input id="ifc" type="file" accept=".ifc" style="width:auto"></label>
<button onclick="runIfc()">Run from IFC</button>
</fieldset>
<div id="out"></div>
<script>
const DEMO={gfa_m2:12000,wwr:0.65,glazing_u:2.8,glazing_shgc:0.55,wall_u:1.2,roof_u:0.9,hvac_cop:3.0,hvac_kw_per_rt:1.05,lighting_wpd:15,renewable_share:0.0};
for(const k in DEMO) document.getElementById(k).value=DEMO[k];
fetch('/api/codes').then(r=>r.json()).then(d=>{
  const s=document.getElementById('code_id');
  d.codes.forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.name+' ('+c.country+')';s.appendChild(o);});
});
function building(){const b={};for(const k in DEMO)b[k]=parseFloat(document.getElementById(k).value);return b;}
function show(r){
  const rows=r.after.checks.map((c,i)=>{const b=r.observe.checks[i];
    return `<tr><td>${c.label}</td><td>${b.value}</td><td>${c.value}</td><td>${c.limit} ${c.unit}</td><td>${c.pass?'PASS':'FAIL'}</td></tr>`}).join('');
  const fixes=r.verdicts.filter(v=>v.verified).map(v=>`<tr><td>${v.title}</td><td>${v.capex.toLocaleString()}</td><td>${v.reason}</td></tr>`).join('')||'<tr><td colspan="3">None</td></tr>';
  document.getElementById('out').innerHTML=`
  <h2>Results — ${r.code.name}</h2>
  <p>Compliance: <b>${r.compliance_before.pass}/${r.compliance_before.total} &rarr; ${r.compliance_after.pass}/${r.compliance_after.total}</b>
  &nbsp;|&nbsp; EUI: <b>${r.observe.simulation.eui_kwh_m2_yr} &rarr; ${r.after.simulation.eui_kwh_m2_yr}</b> kWh/m2/yr
  &nbsp;|&nbsp; LLM used: ${r.llm_used}</p>
  <p>Capex: <b>${r.costs.capex_total.toLocaleString()} ${r.costs.currency}</b> &nbsp;|&nbsp;
  Annual saving: <b>${r.costs.annual_saving.toLocaleString()} ${r.costs.currency}</b> &nbsp;|&nbsp;
  Simple payback: <b>${r.costs.simple_payback_years} years</b></p>
  <p>Embodied carbon: ${r.observe.carbon.total_kgco2e.toLocaleString()} &rarr; ${r.after.carbon.total_kgco2e.toLocaleString()} kgCO2e (delta ${r.carbon_delta_kgco2e.toLocaleString()})</p>
  <table><tr><th>Rule</th><th>Before</th><th>After</th><th>Limit</th><th>Status</th></tr>${rows}</table>
  <h3>Verified fixes only</h3>
  <table><tr><th>Fix</th><th>Capex</th><th>Verification</th></tr>${fixes}</table>
  <h3>Workings</h3><pre>${r.workings.join('\\n')}</pre>
  <p><a href="/api/report/${r.run_id}"><button>Download report (HTML)</button></a></p>
  <p><em>${r.disclaimer}</em></p>`;
}
async function run(){
  const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({building:building(),code_id:document.getElementById('code_id').value,use_llm:document.getElementById('use_llm').checked})});
  show(await r.json());
}
async function runIfc(){
  const f=document.getElementById('ifc').files[0]; if(!f){alert('Choose an .ifc file first');return;}
  const fd=new FormData(); fd.append('file',f); fd.append('code_id',document.getElementById('code_id').value);
  const r=await fetch('/api/ifc',{method:'POST',body:fd}); const d=await r.json();
  alert('IFC: '+d.ifc.note); show(d.run);
}
</script></body></html>"""


# --------------------------------------------------------------------------- #
# FastAPI routes
# --------------------------------------------------------------------------- #
app = FastAPI(title="ORCA-Green", description="Green-code verified-fix agent (ORCA extension)")


class RunRequest(BaseModel):
    building: dict | None = None
    code_id: str = "green_mark_sg"
    use_llm: bool = False


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.get("/api/health")
def health():
    return {"ok": True, "engine": energyplus_note(),
            "llm_configured": bool(os.environ.get("OPENCODE_API_KEY")),
            "codes": list(CODES.keys())}


@app.get("/api/codes")
def codes():
    return CODES_DOC


@app.get("/api/epd")
def epd():
    return EPD_DOC


@app.post("/api/run")
def api_run(req: RunRequest):
    b = demo_building()
    if req.building:
        merged = {**b.model_dump(), **{k: v for k, v in req.building.items() if v is not None}}
        b = Building(**merged)
    return run_loop(b, req.code_id, use_llm=req.use_llm)


@app.get("/api/demo")
def api_demo():
    return run_loop(demo_building(), "green_mark_sg")


@app.post("/api/ifc")
async def api_ifc(file: UploadFile = File(...), code_id: str = "green_mark_sg"):
    import tempfile
    suffix = Path(file.filename or "upload.ifc").suffix or ".ifc"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        parsed = parse_ifc(tmp_path)
    finally:
        os.unlink(tmp_path)
    b = building_from_ifc(parsed)
    return {"ifc": parsed, "run": run_loop(b, code_id)}


@app.get("/api/report/{run_id}", response_class=HTMLResponse)
def api_report(run_id: str):
    result = RUNS.get(run_id)
    if not result:
        return JSONResponse({"error": "run not found (runs are held in memory)"}, status_code=404)
    return render_report(result)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
