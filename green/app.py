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
    # One retry: the Go endpoint occasionally drops a call (observed live as
    # an intermittent deterministic fallback on an otherwise healthy setup).
    for attempt in range(2):
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
            if attempt == 1:
                return candidates, False
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
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GreenForge · AI green-code coordination</title>
<style>
:root{
  --ink:#0b1f3a; --navy:#0a1428; --bg:#070d1a; --teal:#2dd4bf; --teal-d:#0d9488;
  --amber:#f5b942; --red:#ef4444; --green:#22c55e; --grey:#64748b; --line:#e2e8f0;
  --panel:#ffffff; --wash:#f4f6f9; --txt:#0f172a; --mut:#5b6b82;
}
*{box-sizing:border-box}
body{margin:0;font-family:"Avenir Next","Segoe UI",system-ui,-apple-system,sans-serif;color:var(--txt);background:var(--wash)}
button{font:inherit;cursor:pointer}
h1,h2,h3{margin:0}
/* ---------- shared ---------- */
.pill{display:inline-flex;align-items:center;gap:.45rem;border-radius:999px;padding:.55rem 1.1rem;font-weight:600;border:1px solid transparent}
.btn-white{background:#fff;color:var(--navy)}
.btn-ghost{background:transparent;color:#fff;border-color:#3b4a68}
.btn-dark{background:var(--ink);color:#fff;border:none;border-radius:8px;padding:.6rem 1rem;font-weight:600}
.btn-line{background:#fff;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:.6rem 1rem;font-weight:600}
.btn-dark:disabled{opacity:.45;cursor:not-allowed}
.eyebrow{letter-spacing:.18em;font-size:.72rem;font-weight:700;text-transform:uppercase;color:var(--teal)}
.small{font-size:.8rem;color:var(--mut)}
/* ---------- landing ---------- */
#landing{background:var(--bg);color:#e8eef7;min-height:100vh;background-image:linear-gradient(rgba(45,212,191,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(45,212,191,.05) 1px,transparent 1px);background-size:44px 44px}
.topbar{display:flex;align-items:center;gap:2rem;padding:.9rem 2rem;border-bottom:1px solid #1c2a47;position:sticky;top:0;background:rgba(7,13,26,.92);z-index:5}
.wordmark{font-weight:800;font-size:1.25rem;letter-spacing:.04em;color:#fff}
.wordmark span{color:var(--teal)}
.topbar nav{display:flex;gap:1.4rem;margin:0 auto}
.topbar nav a{color:#b8c4da;text-decoration:none;font-size:.9rem}
.topbar nav a:hover{color:#fff}
.hero{display:grid;grid-template-columns:45% 55%;gap:2rem;max-width:1400px;margin:0 auto;padding:3.5rem 2rem 2rem}
.hero h1{font-size:3.4rem;line-height:1.04;color:#fff;font-weight:800}
.hero .sub{color:var(--teal);font-size:1.7rem;font-weight:700;margin-top:.4rem}
.hero p.lede{color:#aebbd2;font-size:1.05rem;line-height:1.55;max-width:34rem}
.award{display:inline-block;background:rgba(245,185,66,.14);color:var(--amber);border:1px solid rgba(245,185,66,.45);padding:.35rem .8rem;border-radius:999px;font-size:.8rem;font-weight:600;margin:1rem 0}
.cta-row{display:flex;gap:.8rem;align-items:center;flex-wrap:wrap;margin-top:1.2rem}
.cta-row a{color:#b8c4da;font-size:.9rem}
.replay-card{background:#0c1626;border:1px solid #1e2c49;border-radius:14px;padding:1.1rem 1.2rem;position:relative;overflow:hidden}
.replay-card h4{margin:0 0 .6rem;font-size:.75rem;letter-spacing:.15em;color:#7d8fb3}
.pin{position:absolute;background:var(--red);color:#fff;font-size:.72rem;font-weight:700;border-radius:999px;padding:.2rem .55rem;box-shadow:0 4px 14px rgba(0,0,0,.4)}
.replay-line{font-size:.92rem;color:#c9d6ee;padding:.42rem 0;border-bottom:1px dashed #1e2c49;min-height:1.4rem}
.replay-line b{color:var(--teal);letter-spacing:.08em}
.replay-ctl{display:flex;gap:.5rem;align-items:center;margin-top:.8rem}
.chipbtn{background:#12203a;color:#c9d6ee;border:1px solid #24365c;border-radius:999px;padding:.3rem .8rem;font-size:.8rem}
.chipbtn.on{background:var(--teal);color:#06281f;border-color:var(--teal);font-weight:700}
.strip{display:flex;gap:2.2rem;justify-content:center;padding:1.1rem;border-top:1px solid #1c2a47;border-bottom:1px solid #1c2a47;color:#7d8fb3;font-weight:700;letter-spacing:.14em;font-size:.8rem;flex-wrap:wrap}
section.blk{max-width:1200px;margin:0 auto;padding:3.2rem 2rem}
section.blk h2{color:#fff;font-size:2rem;margin:.5rem 0 1rem}
section.blk p{color:#aebbd2;line-height:1.6;max-width:46rem}
.loop-tabs{display:flex;gap:.5rem;margin:1.2rem 0}
.loop-body{background:#0c1626;border:1px solid #1e2c49;border-radius:12px;padding:1.2rem;color:#c9d6ee;min-height:7.5rem;line-height:1.6}
.loop-body b{color:#fff}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin-top:1.2rem}
.card-d{background:#0c1626;border:1px solid #1e2c49;border-radius:12px;padding:1.1rem}
.card-d h3{color:#fff;font-size:1.05rem;margin-bottom:.4rem}
.card-d p{font-size:.9rem}
.demo-card{display:grid;grid-template-columns:1.2fr .8fr;gap:1.5rem;background:#0c1626;border:1px solid #1e2c49;border-radius:14px;padding:1.5rem}
.demo-card h3{color:#fff;font-size:1.4rem}
.stat-row{display:flex;gap:2rem;margin:1rem 0;flex-wrap:wrap}
.stat b{display:block;font-size:1.5rem;color:#fff}
.stat span{font-size:.78rem;color:#7d8fb3;letter-spacing:.06em}
input[type=range]{width:100%}
.trybox{background:#0c1626;border:1px solid #1e2c49;border-radius:12px;padding:1.2rem;max-width:640px}
.verdict{display:inline-block;padding:.25rem .7rem;border-radius:6px;font-weight:800;font-size:.8rem;letter-spacing:.06em}
.verdict.no{background:rgba(239,68,68,.15);color:#f87171;border:1px solid rgba(239,68,68,.5)}
.verdict.ok{background:rgba(34,197,94,.15);color:#4ade80;border:1px solid rgba(34,197,94,.5)}
footer{border-top:1px solid #1c2a47;padding:1.6rem 2rem;color:#7d8fb3;font-size:.85rem;display:flex;gap:1.5rem;flex-wrap:wrap}
footer a{color:#b8c4da}
/* ---------- loading overlay ---------- */
#loading{position:fixed;inset:0;background:var(--bg);color:#e8eef7;display:none;place-items:center;z-index:50}
#loading .box{width:min(480px,90vw)}
#loading h2{color:#fff;margin-bottom:1rem}
.loadbar{height:8px;background:#12203a;border-radius:99px;overflow:hidden;margin:1rem 0}
.loadbar i{display:block;height:100%;width:0;background:var(--teal);transition:width .5s}
.loadstep{padding:.4rem 0;color:#7d8fb3;font-size:.95rem}
.loadstep.done{color:#e8eef7}
.loadstep.done::before{content:"✓ ";color:var(--green);font-weight:800}
/* ---------- workspace ---------- */
#workspace{display:none;height:100vh;flex-direction:column;background:var(--wash)}
.ws-top{display:flex;align-items:center;gap:1.2rem;background:var(--ink);color:#fff;padding:.55rem 1rem}
.ws-top .wordmark{font-size:1.05rem}
.ws-top .proj{font-weight:600}
.ws-top a{color:#b8c4da;font-size:.88rem;text-decoration:none}
.ws-top .sp{flex:1}
.ws-grid{flex:1;display:grid;grid-template-columns:400px minmax(0,1fr) 430px;grid-template-rows:1fr 300px;min-height:0;min-width:0}
.ws-grid>*{min-width:0}
.feed p{word-break:break-word}
#compare dl{grid-template-columns:repeat(4,minmax(0,1fr))}
.panel{background:var(--panel);border-right:1px solid var(--line);overflow-y:auto;padding:.8rem}
.panel h3{font-size:.95rem}
.ptabs{display:flex;gap:.4rem;border-bottom:1px solid var(--line);margin-bottom:.7rem}
.ptab{background:none;border:none;padding:.45rem .7rem;font-weight:600;color:var(--mut);border-bottom:2px solid transparent}
.ptab.on{color:var(--ink);border-color:var(--teal-d)}
.finding{border:1px solid var(--line);border-left:4px solid var(--red);border-radius:8px;padding:.55rem .7rem;margin-bottom:.5rem;cursor:pointer;background:#fff}
.finding.pass{border-left-color:var(--green);opacity:.75}
.finding.sel{outline:2px solid var(--teal-d)}
.finding b{font-size:.9rem}
.finding .meta{font-size:.78rem;color:var(--mut)}
.tag{display:inline-block;font-size:.7rem;font-weight:700;padding:.08rem .45rem;border-radius:99px;background:#fee2e2;color:#b91c1c}
.tag.ok{background:#dcfce7;color:#15803d}
.tag.sys{background:#e0f2fe;color:#0369a1}
.centre{display:flex;flex-direction:column;min-width:0;border-right:1px solid var(--line)}
#viewer{position:relative;flex:1;background:#0b1a10;min-height:280px}
#viewer canvas{display:block}
#vstatus{position:absolute;left:.7rem;bottom:.6rem;color:#d1fae5;font-size:.8rem;background:rgba(4,20,12,.72);padding:.3rem .6rem;border-radius:6px;max-width:92%}
.vtools{position:absolute;left:.6rem;top:.6rem;display:flex;flex-direction:column;gap:.35rem;z-index:3}
.vtools button{background:rgba(10,20,40,.82);color:#e2e8f0;border:1px solid #334155;border-radius:7px;padding:.32rem .55rem;font-size:.75rem;text-align:left}
.vtools button:hover{background:rgba(13,148,136,.85)}
#compare{display:none;background:#fff;border-top:1px solid var(--line);padding:.7rem .9rem}
#compare dl{display:grid;grid-template-columns:repeat(4,1fr);gap:.6rem;margin:.6rem 0 0}
#compare dt{font-size:.7rem;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
#compare dd{margin:.15rem 0 0;font-weight:700}
#activity{grid-column:2/3;background:#fff;border-top:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
#activity .a-head{display:flex;align-items:center;gap:.8rem;padding:.45rem .8rem;border-bottom:1px solid var(--line)}
#activity .a-body{overflow-y:auto;padding:.5rem .8rem;flex:1}
table.steps{border-collapse:collapse;width:100%;font-size:.82rem}
table.steps th,table.steps td{border-bottom:1px solid var(--line);text-align:left;padding:.3rem .45rem}
.feed .msg{display:flex;gap:.6rem;padding:.32rem 0;border-bottom:1px dashed #eef2f7;font-size:.85rem}
.lane{flex:0 0 74px;font-size:.68rem;font-weight:800;letter-spacing:.05em;padding-top:.15rem}
.lane.you{color:#7c3aed}.lane.agent{color:#0369a1}.lane.verifier{color:#b45309}.lane.apply{color:#15803d}
.fixcard{border:1px solid var(--line);border-radius:10px;padding:.7rem .8rem;margin-bottom:.6rem}
.fixcard h4{margin:0 0 .3rem;font-size:.95rem}
.badge{display:inline-block;font-size:.7rem;font-weight:800;letter-spacing:.05em;padding:.18rem .55rem;border-radius:99px;background:#e2e8f0;color:#475569}
.badge.ok{background:#dcfce7;color:#15803d}.badge.no{background:#fee2e2;color:#b91c1c}.badge.wait{background:#fef3c7;color:#92400e}
.fixcard .acts{display:flex;gap:.5rem;margin-top:.55rem}
#workpill{position:fixed;top:64px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;padding:.5rem 1.1rem;border-radius:999px;font-weight:600;display:none;z-index:40;box-shadow:0 8px 24px rgba(0,0,0,.35)}
#menu{position:fixed;top:0;right:0;width:340px;max-width:92vw;height:100vh;background:#fff;box-shadow:-12px 0 32px rgba(2,6,23,.25);z-index:45;display:none;padding:1rem;overflow-y:auto}
#menu h4{margin:1rem 0 .4rem;font-size:.75rem;letter-spacing:.12em;color:var(--mut);text-transform:uppercase}
#menu .mitem{display:block;width:100%;text-align:left;background:none;border:none;padding:.45rem .3rem;border-bottom:1px solid #f1f5f9;font-size:.92rem}
select,input[type=text],input[type=number]{width:100%;padding:.45rem .55rem;border:1px solid var(--line);border-radius:7px;font:inherit;background:#fff}
label.f{display:block;font-size:.78rem;color:var(--mut);margin:.45rem 0 .2rem}
details.inputs{margin-top:.8rem;border-top:1px solid var(--line);padding-top:.6rem}
@media (max-width:1100px){.hero{grid-template-columns:1fr}.ws-grid{grid-template-columns:1fr;grid-template-rows:auto} #activity{grid-column:1}}
</style>
</head>
<body>

<!-- ==================== LANDING ==================== -->
<div id="landing">
  <div class="topbar">
    <div class="wordmark">GREEN<span>FORGE</span></div>
    <nav>
      <a href="#how">How it works</a><a href="#try">Try the check</a><a href="#fixes">Fixes</a>
      <a href="#codes">Codes</a><a href="#demo">Demo</a><a href="#plainly">Said plainly</a>
    </nav>
    <button class="pill btn-white" onclick="enterWorkspace(true)">▶ Watch GreenForge fix a building</button>
  </div>

  <div class="hero">
    <div>
      <div class="eyebrow">GreenForge · AI green-code coordination</div>
      <h1>Compliance, not just audits.</h1>
      <div class="sub">Every fix verified before you see it.</div>
      <p class="lede">For the sustainability consultant who makes a building pass its green code. GreenForge reads the
      model, checks it against Green Mark, Estidama, Mostadam or Greenship, proposes a real fix for every failing
      rule — and proves each fix with a re-run of the checks and the energy model before anyone sees it.
      Observe. Resolve. Check. Apply.</p>
      <div class="award">Built on the ORCA hackathon codebase (MIT) · The verifier is the product</div>
      <div class="cta-row">
        <button class="pill btn-white" onclick="enterWorkspace(true)">▶ Watch GreenForge fix a building</button>
        <button class="pill btn-ghost" onclick="enterWorkspace(false,true)">Use your own model</button>
        <a href="#demo">Pick a demo ↓</a>
      </div>
      <div class="replay-card" style="margin-top:1.6rem">
        <h4>THE LOOP, RECORDED ON NORTHBANK</h4>
        <div class="replay-line" id="rp0"><b>OBSERVE</b> — BCA Green Mark: 0/8 rules pass · EUI 195.9 · ETTV 51.8</div>
        <div class="replay-line" id="rp1"><b>RESOLVE</b> — 10 candidate fixes proposed, ranked by the model</div>
        <div class="replay-line" id="rp2"><b>CHECK</b> — 9 verified · 1 rejected (roof insulation fixes no failing rule)</div>
        <div class="replay-line" id="rp3"><b>APPLY</b> — 8/8 rules pass · EUI 115.5 · payback 6.2 years</div>
        <div class="replay-ctl">
          <button class="chipbtn" onclick="enterWorkspace(true)">Run it live</button>
          <span class="small" style="color:#7d8fb3">The recorded run, retold live in 3D on the model's own geometry.</span>
        </div>
      </div>
    </div>
    <div class="replay-card" style="min-height:420px">
      <h4>NORTHBANK — ARCHITECTURE MODEL, ASSESSED LIVE</h4>
      <div style="position:relative;height:340px;border-radius:10px;background:linear-gradient(160deg,#0e2233,#0a2e25);overflow:hidden">
        <div style="position:absolute;inset:auto 12% 18% 12%;height:58%;background:linear-gradient(180deg,rgba(125,211,252,.35),rgba(45,212,191,.12));border:1px solid rgba(125,211,252,.5);border-radius:6px"></div>
        <div style="position:absolute;inset:auto 12% 76% 12%;height:6%;background:rgba(148,163,184,.5);border-radius:3px"></div>
        <div class="pin" style="top:16%;left:20%">#1 ETTV 51.8 → ≤50</div>
        <div class="pin" style="top:38%;right:14%">#2 SHGC 0.55 → ≤0.35</div>
        <div class="pin" style="bottom:20%;left:16%">#3 EUI 195.9 → ≤160</div>
        <div style="position:absolute;bottom:6%;left:5%;color:#7d8fb3;font-size:.8rem">1,085 elements · glazing, walls, roof, floors — the systems the verifier checks</div>
      </div>
      <p class="small" style="color:#7d8fb3;margin-top:.7rem">Open the demo to orbit the real model: every check flies the camera to the system it is verifying.</p>
    </div>
  </div>

  <div class="strip"><span>IFC</span><span>GREEN MARK · SG</span><span>ESTIDAMA · UAE</span><span>MOSTADAM · KSA</span><span>GREENSHIP · ID</span><span>EPD DATABASE</span><span>HTML REPORT</span></div>

  <section class="blk">
    <div class="eyebrow">The problem</div>
    <h2>Every design is efficient on its own. Together, they miss the code.</h2>
    <p>Envelope, HVAC and lighting are designed by different teams against different assumptions. The building that
    reaches the assessor fails on the interactions: the glass that overheats the plant, the plant that eats the EUI
    budget, the materials that blow the carbon cap. Findings in a PDF don't fix that. Verified fixes do.</p>
    <div class="stat-row">
      <div class="stat"><b>8 / 8</b><span>RULES FAILING ON NORTHBANK, BEFORE</span></div>
      <div class="stat"><b>195.9</b><span>EUI KWH/M²/YR, BEFORE</span></div>
      <div class="stat"><b>~60 s</b><span>TO A VERIFIED, COSTED FIX BUNDLE</span></div>
    </div>
  </section>

  <section class="blk" id="how">
    <div class="eyebrow">How it works</div>
    <h2>Observe. Resolve. Check. Apply.</h2>
    <div class="loop-tabs">
      <button class="chipbtn on" data-loop="0" onclick="loopTab(0)">OBSERVE</button>
      <button class="chipbtn" data-loop="1" onclick="loopTab(1)">RESOLVE</button>
      <button class="chipbtn" data-loop="2" onclick="loopTab(2)">CHECK</button>
      <button class="chipbtn" data-loop="3" onclick="loopTab(3)">APPLY</button>
    </div>
    <div class="loop-body" id="loopBody"></div>
  </section>

  <section class="blk" id="try">
    <div class="eyebrow">Try the check yourself</div>
    <h2>A fix isn't a fix until the numbers say so.</h2>
    <p>Northbank's envelope fails ETTV (51.8 against a limit of 50). Drag the glazing's solar heat gain down and watch
    the check — a simplified version of the same arithmetic the verifier runs on every proposed fix.</p>
    <div class="trybox">
      <label style="color:#c9d6ee;font-size:.9rem">Glazing SHGC: <b id="shgcVal">0.55</b> (as modelled)</label>
      <input type="range" id="shgc" min="0.20" max="0.60" step="0.01" value="0.55" oninput="tryCheck()">
      <p style="margin:.6rem 0">ETTV estimate: <b id="ettvVal" style="color:#fff">51.8</b> W/m² · limit 50 ·
      <span class="verdict no" id="tryVerdict">NOT VERIFIED</span></p>
      <button class="chipbtn" onclick="showForgeFix()">Show GreenForge's fix</button>
      <span class="small" style="color:#7d8fb3">Low-e glazing (SHGC 0.28) + shading fins + WWR 0.60 — verified as a bundle in the demo.</span>
    </div>
  </section>

  <section class="blk" id="fixes">
    <div class="eyebrow">Real fixes, not notes</div>
    <h2>Ten kinds of fix. One rule: verified, or it doesn't reach you.</h2>
    <div class="grid3">
      <div class="card-d"><h3>Envelope</h3><p>Reduce window-to-wall ratio · double low-e glazing · external shading fins · wall and roof insulation. Checked against ETTV, WWR and SHGC.</p></div>
      <div class="card-d"><h3>Plant &amp; light</h3><p>High-efficiency HVAC (COP and kW/RT) · LED retrofit (W/m²) · rooftop PV (renewable share). Checked against EUI and the plant rules.</p></div>
      <div class="card-d"><h3>Materials</h3><p>Low-carbon concrete (50% GGBS) · recycled EAF steel, from the EPD database. The carbon track: a fix verifies on a ≥1% embodied-carbon cut alone.</p></div>
    </div>
    <p style="margin-top:1rem"><b style="color:#fff">What “verified” means:</b> the fix repairs at least one failing rule
    (or cuts embodied carbon ≥1%) · no rule that passed starts failing · EUI does not get worse · the bundle is
    re-checked end to end before it is costed.</p>
  </section>

  <section class="blk" id="demo">
    <div class="eyebrow">The demo</div>
    <h2>One building, four codes</h2>
    <div class="demo-card">
      <div>
        <h3>Northbank, a riverside office</h3>
        <p>BUILDING · 1 IFC MODEL · 1,085 ELEMENTS. The architecture model from the ORCA baseline set, assessed
        against BCA Green Mark: all 8 rules failing as modelled. Same building, different code, different verified
        bundle — under Greenship the loop finds 5 fixes and takes it 1/5 → 5/5.</p>
        <div class="stat-row">
          <div class="stat"><b>0/8 → 8/8</b><span>GREEN MARK, AFTER THE BUNDLE</span></div>
          <div class="stat"><b>−41%</b><span>EUI 195.9 → 115.5</span></div>
          <div class="stat"><b>6.2 yr</b><span>SIMPLE PAYBACK, COSTED</span></div>
        </div>
        <div class="cta-row">
          <button class="pill btn-white" onclick="enterWorkspace(true)">Open the Northbank demo</button>
          <button class="pill btn-ghost" onclick="enterWorkspace(false)">Look around first</button>
        </div>
      </div>
      <div>
        <h3 style="font-size:1.05rem">On the left — what failed</h3>
        <p>Every rule, its value and its limit, grouped by system.</p>
        <h3 style="font-size:1.05rem;margin-top:.8rem">In the middle — the model in 3D</h3>
        <p>The agent camera flies to each system as it is checked; green is a pass, red is a rejection.</p>
        <h3 style="font-size:1.05rem;margin-top:.8rem">On the right — pick a finding, press Propose fixes</h3>
        <p>Verified fix cards with what each one checked. Accept the ones you keep; export the report.</p>
      </div>
    </div>
  </section>

  <section class="blk" id="codes">
    <div class="eyebrow">Who it's for</div>
    <h2>The people who carry the green submission</h2>
    <div class="grid3">
      <div class="card-d"><h3>Sustainability &amp; MEP consultants</h3><p>A first-pass verified bundle in a minute, with capex, saving and payback — before the detailed simulation starts.</p></div>
      <div class="card-d"><h3>Developers</h3><p>What compliance costs, per code and per country, while the design can still change cheaply. CORENET X makes Green Mark a filing gate in Singapore from Oct 2026 for new projects ≥5,000 m².</p></div>
      <div class="card-d"><h3>Design teams</h3><p>Fixes as model decisions — glazing spec, fins, plant size, material swaps — not redlines on a PDF.</p></div>
    </div>
  </section>

  <section class="blk" id="plainly">
    <div class="eyebrow">Said plainly</div>
    <h2>What it doesn't do yet</h2>
    <p>· The rule sets are simplified extracts for demonstration — verify against the current edition before any real
    submission, and a QP / licensed professional must sign off. · Energy figures come from a simplified-physics
    engine; EnergyPlus is not bundled, and the app says so on every run. · The LLM (your Opencode Go model) only
    ranks candidate fixes — it never invents a number, and if it is unreachable the loop runs in catalogue order and
    says so. · Accepted fixes are kept for the session and the report; GreenForge does not yet write the edits back
    into your IFC file.</p>
    <p style="margin-top:1rem"><b style="color:#fff">Words you'll see:</b> IFC (the open building-model format) ·
    ETTV (envelope thermal transfer, W/m²) · EUI (energy use intensity, kWh/m²/yr) · SHGC (solar heat gain
    coefficient) · WWR (window-to-wall ratio) · EPD (environmental product declaration) · QP (qualified person).</p>
    <p><b style="color:#fff">3D colour key:</b> <span style="color:#38bdf8">■</span> system being checked ·
    <span style="color:#22c55e">■</span> verified · <span style="color:#ef4444">■</span> rejected ·
    <span style="color:#f59e0b">■</span> walls highlighted · dimmed = everything else.</p>
  </section>

  <section class="blk" style="text-align:center">
    <h2>See a verified fix bundle in about a minute.</h2>
    <div class="cta-row" style="justify-content:center">
      <button class="pill btn-white" onclick="enterWorkspace(true)">▶ Watch GreenForge fix a building</button>
      <button class="pill btn-ghost" onclick="enterWorkspace(false,true)">Use your own model</button>
    </div>
  </section>

  <footer>
    <span>GreenForge · Observe. Resolve. Check. Apply.</span>
    <span>Built on the ORCA codebase by IssueLab (MIT) — the green-code verified-fix extension.</span>
    <a href="https://github.com/syaikhipin/greenforge" target="_blank" rel="noopener">Source on GitHub</a>
    <span>Built with IfcOpenShell, three.js and web-ifc.</span>
  </footer>
</div>

<!-- ==================== LOADING ==================== -->
<div id="loading"><div class="box">
  <div class="eyebrow">GreenForge</div>
  <h2 id="loadTitle">Opening the Northbank demo…</h2>
  <div class="loadbar"><i id="loadFill"></i></div>
  <div class="loadstep" id="ls0">Loading the architecture IFC (1.1 MB)</div>
  <div class="loadstep" id="ls1">Parsing 1,085 elements in your browser (web-ifc)</div>
  <div class="loadstep" id="ls2">Assessing against the code — Observe</div>
  <div class="loadstep" id="ls3">Findings ready</div>
</div></div>

<!-- ==================== WORKSPACE ==================== -->
<div id="workspace">
  <div class="ws-top">
    <div class="wordmark">GREEN<span>FORGE</span></div>
    <div class="proj" id="wsTitle">Northbank — Architecture</div>
    <a href="#" id="exportLink" style="display:none">Download report</a>
    <div class="sp"></div>
    <span class="small" style="color:#b8c4da" id="keptCount">0 kept fixes</span>
    <button class="btn-line" style="padding:.35rem .8rem" onclick="toggleMenu(true)">Menu</button>
  </div>
  <div class="ws-grid">
    <!-- LEFT -->
    <div class="panel" style="grid-row:1/3">
      <h3>Compliance</h3>
      <div class="ptabs">
        <button class="ptab on" id="tabFindings" onclick="leftTab('findings')">Findings <span id="findCount"></span></button>
        <button class="ptab" id="tabInsights" onclick="leftTab('insights')">Insights</button>
      </div>
      <div id="paneFindings">
        <label class="f">Green code</label>
        <select id="code_id" onchange="assess()"></select>
        <label class="f">Search findings</label>
        <input type="text" id="findSearch" placeholder="Search rules, systems, values…" oninput="renderFindings()">
        <label class="f">Group by</label>
        <select id="groupBy" onchange="renderFindings()"><option value="system">System</option><option value="none">None — severity order</option></select>
        <div style="border:1px solid var(--line);border-radius:10px;padding:.7rem;margin:.8rem 0">
          <b style="font-size:.8rem;letter-spacing:.08em">ASSESS — STEP 1 OF 4</b>
          <p class="small" style="margin:.4rem 0">1 Assess — read the model against the code.<br>2 Propose — candidate fixes for what failed.<br>3 Check — every fix verified before you see it.<br>4 Apply — keep the bundle, export the report.</p>
          <button class="btn-dark" style="width:100%" onclick="assess()">Assess the building</button>
        </div>
        <div id="findList"></div>
        <details class="inputs">
          <summary style="cursor:pointer;font-weight:600;font-size:.9rem">Building inputs &amp; your own model</summary>
          <div id="inputs"></div>
          <label class="f">Upload IFC</label>
          <input type="file" id="ifc" accept=".ifc">
          <button class="btn-line" style="width:100%;margin-top:.5rem" onclick="runIfc()">Analyse uploaded IFC</button>
          <button class="btn-line" style="width:100%;margin-top:.5rem" onclick="loadDemoModel()">Reload demo IFC in 3D</button>
        </details>
      </div>
      <div id="paneInsights" style="display:none"></div>
    </div>
    <!-- CENTRE -->
    <div class="centre">
      <div id="viewer">
        <div class="vtools">
          <button onclick="gfCueSafe({mode:'overview'})">⌂ Home view</button>
          <button onclick="highlight('window')">Glazing</button>
          <button onclick="highlight('wall')">Walls</button>
          <button onclick="highlight(null)">Reset colours</button>
        </div>
        <div id="vstatus">3D viewer idle — load the demo IFC or upload one.</div>
      </div>
      <div id="compare">
        <div style="display:flex;gap:.5rem;align-items:center">
          <b>Compare before and after</b>
          <span class="sp" style="flex:1"></span>
          <button class="chipbtn" style="background:#eef2f7;color:var(--ink);border-color:var(--line)" onclick="compareMode('before')">Before</button>
          <button class="chipbtn on" id="cmpAfter" style="background:var(--ink);color:#fff;border-color:var(--ink)" onclick="compareMode('after')">After</button>
          <button class="chipbtn" style="background:#eef2f7;color:var(--ink);border-color:var(--line)" onclick="compareMode('both')">Both</button>
        </div>
        <dl id="compareDl"></dl>
      </div>
    </div>
    <!-- RIGHT -->
    <div class="panel" style="grid-row:1/3;border-right:none;border-left:1px solid var(--line)">
      <div class="ptabs">
        <button class="ptab on" id="tabFinding" onclick="rightTab('finding')">Finding</button>
        <button class="ptab" id="tabFixes" onclick="rightTab('fixes')">Fixes <span id="fixCount"></span></button>
        <button class="ptab" id="tabReport" onclick="rightTab('report')">Report</button>
      </div>
      <div id="paneFinding">
        <div id="findingDetail"><p class="small">Pick a finding on the left to see its details, then ask GreenForge for fixes.</p></div>
        <label style="display:flex;gap:.5rem;align-items:center;font-size:.88rem;margin:.8rem 0">
          <input type="checkbox" id="use_llm" checked style="width:auto"> LLM ranking (your Opencode Go model)
        </label>
        <button class="btn-dark" id="proposeBtn" style="width:100%" onclick="propose()">Propose fixes</button>
        <p class="small" style="margin-top:.5rem">The model proposes. The verifier decides. Every fix is re-checked before it reaches this panel.</p>
      </div>
      <div id="paneFixes" style="display:none">
        <p class="small" id="fixStatus">No run yet. Propose fixes from a finding.</p>
        <div id="fixCards"></div>
      </div>
      <div id="paneReport" style="display:none">
        <div id="reportBody"><p class="small">Run the loop to get the costed before/after report.</p></div>
      </div>
    </div>
    <!-- BOTTOM: ACTIVITY -->
    <div id="activity">
      <div class="a-head">
        <b>Activity</b>
        <button class="ptab on" id="tabSteps" onclick="actTab('steps')">Steps</button>
        <button class="ptab" id="tabWork" onclick="actTab('workings')">Workings</button>
        <span style="flex:1"></span>
        <span class="small">Replay</span>
        <button class="chipbtn" style="background:#eef2f7;color:var(--ink);border-color:var(--line)" onclick="replayLast()">↻ Replay</button>
        <button class="chipbtn spd on" data-s="1" onclick="setSpeed(1)">1×</button>
        <button class="chipbtn spd" data-s="2" onclick="setSpeed(2)">2×</button>
        <button class="chipbtn spd" data-s="4" onclick="setSpeed(4)">4×</button>
      </div>
      <div class="a-body">
        <div id="stepsPane"><table class="steps"><thead><tr><th>#</th><th>Step</th><th>Who</th><th>Type</th><th>Duration</th></tr></thead><tbody id="stepsBody"><tr><td colspan="5" class="small">No run yet.</td></tr></tbody></table>
        <p class="small" id="stepsFoot"></p></div>
        <div id="workPane" style="display:none"><div class="feed" id="feed"></div></div>
      </div>
    </div>
  </div>
</div>

<div id="workpill">GreenForge Working… <span style="cursor:pointer" onclick="document.getElementById('workpill').style.display='none'">×</span></div>

<div id="menu">
  <div style="display:flex;align-items:center"><b>Menu</b><span style="flex:1"></span><button class="btn-line" style="padding:.3rem .7rem" onclick="toggleMenu(false)">Close</button></div>
  <h4>Code</h4><div id="menuCodes"></div>
  <h4>Share</h4>
  <button class="mitem" id="menuReport" onclick="downloadReport()">Download submission-ready report (HTML)</button>
  <h4>Session</h4>
  <button class="mitem" onclick="location.reload()">Start fresh</button>
  <button class="mitem" onclick="backHome()">Back to the GreenForge home page</button>
  <h4>About</h4>
  <p class="small">GreenForge — the green-code verified-fix agent. Built on the ORCA codebase by IssueLab (MIT). Observe. Resolve. Check. Apply.</p>
  <p class="small" id="menuSession"></p>
</div>

<script>
const DEMO={gfa_m2:12000,wwr:0.65,glazing_u:2.8,glazing_shgc:0.55,wall_u:1.2,roof_u:0.9,hvac_cop:3.0,hvac_kw_per_rt:1.05,lighting_wpd:15,renewable_share:0.0};
const LABELS={gfa_m2:'Gross floor area (m²)',wwr:'Window-to-wall ratio',glazing_u:'Glazing U (W/m²K)',glazing_shgc:'Glazing SHGC',wall_u:'Wall U (W/m²K)',roof_u:'Roof U (W/m²K)',hvac_cop:'HVAC COP',hvac_kw_per_rt:'HVAC kW/RT',lighting_wpd:'Lighting (W/m²)',renewable_share:'Renewable share'};
const SYS={ettv:['Envelope','glazing'],wwr:['Envelope','glazing'],glazing_shgc:['Envelope','glazing'],wall_u:['Envelope','walls'],roof_u:['Envelope','roof'],hvac_cop:['Plant','roof'],hvac_kw_per_rt:['Plant','roof'],eui:['Energy','floors'],renewable_share:['Renewables','roof'],lighting_wpd:['Lighting','floors']};
const FIXSET={reduce_wwr:'glazing',lowe_glazing:'glazing',shading_fins:'glazing',wall_insulation:'walls',roof_insulation:'roof',hvac_upgrade:'roof',led_lighting:'floors',rooftop_pv:'roof',low_carbon_concrete:'structure',recycled_steel:'members'};
const S={run:null,selected:null,kept:new Set(),speed:1,steps:[],autoPropose:false};
const $=id=>document.getElementById(id);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const fmt=n=>typeof n==='number'?n.toLocaleString():n;

/* ---------- landing ---------- */
const LOOP=[`<b>Observe.</b> GreenForge parses the IFC (in your browser, and server-side with IfcOpenShell), reads the building as assessed — WWR, glazing, envelope, plant, lighting, materials — and checks it against the chosen code. Northbank as modelled: <b>0 of 8 Green Mark rules pass</b>, EUI 195.9, embodied carbon 3,129,108 kgCO₂e. The engine states itself on every run: simplified-physics, EnergyPlus not bundled.`,
`<b>Resolve.</b> A deterministic catalogue proposes candidate fixes for what failed — glazing, shading, insulation, plant, PV, material swaps from the EPD database. Your own LLM (Opencode Go) may <b>re-rank the order</b> the verifier sees them in. It never invents a number, and if it is unreachable the catalogue order is used and the run says so.`,
`<b>Check.</b> Every candidate is re-run through the full rule set and the energy model. A fix verifies only if it <b>repairs a failing rule</b> (or cuts embodied carbon ≥1% on the carbon track), <b>regresses nothing</b>, and <b>does not raise EUI</b>. Roof insulation on Northbank is rejected in the open: it fixes no failing rule.`,
`<b>Apply.</b> The verified fixes — and only those — are combined and re-checked as a bundle, then costed: capex, annual saving, simple payback. <b>0/8 → 8/8, EUI 195.9 → 115.5, payback 6.2 years.</b> Accept the fixes you keep and download the submission-ready report.`];
function loopTab(i){document.querySelectorAll('[data-loop]').forEach(b=>b.classList.toggle('on',+b.dataset.loop===i));$('loopBody').innerHTML=LOOP[i];}
loopTab(0);
function tryCheck(){const shgc=parseFloat($('shgc').value);$('shgcVal').textContent=shgc.toFixed(2);
  const ettv=Math.round(51.8*(0.35+0.65*(shgc/0.55))*10)/10;$('ettvVal').textContent=ettv;
  const ok=ettv<=50;const v=$('tryVerdict');v.className='verdict '+(ok?'ok':'no');v.textContent=ok?'VERIFIED ✓':'NOT VERIFIED';}
function showForgeFix(){$('shgc').value=0.28;tryCheck();}

/* ---------- enter / loading ---------- */
async function enterWorkspace(autoPropose,ownModel){
  $('landing').style.display='none';$('loading').style.display='grid';
  const steps=[0,1,2,3];
  for(let i=0;i<steps.length;i++){$('loadFill').style.width=((i+1)*25)+'%';await sleep(420);$('ls'+i).classList.add('done');}
  $('workspace').style.display='flex';$('loading').style.display='none';
  buildInputs();await loadCodes();loadDemoModel();await assess();
  if(ownModel){document.querySelector('details.inputs').open=true;$('ifc').click();}
  if(autoPropose){propose();}
}
function backHome(){$('workspace').style.display='none';$('landing').style.display='block';toggleMenu(false);}
function toggleMenu(open){$('menu').style.display=open?'block':'none';}

/* ---------- setup ---------- */
function buildInputs(){const w=$('inputs');w.innerHTML='';
  for(const k in DEMO){const l=document.createElement('label');l.className='f';l.textContent=LABELS[k];
    const i=document.createElement('input');i.type='number';i.step='any';i.id='in_'+k;i.value=DEMO[k];l.appendChild(i);w.appendChild(l);}}
function building(){const b={};for(const k in DEMO)b[k]=parseFloat($('in_'+k).value);return b;}
async function loadCodes(){const d=await (await fetch('/api/codes')).json();const s=$('code_id');s.innerHTML='';
  const mc=$('menuCodes');mc.innerHTML='';
  d.codes.forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.name+' ('+c.country+')';s.appendChild(o);
    const b=document.createElement('button');b.className='mitem';b.textContent=c.name+' — '+c.country;b.onclick=()=>{s.value=c.id;assess();};mc.appendChild(b);});}

/* ---------- assess / findings ---------- */
async function assess(){
  const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({building:building(),code_id:$('code_id').value,use_llm:false})});
  S.run=await r.json();S.kept.clear();updateKept();
  const fails=S.run.observe.checks.filter(c=>!c.pass);
  S.selected=fails.length?fails[0].key:S.run.observe.checks[0].key;
  $('findCount').textContent=S.run.observe.checks.length;
  $('wsTitle').textContent='Northbank — Architecture · '+S.run.code.name;
  $('menuSession').textContent='Session: '+S.run.compliance_before.pass+'/'+S.run.compliance_before.total+' rules passing · '+S.kept.size+' kept fixes';
  renderFindings();renderInsights();renderFindingDetail();renderReport();
}
function sysOf(key){return SYS[key]||['Other','glazing'];}
function renderFindings(){
  const q=($('findSearch').value||'').toLowerCase();const grp=$('groupBy').value;
  let checks=[...S.run.observe.checks].sort((a,b)=>(a.pass-b.pass));
  if(q)checks=checks.filter(c=>(c.label+c.key+sysOf(c.key)[0]).toLowerCase().includes(q));
  const w=$('findList');w.innerHTML='';
  let lastGroup=null;
  checks.forEach((c,i)=>{
    const g=sysOf(c.key)[0];
    if(grp==='system'&&g!==lastGroup){lastGroup=g;const h=document.createElement('div');h.className='small';h.style='font-weight:700;margin:.5rem 0 .3rem';h.textContent=g.toUpperCase();w.appendChild(h);}
    const d=document.createElement('div');d.className='finding'+(c.pass?' pass':'')+(S.selected===c.key?' sel':'');
    d.innerHTML='<b></b> <span class="tag '+(c.pass?'ok':'')+'">'+(c.pass?'PASS':'FAIL')+'</span> <span class="tag sys"></span><div class="meta"></div>';
    d.querySelector('b').textContent='#'+(i+1)+' '+c.label;
    d.querySelector('.sys').textContent=g;
    d.querySelector('.meta').textContent='Value '+c.value+' · limit '+c.limit+' '+c.unit;
    d.onclick=()=>{S.selected=c.key;renderFindings();renderFindingDetail();
      if(window.gfCue)gfCue({mode:'focus',set:sysOf(c.key)[1],color:c.pass?0x22c55e:0xef4444,label:'finding: '+c.label+' — '+(c.pass?'passing.':'failing at '+c.value+' against a limit of '+c.limit+' '+c.unit+'.')});};
    w.appendChild(d);
  });
}
function renderInsights(){
  const fails=S.run.observe.checks.filter(c=>!c.pass);const bySys={};
  fails.forEach(c=>{const g=sysOf(c.key)[0];(bySys[g]=bySys[g]||[]).push(c);});
  const w=$('paneInsights');w.innerHTML='<p class="small">Same system, many failures. GreenForge groups findings that one plan can fix together.</p>';
  Object.entries(bySys).forEach(([g,cs])=>{
    const d=document.createElement('div');d.className='fixcard';
    d.innerHTML='<h4>'+cs.length+' findings, 1 system — '+g+'</h4><p class="small">'+cs.map(c=>c.label).join(' · ')+'. One verified bundle usually carries all of them.</p>';
    w.appendChild(d);});
  const carb=document.createElement('div');carb.className='fixcard';
  carb.innerHTML='<h4>Carbon track</h4><p class="small">Embodied carbon '+fmt(S.run.observe.carbon.total_kgco2e)+' kgCO₂e. Material swaps verify on a ≥1% cut even when no rule is failing.</p>';
  w.appendChild(carb);
}
function renderFindingDetail(){
  const c=S.run.observe.checks.find(x=>x.key===S.selected);if(!c)return;
  const after=S.run.after.checks.find(x=>x.key===c.key);
  $('findingDetail').innerHTML='<h3 style="margin-bottom:.3rem">'+c.label+'</h3>'+
    '<p><span class="tag '+(c.pass?'ok':'')+'">'+(c.pass?'PASSING':'FAILING')+'</span> <span class="tag sys">'+sysOf(c.key)[0]+'</span></p>'+
    '<p class="small">Value <b>'+c.value+'</b> against a limit of <b>'+c.limit+' '+c.unit+'</b> under '+S.run.code.name+'.</p>'+
    '<p class="small"><b>What needs to change:</b> '+(c.pass?'Nothing — this rule already passes. Keep it that way: the verifier rejects any fix that regresses it.':'A verified fix must move this value inside the limit without breaking any other rule or raising EUI. Propose fixes and watch each candidate get checked.')+'</p>'+
    (after?'<p class="small">After the last verified bundle: <b>'+after.value+' '+after.unit+'</b> — '+(after.pass?'PASS':'still failing')+'.</p>':'');
}
function leftTab(t){$('paneFindings').style.display=t==='findings'?'block':'none';$('paneInsights').style.display=t==='insights'?'block':'none';
  $('tabFindings').classList.toggle('on',t==='findings');$('tabInsights').classList.toggle('on',t==='insights');}
function rightTab(t){['finding','fixes','report'].forEach(x=>{$('pane'+x[0].toUpperCase()+x.slice(1)).style.display=x===t?'block':'none';$('tab'+x[0].toUpperCase()+x.slice(1)).classList.toggle('on',x===t);});}
function actTab(t){$('stepsPane').style.display=t==='steps'?'block':'none';$('workPane').style.display=t==='workings'?'block':'none';
  $('tabSteps').classList.toggle('on',t==='steps');$('tabWork').classList.toggle('on',t==='workings');}
function gfCueSafe(cue){if(window.gfCue)gfCue(cue);}

/* ---------- the loop ---------- */
function say(lane,text){const f=$('feed');const d=document.createElement('div');d.className='msg';
  d.innerHTML='<div class="lane '+lane+'">'+lane.toUpperCase()+'</div><p style="margin:0"></p>';
  d.querySelector('p').textContent=text;f.appendChild(d);f.scrollTop=f.scrollHeight;}
function stepRow(n,name,who,type,t0){const tb=$('stepsBody');
  if(n===1)tb.innerHTML='';
  const tr=document.createElement('tr');tr.innerHTML='<td>'+n+'</td><td></td><td>'+who+'</td><td>'+type+'</td><td>'+((performance.now()-t0)/1000).toFixed(1)+' s</td>';
  tr.children[1].textContent=name;tb.appendChild(tr);}
async function propose(){
  rightTab('fixes');actTab('workings');
  $('proposeBtn').disabled=true;$('proposeBtn').textContent='Working…';$('workpill').style.display='block';
  if(!window.__gfModel){say('agent','Loading the building in 3D so you can watch each check on the model…');await loadDemoModel();}
  const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({building:building(),code_id:$('code_id').value,use_llm:$('use_llm').checked})});
  const run=await r.json();S.run=run;S.kept.clear();updateKept();
  await replay(run);
  $('proposeBtn').disabled=false;$('proposeBtn').textContent='Propose fixes';$('workpill').style.display='none';
  renderFindings();renderFindingDetail();renderReport();
  $('exportLink').style.display='inline';$('exportLink').href='/api/report/'+run.run_id;
}
async function replay(run){
  $('feed').innerHTML='';$('fixCards').innerHTML='';$('stepsBody').innerHTML='';$('stepsFoot').textContent='';
  $('fixStatus').textContent='Running the loop against '+run.code.name+'…';
  const t0=performance.now();let n=0;
  say('you','Propose fixes for '+run.code.name+'.');await sleep(300/S.speed);
  for(const w of run.workings){
    n++;
    if(w.startsWith('OBSERVE')){say('agent',w);stepRow(n,'Observe — read the building against the code','GreenForge','tool',t0);
      gfCueSafe({mode:'overview'});await sleep(800/S.speed);}
    else if(w.startsWith('RESOLVE')){say('agent',w);stepRow(n,'Resolve — candidate fixes proposed'+(run.llm_used?' (LLM-ranked)':''),run.llm_used?'Your LLM':'GreenForge',run.llm_used?'model':'tool',t0);
      run.candidates.forEach(c=>{const el=document.createElement('div');el.className='fixcard';el.id='card-'+c.id;
        el.innerHTML='<h4></h4><p class="small"></p><span class="badge wait">AWAITING VERIFICATION</span>';
        el.querySelector('h4').textContent=c.title;
        el.querySelector('p').textContent=c.description+' Capex: '+fmt(c.capex)+' '+run.code.currency+'.';
        $('fixCards').appendChild(el);});
      $('fixCount').textContent=run.candidates.length;await sleep(800/S.speed);}
    else if(w.startsWith('CHECK')){say('verifier',w);
      const id=w.split(' ')[1].replace(':','');const v=run.verdicts.find(x=>x.id===id);
      stepRow(n,'Check — '+(v?v.title:id),'Verifier','tool',t0);
      const el=$('card-'+id);
      if(el&&v){const b=el.querySelector('.badge');b.className='badge '+(v.verified?'ok':'no');b.textContent=v.verified?'✓ VERIFIED':'✗ REJECTED';
        const p=document.createElement('p');p.className='small';p.textContent='What it checked: '+v.reason+'.';el.appendChild(p);
        if(v.verified){const acts=document.createElement('div');acts.className='acts';
          const acc=document.createElement('button');acc.className='btn-dark';acc.textContent='Accept';
          const rej=document.createElement('button');rej.className='btn-line';rej.textContent='Reject';
          acc.onclick=()=>{S.kept.add(id);updateKept();acc.disabled=true;acc.textContent='Kept ✓';rej.disabled=true;};
          rej.onclick=()=>{S.kept.delete(id);updateKept();el.style.opacity=.5;acc.disabled=true;rej.textContent='Rejected';};
          acts.appendChild(acc);acts.appendChild(rej);el.appendChild(acts);}}
      if(v)gfCueSafe({mode:'focus',set:FIXSET[id]||'glazing',color:v.verified?0x22c55e:0xef4444,label:'verifying '+(v.title)+' — '+(v.verified?'VERIFIED, glowing green.':'REJECTED, glowing red.')});
      await sleep(2600/S.speed);}
    else if(w.startsWith('APPLY')){say('apply',w);stepRow(n,'Apply — verified bundle, costed','GreenForge','tool',t0);
      gfCueSafe({mode:'apply'});
      $('fixStatus').innerHTML='<b>'+run.verified_fixes.length+' verified fixes</b> — GreenForge is holding the bundle until you decide. Accept the fixes you keep; the report carries the whole verified bundle.';
      showCompare(run);await sleep(900/S.speed);}
  }
  const secs=((performance.now()-t0)/1000).toFixed(1);
  $('stepsFoot').textContent=n+' steps · '+(run.llm_used?'1 model call (ranking)':'0 model calls')+' · '+secs+' s replay. The run has finished. It is not kept yet: Accept fixes to keep them.';
  actTab('steps');
}
function showCompare(run){
  $('compare').style.display='block';
  $('compareDl').innerHTML=
    '<div><dt>Compliance</dt><dd>'+run.compliance_before.pass+' → '+run.compliance_after.pass+' of '+run.compliance_after.total+' rules</dd></div>'+
    '<div><dt>EUI</dt><dd>'+run.observe.simulation.eui_kwh_m2_yr+' → '+run.after.simulation.eui_kwh_m2_yr+' kWh/m²/yr</dd></div>'+
    '<div><dt>Embodied carbon</dt><dd>'+fmt(run.observe.carbon.total_kgco2e)+' → '+fmt(run.after.carbon.total_kgco2e)+' kgCO₂e</dd></div>'+
    '<div><dt>Payback</dt><dd>'+run.costs.simple_payback_years+' yr · capex '+fmt(run.costs.capex_total)+' '+run.costs.currency+'</dd></div>';
}
function compareMode(m){if(m==='before'){gfCueSafe({mode:'overview'});if(window.highlight)highlight(null);}
  else gfCueSafe({mode:'apply'});}
function setSpeed(s){S.speed=s;document.querySelectorAll('.spd').forEach(b=>b.classList.toggle('on',+b.dataset.s===s));}
function replayLast(){if(S.run)replay(S.run);}
function updateKept(){$('keptCount').textContent=S.kept.size+' kept fixes';const sf=$('stepsFoot');if(sf&&sf.textContent.includes('steps ·')){const base=sf.textContent.split(' · ')[0]+' · run finished.';sf.textContent=base+' '+S.kept.size+' fix(es) kept so far.';}
  if(S.run)$('menuSession').textContent='Session: '+S.run.compliance_before.pass+'/'+S.run.compliance_before.total+' rules passing · '+S.kept.size+' kept fixes';}
function renderReport(){
  if(!S.run)return;const run=S.run;
  const rows=run.after.checks.map((c,i)=>{const b=run.observe.checks[i];
    return '<tr><td>'+c.label+'</td><td>'+b.value+'</td><td>'+c.value+'</td><td>'+c.limit+' '+c.unit+'</td><td>'+(c.pass?'PASS':'FAIL')+'</td></tr>';}).join('');
  $('reportBody').innerHTML='<p><b>'+run.code.name+'</b> — compliance '+run.compliance_before.pass+'/'+run.compliance_before.total+' → <b>'+run.compliance_after.pass+'/'+run.compliance_after.total+'</b></p>'+
    '<p class="small">Capex <b>'+fmt(run.costs.capex_total)+' '+run.costs.currency+'</b> · annual saving <b>'+fmt(run.costs.annual_saving)+' '+run.costs.currency+'</b> · payback <b>'+run.costs.simple_payback_years+' years</b> · LLM ranked: '+run.llm_used+'</p>'+
    '<table class="steps"><tr><th>Rule</th><th>Before</th><th>After</th><th>Limit</th><th>Status</th></tr>'+rows+'</table>'+
    '<p style="margin-top:.7rem"><a href="/api/report/'+run.run_id+'"><button class="btn-dark">Download submission-ready report (HTML)</button></a></p>'+
    '<p class="small">'+run.disclaimer+'</p>';
}
function downloadReport(){if(S.run)window.open('/api/report/'+S.run.run_id,'_blank');}

/* ---------- IFC ---------- */
async function loadDemoModel(){
  const st=$('vstatus');st.textContent='Loading demo IFC…';
  try{
    const resp=await fetch('/demo.ifc');if(!resp.ok)throw new Error('HTTP '+resp.status);
    const buf=await resp.arrayBuffer();
    for(let i=0;i<40&&typeof loadIfcBuffer!=='function';i++){st.textContent='Waiting for the 3D module (three.js + web-ifc from CDN)…';await sleep(500);}
    if(typeof loadIfcBuffer!=='function')throw new Error('3D module did not arrive — check the connection and reload the page');
    await loadIfcBuffer(buf,'Northbank-Architecture.ifc');
  }catch(e){st.textContent='Demo IFC failed to load: '+e.message;}
}
async function runIfc(){
  const f=$('ifc').files[0];if(!f){alert('Choose an .ifc file first');return;}
  loadIfcBuffer(await f.arrayBuffer(),f.name);
  const fd=new FormData();fd.append('file',f);fd.append('code_id',$('code_id').value);
  const r=await fetch('/api/ifc',{method:'POST',body:fd});const d=await r.json();
  S.run=d.run;S.selected=(d.run.observe.checks.find(c=>!c.pass)||d.run.observe.checks[0]).key;
  $('wsTitle').textContent=f.name+' · '+d.run.code.name;
  say('agent','IFC ingest: '+d.ifc.note);
  renderFindings();renderInsights();renderFindingDetail();renderReport();
}
</script>
<script type="importmap">
{"imports":{"three":"/static/three.module.js","three/addons/":"/static/addons/","web-ifc":"/static/web-ifc-api.js"}}
</script>
<script type="module">
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {IfcAPI, IFCWINDOW, IFCPLATE, IFCWALL, IFCWALLSTANDARDCASE, IFCROOF, IFCCOVERING, IFCSLAB, IFCMEMBER} from 'web-ifc';
const box=document.getElementById('viewer'),status=document.getElementById('vstatus');
const setStatus=t=>{status.textContent=t;};
window.addEventListener('error',e=>{if(!window.__gfModel) setStatus('3D viewer error: '+(e.message||'script failed to load')+'. The analysis still runs server-side.');});
const scene=new THREE.Scene();scene.background=new THREE.Color(0x0b1a10);
const camera=new THREE.PerspectiveCamera(45,box.clientWidth/box.clientHeight,0.1,5000);camera.position.set(28,22,28);
const renderer=new THREE.WebGLRenderer({antialias:true});renderer.setSize(box.clientWidth,box.clientHeight);box.appendChild(renderer.domElement);
const controls=new OrbitControls(camera,renderer.domElement);
let camAnim=null,pulseSet=[];
scene.add(new THREE.AmbientLight(0xffffff,0.8));
const sun=new THREE.DirectionalLight(0xffffff,1.1);sun.position.set(30,50,20);scene.add(sun);
scene.add(new THREE.GridHelper(200,40,0x2f5d3a,0x1d3a24));
function animate(){requestAnimationFrame(animate);
  if(camAnim){const k=Math.min((performance.now()-camAnim.start)/camAnim.dur,1);const e=k<0.5?4*k*k*k:1-Math.pow(-2*k+2,3)/2;
    camera.position.lerpVectors(camAnim.p0,camAnim.p1,e);controls.target.lerpVectors(camAnim.t0,camAnim.t1,e);
    if(k>=1)camAnim=null;}
  if(pulseSet.length){const v=0.45+0.3*Math.sin(performance.now()*0.006);pulseSet.forEach(m=>{m.material.emissiveIntensity=v;});}
  controls.update();renderer.render(scene,camera);}
animate();
const ifcAPI=new IfcAPI();
ifcAPI.SetWasmPath('/static/');
let apiReady=null;
function initApi(){if(!apiReady){apiReady=ifcAPI.Init().then(()=>true).catch(e=>{setStatus('3D viewer: web-ifc failed to initialise ('+e.message+').');throw e;});}return apiReady;}
let modelGroup=null;const byType={window:[],wall:[],roof:[],slab:[],member:[]};
function gfFit(){const w=box.clientWidth||800,h=box.clientHeight||450;camera.aspect=w/h;camera.updateProjectionMatrix();renderer.setSize(w,h);}
window.loadIfcBuffer=async function(buf,name){gfFit();
  try{
    setStatus('Initialising web-ifc…');await initApi();
    setStatus('Parsing '+name+' in your browser…');
    if(modelGroup){scene.remove(modelGroup);modelGroup=null;byType.window=[];byType.wall=[];byType.roof=[];byType.slab=[];byType.member=[];}
    const modelID=ifcAPI.OpenModel(new Uint8Array(buf));
    modelGroup=new THREE.Group();let meshCount=0;
    const types={};
    ifcAPI.StreamAllMeshes(modelID,(flatMesh)=>{
      const typeCode=ifcAPI.GetLineType(modelID,flatMesh.expressID);
      const geos=flatMesh.geometries;
      for(let gi=0;gi<geos.size();gi++){
        const placed=geos.get(gi);
        const geom=ifcAPI.GetGeometry(modelID,placed.geometryExpressID);
        const verts=ifcAPI.GetVertexArray(geom.GetVertexData(),geom.GetVertexDataSize());
        const indices=ifcAPI.GetIndexArray(geom.GetIndexData(),geom.GetIndexDataSize());
        const n=verts.length/6;const pos=new Float32Array(n*3);const nor=new Float32Array(n*3);
        for(let i=0;i<n;i++){pos.set(verts.subarray(i*6,i*6+3),i*3);nor.set(verts.subarray(i*6+3,i*6+6),i*3);}
        const g=new THREE.BufferGeometry();
        g.setAttribute('position',new THREE.BufferAttribute(pos,3));
        g.setAttribute('normal',new THREE.BufferAttribute(nor,3));
        g.setIndex(new THREE.BufferAttribute(indices,1));
        const c=placed.color;
        const mat=new THREE.MeshLambertMaterial({color:new THREE.Color(c.x,c.y,c.z),transparent:c.w<1,opacity:c.w});
        const mesh=new THREE.Mesh(g,mat);
        mesh.matrixAutoUpdate=false;mesh.matrix.fromArray(placed.flatTransformation);
        mesh.userData.baseColor=mat.color.getHex();mesh.userData.baseOpacity=c.w;mesh.userData.typeCode=typeCode;
        modelGroup.add(mesh);meshCount++;
        if(typeCode===IFCWINDOW||typeCode===IFCPLATE)byType.window.push(mesh);
        if(typeCode===IFCWALL||typeCode===IFCWALLSTANDARDCASE)byType.wall.push(mesh);
        if(typeCode===IFCROOF||typeCode===IFCCOVERING)byType.roof.push(mesh);
        if(typeCode===IFCSLAB)byType.slab.push(mesh);
        if(typeCode===IFCMEMBER)byType.member.push(mesh);
        geom.delete();
      }
    });
    scene.add(modelGroup);window.__gfModel=true;
    const bb=new THREE.Box3().setFromObject(modelGroup);const ctr=bb.getCenter(new THREE.Vector3());const size=bb.getSize(new THREE.Vector3()).length();
    controls.target.copy(ctr);camera.position.set(ctr.x+size*0.6,ctr.y+size*0.45,ctr.z+size*0.6);
    setStatus(name+' loaded: '+meshCount+' meshes ('+byType.window.length+' glazing, '+byType.wall.length+' wall pieces). Drag to orbit, scroll to zoom.');
  }catch(e){setStatus('3D viewer could not parse this IFC ('+e.message+'). The server-side analysis still runs.');}
};

// ---- Agent camera: the agent's workings, staged in 3D ----
function flyTo(pos,tgt,dur){
  camAnim={p0:camera.position.clone(),p1:pos,t0:controls.target.clone(),t1:tgt,start:performance.now(),dur:dur||1400};
}
const _bb=new THREE.Box3(),_v=new THREE.Vector3();
function frameMeshes(meshes,distFactor,fixedDir){
  if(!meshes.length)return;
  _bb.makeEmpty();meshes.forEach(m=>_bb.expandByObject(m));
  const ctr=_bb.getCenter(new THREE.Vector3());const size=Math.max(_bb.getSize(new THREE.Vector3()).length(),4);
  let dir=camera.position.clone().sub(controls.target).normalize();
  if(fixedDir||dir.lengthSq()<0.1||Math.abs(dir.y)>0.75)dir=new THREE.Vector3(1,0.55,1).normalize();
  const dist=size*(distFactor||0.9);
  flyTo(ctr.clone().add(dir.multiplyScalar(dist)).add(new THREE.Vector3(0,size*0.18,0)),ctr,1300);
}
function resetVisuals(){
  pulseSet=[];
  if(!modelGroup)return;
  modelGroup.traverse(o=>{if(o.isMesh){o.material.color.setHex(o.userData.baseColor);o.material.emissive.setHex(0x000000);o.material.opacity=o.userData.baseOpacity;o.material.transparent=o.userData.baseOpacity<1;}});
}
function paintSet(list,color,dimRest){
  if(!modelGroup)return;
  if(dimRest)modelGroup.traverse(o=>{if(o.isMesh&&!list.includes(o)){o.material.transparent=true;o.material.opacity=0.13;o.material.emissive.setHex(0x000000);}});
  list.forEach(m=>{m.material.color.setHex(color);m.material.emissive.setHex(color);m.material.emissiveIntensity=0.55;m.material.transparent=false;m.material.opacity=1;});
  pulseSet=list;
}
const SETS=()=>({glazing:byType.window,walls:byType.wall,roof:byType.roof,floors:byType.slab,structure:byType.wall.concat(byType.slab),members:byType.member});
window.gfCue=function(cue){gfFit();
  if(!modelGroup||!cue)return;
  if(cue.mode==='overview'){resetVisuals();frameMeshes([modelGroup],1.1,true);setStatus('Agent camera: full model overview while the agent assesses the building.');return;}
  if(cue.mode==='apply'){
    resetVisuals();
    const all=byType.window.concat(byType.wall,byType.roof,byType.slab);
    paintSet(all,0x22c55e,false);frameMeshes([modelGroup],1.15,true);
    setStatus('Agent camera: the verified bundle applied — glazing, walls and roof glow green.');
    return;
  }
  if(cue.mode==='focus'){
    const list=(SETS()[cue.set]||[]);
    if(!list.length){setStatus('Agent camera: no '+cue.set+' elements in this model to stage.');return;}
    resetVisuals();
    paintSet(list,cue.color, true);
    frameMeshes(list,1.0);
    setStatus('Agent camera: '+cue.label);
  }
};
// pulse the focused set + step the camera animation inside the render loop
window.highlight=function(kind){gfFit();
  if(!modelGroup){setStatus('Load an IFC first.');return;}
  modelGroup.traverse(o=>{if(o.isMesh){o.material.color.setHex(o.userData.baseColor);o.material.emissive.setHex(0x000000);o.material.opacity=o.userData.baseOpacity;o.material.transparent=o.userData.baseOpacity<1;}});
  if(!kind){setStatus('Colours reset.');return;}
  const list=byType[kind]||[];
  if(!list.length){setStatus('No '+(kind==='window'?'glazing':'wall')+' elements in this model.');return;}
  const hot=kind==='window'?0x38bdf8:0xf59e0b;
  modelGroup.traverse(o=>{if(o.isMesh&&!list.includes(o)){o.material.transparent=true;o.material.opacity=0.13;}});
  list.forEach(m=>{m.material.color.setHex(hot);m.material.emissive.setHex(hot);m.material.emissiveIntensity=0.55;m.material.transparent=false;m.material.opacity=1;});
  setStatus('Highlighted '+list.length+' '+(kind==='window'?'glazing':'wall')+' elements (rest of model dimmed) — the systems the verifier checks first.');
};
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
# FastAPI routes
# --------------------------------------------------------------------------- #
app = FastAPI(title="GreenForge", description="Green-code verified-fix agent")

# Self-hosted 3D libraries (three.js + web-ifc): the viewer must not depend
# on a CDN being reachable/fast from the visitor's network.
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


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
