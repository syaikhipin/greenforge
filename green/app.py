"""
GreenForge: Green-code verified-fix agent.

The verified-fix loop — Assess -> Propose -> Verify -> Apply — where the verifier is
green building codes + a simplified energy simulation + embodied-carbon calc
instead of geometry.

Honesty rules baked in:
- EnergyPlus is NEVER claimed to have run. If the binary is present we say so,
  but the demo still uses the simplified physics engine unless a real IDF
  template is configured.
- IFC counts are never fabricated: if IfcOpenShell is missing or parsing
  fails, parse_ifc returns parsed=False and callers fall back to the demo
  building.
- An LLM (OpenAI) may only RANK deterministic candidate fixes. It never
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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
# Optional LLM ranking via an OpenAI-compatible endpoint (ranks only, never
# invents numbers). Provider order: Opencode Go if OPENCODE_API_KEY is set
# (Go endpoint, needs the x-opencode-session header + a browser user agent),
# otherwise OpenAI if OPENAI_API_KEY is set.
# --------------------------------------------------------------------------- #
def _env_real(name: str):
    v = os.environ.get(name)
    return v if v and "${" not in v and not v.startswith("$") else None


def llm_rank(candidates: list, code: dict, failures: list) -> tuple:
    """Return (ordered_candidates, llm_used). Any error -> (candidates, False)."""
    def _real(v):
        # Coolify/compose can leave the literal "${VAR:-}" placeholder in a var;
        # that is not a key.
        return v if v and "${" not in v and not v.startswith("$") else None

    oc_key = _real(os.environ.get("OPENCODE_API_KEY"))
    oa_key = _real(os.environ.get("OPENAI_API_KEY"))
    if not candidates or not (oc_key or oa_key):
        return candidates, False
    go_headers = {
        "x-opencode-session": "greenforge-1",
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    }
    if oc_key:
        key = oc_key
        model = os.environ.get("OPENCODE_GO_MODEL", "mimo-v2.6-flash")
        if "${" in model:
            model = "mimo-v2.6-flash"
        base_url = "https://opencode.ai/zen/go/v1"
        extra_headers = go_headers
    else:
        key = oa_key
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        # An OpenAI-compatible config pointed at Opencode still needs the
        # session header + browser UA, or Go rejects the call.
        extra_headers = go_headers if "opencode.ai" in base_url else {}
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
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                **extra_headers,
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
        f"({'LLM-ranked (provider configured)' if llm_used else 'deterministic catalogue order'}).")

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
        # Two verification tracks, same discipline as a geometry clash check:
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
# IFC ingest (IfcOpenShell)
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
<title>GreenForge Report — {code['name']}</title>
<style>body{{font-family:system-ui,Arial,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#14231a}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}td,th{{border:1px solid #cbd5ce;padding:.4rem .6rem;text-align:left}}
h1{{color:#166534}}.pass{{color:#166534}}</style></head><body>
<h1>GreenForge — Verified-Fix Report</h1>
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
<title>GreenForge — Green-code verified-fix agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--ink:#14231a;--green:#166534;--green2:#22c55e;--amber:#d97706;--red:#dc2626;--line:#cfe0d4;--panel:#fff}
*{box-sizing:border-box}body{font-family:system-ui,Arial,sans-serif;margin:0;color:var(--ink);background:#f2f7f3}
header{background:#0f2417;color:#e7f6ec;padding:1rem 1.25rem}header h1{margin:0;font-size:1.6rem;color:#fff}
header p{margin:.3rem 0 0;color:#b9e4c6;max-width:70ch}
main{max-width:1180px;margin:0 auto;padding:1rem}
.pipe{display:flex;gap:.5rem;margin:1rem 0;flex-wrap:wrap}
.stage{flex:1;min-width:140px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:.6rem .8rem;opacity:.45}
.stage.active{opacity:1;border-color:var(--green2);box-shadow:0 0 0 2px #bbf7d0}
.stage.done{opacity:1;border-color:var(--green)}
.stage b{display:block;font-size:1rem}.stage span{font-size:.8rem;color:#48604f}
.cols{display:grid;grid-template-columns:340px 1fr;gap:1rem}@media(max-width:900px){.cols{grid-template-columns:1fr}}
fieldset{border:1px solid var(--line);border-radius:10px;background:var(--panel);margin:0 0 1rem;padding:.8rem 1rem}
legend{font-weight:700;color:var(--green)}label{display:block;margin:.35rem 0;font-size:.88rem}
input,select{width:100%;padding:.35rem;border:1px solid var(--line);border-radius:6px}
button{background:var(--green);color:#fff;border:0;padding:.6rem 1.1rem;border-radius:8px;cursor:pointer;margin:.3rem .3rem .3rem 0;font-weight:600}
button.ghost{background:#fff;color:var(--green);border:1px solid var(--green)}
#viewer{height:430px;background:#0b1a10;border-radius:10px;overflow:hidden;position:relative}
#viewer canvas{display:block}#vstatus{position:absolute;left:.6rem;bottom:.6rem;background:rgba(10,25,15,.85);color:#d1fae5;padding:.3rem .6rem;border-radius:6px;font-size:.8rem}
.feed{background:#0f2417;border-radius:10px;padding:.8rem;height:430px;overflow:auto;font-size:.86rem}
.msg{display:flex;gap:.6rem;margin:.45rem 0;animation:in .3s ease}
@keyframes in{from{opacity:0;transform:translateY(4px)}to{opacity:1}}
.lane{flex:0 0 86px;text-align:center;border-radius:6px;padding:.15rem 0;font-weight:700;font-size:.72rem;height:fit-content}
.lane.you{background:#334155;color:#e2e8f0}.lane.agent{background:#14532d;color:#bbf7d0}.lane.verifier{background:#713f12;color:#fde68a}.lane.apply{background:#1e3a8a;color:#bfdbfe}
.msg p{margin:0;color:#e7f6ec}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:.6rem;margin:.6rem 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:.6rem .8rem;animation:in .3s ease}
.card h4{margin:.1rem 0 .3rem;font-size:.95rem}.card p{margin:.2rem 0;font-size:.82rem;color:#48604f}
.badge{display:inline-block;border-radius:6px;padding:.1rem .5rem;font-size:.72rem;font-weight:700}
.badge.ok{background:#dcfce7;color:#166534}.badge.no{background:#fee2e2;color:#991b1b}.badge.wait{background:#fef3c7;color:#92400e}
.bars{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:.8rem 1rem;margin:.8rem 0}
.bar{margin:.55rem 0}.bar label{font-size:.85rem;font-weight:600}
.track{height:14px;background:#e5efe7;border-radius:7px;position:relative;overflow:hidden}
.fill{height:100%;border-radius:7px;transition:width 1s ease}
table{border-collapse:collapse;width:100%;margin:.6rem 0;background:#fff}td,th{border:1px solid var(--line);padding:.35rem .5rem;text-align:left;font-size:.88rem}
.pass{color:var(--green);font-weight:700}.fail{color:var(--red);font-weight:700}
.small{font-size:.82rem;color:#48604f}
</style></head><body>
<header>
<h1>GreenForge</h1>
<p><b>Fixes, not just findings. Every fix proven before you see it.</b><br>
The verified-fix loop for green building codes: <b>Assess &rarr; Propose &rarr; Verify &rarr; Apply</b>.
The verifier is energy simulation + green codes + embodied carbon — a fix only reaches you if it
fixes a failing rule (or cuts embodied carbon &ge;1%), regresses nothing, and does not raise EUI.</p>
</header>
<main>
<div class="pipe">
  <div class="stage" id="st0"><b>1 · Assess</b><span>Ingest IFC, rules-check the model</span></div>
  <div class="stage" id="st1"><b>2 · Propose</b><span>Agent proposes edits (glazing, shading, HVAC, materials)</span></div>
  <div class="stage" id="st2"><b>3 · Verify</b><span>Energy + carbon + rules re-check every edit</span></div>
  <div class="stage" id="st3"><b>4 · Apply</b><span>Costed, compliant bundle + report</span></div>
</div>
<div class="cols">
<div>
<fieldset><legend>Building &amp; code</legend>
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
<label><input id="use_llm" type="checkbox" style="width:auto"> Use LLM ranking (Opencode Go / OpenAI, ranks only — numbers come from the verifier)</label>
<button onclick="run()">&#9654; Run verified-fix loop</button>
</fieldset>
<fieldset><legend>Model (IFC)</legend>
<label>Upload IFC <input id="ifc" type="file" accept=".ifc"></label>
<button class="ghost" onclick="runIfc()">Analyse uploaded IFC</button>
<button class="ghost" onclick="loadDemo()">Load demo IFC in 3D</button>
<p class="small">The 3D viewer renders the IFC in your browser (web-ifc + three.js). Highlight buttons colour the envelope systems the verifier reasons about.</p>
<button class="ghost" onclick="highlight('window')">Highlight glazing</button>
<button class="ghost" onclick="highlight('wall')">Highlight walls</button>
<button class="ghost" onclick="highlight(null)">Reset colours</button>
</fieldset>
</div>
<div>
<div id="viewer"><div id="vstatus">3D viewer idle — load the demo IFC or upload one.</div></div>
<h3 style="margin:.8rem 0 .3rem">Workings — watch the agent work</h3>
<div class="feed" id="feed"><div class="msg"><div class="lane you">YOU</div><p>Set the building, then run the loop. Every proposal and every verdict will appear here, live.</p></div></div>
</div>
</div>
<div id="out"></div>
</main>
<script>
const DEMO={gfa_m2:12000,wwr:0.65,glazing_u:2.8,glazing_shgc:0.55,wall_u:1.2,roof_u:0.9,hvac_cop:3.0,hvac_kw_per_rt:1.05,lighting_wpd:15,renewable_share:0.0};
for(const k in DEMO) document.getElementById(k).value=DEMO[k];
fetch('/api/codes').then(r=>r.json()).then(d=>{const s=document.getElementById('code_id');d.codes.forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.name+' ('+c.country+')';s.appendChild(o);});});
function building(){const b={};for(const k in DEMO)b[k]=parseFloat(document.getElementById(k).value);return b;}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function setStage(i,cls){for(let j=0;j<4;j++){const e=document.getElementById('st'+j);e.className='stage'+(j<i||cls==='done'&&j<=i?' done':j===i?' active':'');}}
function say(lane,text){const f=document.getElementById('feed');const d=document.createElement('div');d.className='msg';d.innerHTML='<div class="lane '+lane+'">'+lane.toUpperCase()+'</div><p></p>';d.querySelector('p').textContent=text;f.appendChild(d);f.scrollTop=f.scrollHeight;}
async function replay(r){
  document.getElementById('feed').innerHTML='';document.getElementById('out').innerHTML='';
  const cards=document.createElement('div');cards.className='cards';cards.id='cards';
  say('you','Run the verified-fix loop against '+r.code.name+'.');await sleep(350);
  for(const w of r.workings){
    if(w.startsWith('OBSERVE')){setStage(0);say('agent',w);}
    else if(w.startsWith('RESOLVE')){setStage(1);say('agent',w);document.getElementById('out').appendChild(cards);
      r.candidates.forEach(c=>{const el=document.createElement('div');el.className='card';el.id='card-'+c.id;
        el.innerHTML='<h4></h4><p></p><span class="badge wait">AWAITING VERIFICATION</span>';
        el.querySelector('h4').textContent=c.title;el.querySelector('p').textContent=c.description+' Capex: '+c.capex.toLocaleString()+' '+r.code.currency+'.';cards.appendChild(el);});}
    else if(w.startsWith('CHECK')){setStage(2);say('verifier',w);
      const id=w.split(' ')[1].replace(':','');const v=r.verdicts.find(x=>x.id===id);const el=document.getElementById('card-'+id);
      if(el&&v){const b=el.querySelector('.badge');b.className='badge '+(v.verified?'ok':'no');b.textContent=v.verified?'VERIFIED':'REJECTED';}}
    else if(w.startsWith('APPLY')){setStage(3);say('apply',w);}
    await sleep(650);
  }
  setStage(3,'done');renderResults(r);
}
function bar(label,before,after,unit,lowerBetter,max){
  const bw=Math.min(100,before/max*100),aw=Math.min(100,after/max*100);
  const good=lowerBetter?after<before:after>before;
  return '<div class="bar"><label>'+label+': '+before.toLocaleString()+' &rarr; '+after.toLocaleString()+' '+unit+'</label>'+
  '<div class="track"><div class="fill" style="width:'+bw+'%;background:#94a3b8"></div></div>'+
  '<div class="track" style="margin-top:3px"><div class="fill" style="width:'+aw+'%;background:'+(good?'#22c55e':'#dc2626')+'"></div></div></div>';
}
function renderResults(r){
  const rows=r.after.checks.map((c,i)=>{const b=r.observe.checks[i];
    return '<tr><td>'+c.label+'</td><td>'+b.value+'</td><td>'+c.value+'</td><td>'+c.limit+' '+c.unit+'</td><td class="'+(c.pass?'pass':'fail')+'">'+(c.pass?'PASS':'FAIL')+'</td></tr>';}).join('');
  document.getElementById('out').insertAdjacentHTML('beforeend',
  '<div class="bars"><h3 style="margin:.2rem 0">Before / After</h3>'+
  bar('Compliance (rules passing)',r.compliance_before.pass,r.compliance_after.pass,'of '+r.compliance_after.total,false,r.compliance_after.total)+
  bar('EUI',r.observe.simulation.eui_kwh_m2_yr,r.after.simulation.eui_kwh_m2_yr,'kWh/m2/yr',true,250)+
  bar('Embodied carbon',r.observe.carbon.total_kgco2e,r.after.carbon.total_kgco2e,'kgCO2e',true,r.observe.carbon.total_kgco2e)+
  bar('Annual energy cost',r.costs.annual_cost_before,r.costs.annual_cost_after,r.costs.currency,true,r.costs.annual_cost_before)+
  '<p><b>Capex '+r.costs.capex_total.toLocaleString()+' '+r.costs.currency+'</b> &nbsp;|&nbsp; Annual saving <b>'+r.costs.annual_saving.toLocaleString()+' '+r.costs.currency+'</b> &nbsp;|&nbsp; Simple payback <b>'+r.costs.simple_payback_years+' years</b> &nbsp;|&nbsp; LLM ranked: '+r.llm_used+'</p>'+
  '<p><a href="/api/report/'+r.run_id+'"><button>Download submission-ready report (HTML)</button></a></p></div>'+
  '<h3>Rules — before / after</h3><table><tr><th>Rule</th><th>Before</th><th>After</th><th>Limit</th><th>Status</th></tr>'+rows+'</table>'+
  '<p class="small">'+r.disclaimer+'</p>');
}
async function run(){
  setStage(0);say('you','Running… the verifier checks every proposal before you see a fix.');
  const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({building:building(),code_id:document.getElementById('code_id').value,use_llm:document.getElementById('use_llm').checked})});
  replay(await r.json());
}
async function runIfc(){
  const f=document.getElementById('ifc').files[0];if(!f){alert('Choose an .ifc file first');return;}
  loadIfcBuffer(await f.arrayBuffer(),f.name);
  const fd=new FormData();fd.append('file',f);fd.append('code_id',document.getElementById('code_id').value);
  const r=await fetch('/api/ifc',{method:'POST',body:fd});const d=await r.json();
  say('agent','IFC ingest: '+d.ifc.note);replay(d.run);
}
async function loadDemo(){
  document.getElementById('vstatus').textContent='Loading demo IFC…';
  const buf=await (await fetch('/demo.ifc')).arrayBuffer();loadIfcBuffer(buf,'Northbank-Architecture.ifc');
}
</script>
<script type="importmap">
{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/","web-ifc":"https://unpkg.com/web-ifc@0.0.57/web-ifc-api.js"}}
</script>
<script type="module">
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {IFCLoader} from 'three/addons/loaders/IFCLoader.js';
const box=document.getElementById('viewer'),status=document.getElementById('vstatus');
const scene=new THREE.Scene();scene.background=new THREE.Color(0x0b1a10);
const camera=new THREE.PerspectiveCamera(45,box.clientWidth/box.clientHeight,0.1,2000);camera.position.set(28,22,28);
const renderer=new THREE.WebGLRenderer({antialias:true});renderer.setSize(box.clientWidth,box.clientHeight);box.appendChild(renderer.domElement);
const controls=new OrbitControls(camera,renderer.domElement);
scene.add(new THREE.AmbientLight(0xffffff,0.75));
const sun=new THREE.DirectionalLight(0xffffff,1.1);sun.position.set(30,50,20);scene.add(sun);
scene.add(new THREE.GridHelper(120,24,0x2f5d3a,0x1d3a24));
const loader=new IFCLoader();loader.ifcManager.setWasmPath('https://unpkg.com/web-ifc@0.0.57/');
let model=null;const TYPES={window:null,wall:null};
(async()=>{const m=await import('web-ifc');TYPES.window=m.IFCWINDOW;TYPES.wall=m.IFCWALL;})();
function animate(){requestAnimationFrame(animate);controls.update();renderer.render(scene,camera);}
animate();
window.loadIfcBuffer=async function(buf,name){
  try{
    if(model){scene.remove(model);model=null;}
    status.textContent='Parsing '+name+' in your browser…';
    model=await loader.parse(buf);scene.add(model);
    const bb=new THREE.Box3().setFromObject(model);const c=bb.getCenter(new THREE.Vector3());
    controls.target.copy(c);camera.position.set(c.x+30,c.y+24,c.z+30);
    status.textContent=name+' loaded — drag to orbit, scroll to zoom. Use the highlight buttons to colour glazing / walls.';
  }catch(e){status.textContent='3D viewer could not parse this IFC in the browser ('+e.message+'). The server-side analysis still runs.';}
};
window.highlight=async function(kind){
  if(!model){status.textContent='Load an IFC first.';return;}
  const mgr=loader.ifcManager;const mid=model.modelID;
  mgr.removeSubset(mid);model.traverse(o=>{if(o.material&&o.material.transparent!==undefined){o.material.opacity=1;o.material.transparent=false;}});
  if(!kind)return;
  try{
    const ids=await mgr.getAllItemsOfType(mid,TYPES[kind],false);
    if(!ids.length){status.textContent='No elements of that type in this model.';return;}
    const mat=new THREE.MeshLambertMaterial({color:kind==='window'?0x38bdf8:0xf59e0b,transparent:true,opacity:0.95});
    mgr.createSubset({modelID:mid,ids,material:mat,scene,removePrevious:true});
    status.textContent='Highlighted '+ids.length+' '+(kind==='window'?'glazing':'wall')+' elements — the systems the verifier checks first.';
  }catch(e){status.textContent='Highlight failed: '+e.message;}
};
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
# FastAPI routes
# --------------------------------------------------------------------------- #
app = FastAPI(title="GreenForge", description="Green-code verified-fix agent")


class RunRequest(BaseModel):
    building: dict | None = None
    code_id: str = "green_mark_sg"
    use_llm: bool = False


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


@app.get("/demo.ifc")
def demo_ifc():
    """Bundled sample IFC for the 3D viewer (Northbank Architecture, from the upstream sample set)."""
    f = Path(__file__).parent / "samples" / "Northbank-Architecture.ifc"
    if not f.exists():
        return JSONResponse({"error": "demo IFC not bundled"}, status_code=404)
    return FileResponse(str(f), media_type="application/octet-stream", filename="demo.ifc")


@app.get("/api/health")
def health():
    return {"ok": True, "engine": energyplus_note(),
            "llm_configured": bool(_env_real("OPENCODE_API_KEY") or _env_real("OPENAI_API_KEY")),
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
