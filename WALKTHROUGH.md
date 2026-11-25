# GreenForge — Live Demonstration Walkthrough

Everything below was executed and observed on the production deployment at
**https://greenforge.arifin.uk** on 22 Nov 2026 (CET), final build commit
`0fa1304` (plus an LLM retry in the following commit). Status lines and figures are quoted as observed. Nothing here is
a mock-up: where a step failed during the session, the failure and its fix
are recorded in §8.

GreenForge is a green-code verified-fix agent: it ingests a building model,
checks it against a green building code, proposes fixes, and **verifies
every fix before a human sees it** — a candidate is only applied if it
fixes at least one failing rule (compliance track) or cuts embodied carbon
by at least 1% (carbon track), with no rule regression and no EUI increase.
An LLM may re-rank the candidate order; it never invents numbers.

---

## 0. Setup state (verified)

- Health endpoint: `GET /api/health` →

```json
{"ok": true,
 "engine": "simplified-physics (EnergyPlus not installed — install EnergyPlus and set ENERGYPLUS_IDF_TEMPLATE to run full IDF)",
 "llm_configured": true,
 "codes": ["green_mark_sg", "estidama_uae", "mostadam_ksa", "greenship_id"]}
```

- The engine label is honest: EnergyPlus is not bundled, so runs use the
  built-in simplified-physics model and say so in every output.
- LLM ranking runs through the user's own Opencode Go subscription
  (OpenAI-compatible endpoint, model `glm-5.3`), configured server-side.
  No key is stored in the repo or shown in the UI.

## 1. Load the demo building in 3D

1. Open https://greenforge.arifin.uk/
2. Click **Load demo IFC in 3D**.

Observed status line:

> Northbank-Architecture.ifc loaded: 1085 meshes (173 glazing, 47 wall pieces). Drag to orbit, scroll to zoom.

The bundled IFC (`Northbank-Architecture.ifc`, ~1.1 MB) is parsed **in the
browser** by web-ifc and rendered with three.js: a multi-storey glass
building with grey roof, dark frames and visible interior floors.
Dragging orbits the model; scroll zooms.

Element census of the demo model (counted with web-ifc): 498 furniture,
279 members, 161 plates (the curtain-wall glass), 47 walls, 46 doors,
16 coverings, 12 windows, 12 railings, 6 stair flights, 4 slabs, 3 roofs,
1 transport element — 1,085 meshes in total.

## 2. Highlight the systems the verifier checks

- **Highlight glazing** →

  > Highlighted 173 glazing elements (rest of model dimmed) — the systems the verifier checks first.

  The entire glass facade (12 IFCWINDOW + 161 IFCPLATE curtain-wall panels)
  glows vivid blue; the rest of the model dims to pale grey.

- **Highlight walls** →

  > Highlighted 47 wall elements (rest of model dimmed) — the systems the verifier checks first.

  Walls glow amber against the dimmed model.

- **Reset colours** → `Colours reset.` — original IFC colours restored.

Uploading your own `.ifc` via **Upload IFC** renders it the same way and
also analyses it server-side (IfcOpenShell): WWR, areas and quantities are
read from the model where present, with an honest fallback notice when a
file cannot be parsed.

## 3. Run the verified-fix loop — BCA Green Mark (Singapore)

Defaults: GFA 12,000 m², WWR 0.65, glazing U 2.8 / SHGC 0.55, wall U 1.2,
roof U 0.9, HVAC COP 3.0 at 1.05 kW/RT, lighting 15 W/m², renewables 0.
**Use LLM ranking** ticked. Click **Run verified-fix loop**. The agent
console replays the run through its four stages — Assess → Propose →
Verify → Apply — and the Workings feed shows, verbatim:

> **OBSERVE:** BCA Green Mark — 0/8 rules pass; EUI 195.9 kWh/m2/yr; ETTV 51.8 W/m2; embodied carbon 3,129,108 kgCO2e. Engine: simplified-physics (EnergyPlus not installed …).

> **RESOLVE:** 10 candidate fixes proposed (LLM-ranked (provider configured)).

The LLM re-ordered the catalogue — the verifier's checks ran in the
LLM's order, not the built-in one (HVAC first):

| Check | Verdict | Why |
|---|---|---|
| hvac_upgrade | ✅ VERIFIED | fixes hvac_cop, hvac_kw_per_rt, eui; EUI −25.9 |
| lowe_glazing | ✅ VERIFIED | fixes ettv, glazing_shgc, eui; EUI −27.8 |
| reduce_wwr | ✅ VERIFIED | fixes ettv, wwr; EUI −3.8 |
| led_lighting | ✅ VERIFIED | fixes eui, lighting_wpd; EUI −24.0 |
| shading_fins | ✅ VERIFIED | fixes ettv; EUI −11.5 |
| rooftop_pv | ✅ VERIFIED | fixes renewable_share; EUI −11.7 |
| wall_insulation | ✅ VERIFIED | fixes ettv; EUI −2.4 |
| roof_insulation | ❌ REJECTED | fixes no failing rule; EUI −2.7 |
| low_carbon_concrete | ✅ VERIFIED | carbon track: −607,500 kgCO2e (19.4% cut) |
| recycled_steel | ✅ VERIFIED | carbon track: −868,000 kgCO2e (27.7% cut) |

Rejected candidates are not hidden — the roof-insulation card stays in the
UI with its red REJECTED badge and the reason it failed verification.

> **APPLY:** bundle of 9 verified fixes — compliance 0/8 -> 8/8; EUI 195.9 -> 115.5 kWh/m2/yr; capex 1,673,644.44 SGD; annual saving 270,064.72 SGD; simple payback 6.2 yr.

## 3b. Watch the workings in 3D — the agent camera

The checklist is not the only view. Clicking **Run verified-fix loop**
auto-loads the building in 3D (no separate demo click), and the agent's
work is staged on the model itself by an animated camera:

- **OBSERVE** — the camera flies to a ¾ overview of the whole building.
  Status: *Agent camera: full model overview while the agent assesses the building.*
- **Each CHECK** — the camera flies to the system under test and pulses it:
  the glazing facade for WWR / low-e glazing / shading fins, the walls for
  insulation, the roof plate for HVAC / PV / roof insulation, the floor
  slabs for LED lighting, the structure for the concrete swap, the frame
  members for the steel swap. **Green pulse = VERIFIED, red pulse =
  REJECTED**, with the rest of the model dimmed. Status, e.g.:
  *Agent camera: verifying Roof insulation upgrade — REJECTED, glowing red.*
  (Observed live: the roof element pulsed saturated red for that check,
  and the facade pulsed green for shading fins.)
- **APPLY** — the camera pulls back to the ¾ overview and the whole
  verified bundle glows green: facade, walls, roof plate. Status:
  *Agent camera: the verified bundle applied — glazing, walls and roof glow green.*

Each check dwells ~2.6 s so the flight and the verdict pulse are watchable;
the full staged replay takes about a minute. You can still orbit freely
with the mouse at any point — the camera resumes staging at the next step.

Element staging follows the IFC types as modelled: glazing = IFCWINDOW +
IFCPLATE curtain-wall panels (173), walls = IFCWALL (47), roof = IFCROOF +
IFCCOVERING (19 — in this model the visible roof plate is a covering, and
IFCROOF is only a small entrance canopy).

## 4. Read the Before / After panel

| Metric | Before | After |
|---|---|---|
| Compliance (rules passing) | 0 of 8 | **8 of 8** |
| EUI | 195.9 | **115.5 kWh/m²/yr** |
| Embodied carbon | 3,129,108 | **1,711,223 kgCO₂e** (−45%) |
| Annual energy cost | SGD 658,277.20 | **SGD 388,212.48** |
| Capex | — | SGD 1,673,644.44 |
| Annual saving | — | SGD 270,064.72 |
| Simple payback | — | **6.2 years** |
| LLM ranked | — | true |

Per-rule table (all PASS after): ETTV 51.8 → 24.1 W/m² · WWR 0.65 → 0.60 ·
SHGC 0.55 → 0.28 · HVAC COP 3.0 → 4.2 · HVAC 1.05 → 0.68 kW/RT · EUI
195.9 → 115.5 · Renewable share 0 → 0.06 · Lighting 15 → 7 W/m².

## 5. Download the submission-ready report

**Download submission-ready report (HTML)** opens
`/api/report/<run_id>` — a standalone report with the code name, the
before/after compliance and EUI, the costed summary (capex, annual saving,
payback), embodied-carbon delta, and the per-rule results table. This is
the artefact a consultant attaches to a Green Mark submission pack.

## 6. Same building, different country — Greenship (Indonesia)

Switch the **Code** dropdown to *Greenship (GBCI) (Indonesia)* and run the
loop again (LLM ranking on). Observed:

> **RESOLVE:** 10 candidate fixes proposed (LLM-ranked (provider configured)).
>
> **APPLY:** bundle of 5 verified fixes — compliance 1/5 -> 5/5; EUI 195.9 -> 163.6 kWh/m2/yr; capex 573,170.66 IDR; annual saving 108,446.80 IDR; simple payback 5.29 yr.

The verifier's judgement changes with the code: under Greenship, shading
fins, roof insulation, HVAC upgrade, LED lighting and rooftop PV were
REJECTED (they fix no failing Greenship rule for this building), while
WWR reduction, low-e glazing, wall insulation and the two carbon-track
material swaps carried the building to 5/5. Same model, same catalogue —
different code, different verified bundle. Estidama / Al Sa'fat (UAE) and
Mostadam (KSA) are in the same dropdown.

## 7. Try it with the API

```bash
curl https://greenforge.arifin.uk/api/health
curl https://greenforge.arifin.uk/api/codes
curl -X POST https://greenforge.arifin.uk/api/run \
  -H 'Content-Type: application/json' \
  -d '{"code_id":"green_mark_sg","use_llm":true}'
```

## 8. Failure log from this session (fixed, not hidden)

1. **First LLM run after a redeploy fell back** to deterministic order
   (cold start); every subsequent run ranked by LLM. Behaviour is safe —
   the fallback is labelled in the RESOLVE line, never silent.
2. **3D viewer v1 (three.js IFCLoader) hung** on "Loading demo IFC…" with
   no error. Replaced with a direct web-ifc integration that surfaces
   every failure in the status line.
3. **Parse error** `flatMesh.geometries is not iterable` — web-ifc returns
   a Vector, not an array. Fixed by iterating `size()`/`get()`; verified
   locally against the demo IFC before redeploying.
4. **Agent-camera crash (TDZ)** — the first staged build declared its
   camera state after the render loop started and killed the whole viewer
   module. Fixed by declaring state before the loop; the error surfaced in
   the status line, which is how it was diagnosed.
5. **APPLY framing + roof glow** — flights initially inherited the previous
   camera angle (APPLY ended top-down), checks flashed past at 1.7 s, and
   the roof plate did not glow because it is an IFCCOVERING, not IFCROOF.
   Fixed with a fixed ¾ cinematic angle for overview/APPLY, 2.6 s check
   dwell, and the roof set widened to roofs + coverings.
6. **Glazing highlight was invisible** — the facade is IFCPLATE
   curtain-wall panels, not IFCWINDOW. Glazing now covers both (173
   elements), and highlighting dims the rest of the model so the selection
   cannot be missed.

## 9. Addendum — 25 Nov 2026: the demo rebuilt in ORCA's shape

At the owner's request the demo was rebuilt to mirror the ORCA demo's
experience (their Modal deployment was toured end to end first: landing,
Northbank workspace, a live Propose-fixes run), and the README was
rewritten to follow ORCA's README instead of prepending a banner to it.
Verified live on production the same evening (final build `660e4ce`):

- **Landing (dark)** — "Compliance, not just audits. Every fix verified
  before you see it.", a recorded-run replay card, problem / how-it-works
  tabs / **Try the check yourself** (SHGC slider: at 0.28 the ETTV
  estimate reads 35.3 against the limit of 50 → VERIFIED) / fix kinds /
  Northbank demo card / said-plainly / glossary + colour key.
- **Loading** — checklist: IFC load → browser parse (1,085 elements) →
  Observe → findings ready.
- **Workspace (light, three panels + Activity)** — left: 8 findings, all
  FAIL, grouped by system (ETTV 51.8 ≤50, WWR 0.65 ≤0.6, SHGC 0.55 ≤0.35,
  COP 3 ≥3.8, HVAC 1.05 ≤0.85 kW/RT, EUI 195.9 ≤180, renewables 0 ≥0.02,
  LPD 15 ≤12). Right: Finding / Fixes / Report tabs. Bottom: Activity
  with a Steps table and Workings feed, replay at 1×/2×/4×.
- **Run** — 13 steps · 1 model call (ranking) · ~29 s replay; **9 ✓
  VERIFIED, 1 ✗ REJECTED** (roof insulation — fixes no failing rule);
  compare card: compliance 0 → 8 of 8, EUI 195.9 → 115.5, carbon
  3,129,108 → 1,711,223 kgCO₂e, payback 6.2 yr · capex SGD 1,673,644.44.
  Accept on a card moves the header counter to "1 kept fixes".
- **3D + agent camera** — in a clean browser the building renders, each
  check narrates in the status line ("Agent camera: verifying LED
  lighting retrofit — VERIFIED, glowing green."), and APPLY ends with
  "the verified bundle applied — glazing, walls and roof glow green"
  over a visibly green building.
- **Report tab** — 0/8 → 8/8, all rules PASS after (ETTV 24.1, SHGC
  0.28, COP 4.2, EUI 115.5, LPD 7), capex/saving/payback, download
  button, simplified-thresholds disclaimer.
- **README** — GreenForge's own, in ORCA's structure (hero, tagline,
  badges, see-it-work, try-it, how-it-works, codes, what-verified-means,
  run-locally, said-plainly, provenance); the bolt-on `GREENFORGE.md`
  was folded in and removed.

Failures during the rebuild, fixed before sign-off: (a) the workspace
grid overflowed the viewport after a run — grid children now get
`min-width: 0`; (b) landing copy cited an ETTV limit of 45 while
`codes.json` enforces 50 — copy corrected to the enforced value;
(c) the viewer module created its renderer while the workspace was
still `display:none` (landing-first layout), sizing it 0×0 — the
viewer now refits camera + renderer whenever a model loads or a cue
fires, and the page waits for the CDN module (up to 20 s) instead of
failing instantly. One environment note: in a long-lived Chrome
profile with many heavy WebGL tabs from the day's testing, the module
intermittently never arrived; a clean browser renders every time —
if the viewer ever stays dark, reload in a fresh tab.

## Honest limits

- Rule sets in `green/codes.json` are simplified extracts for
  demonstration — verify against the current edition before any real
  submission.
- Energy/cost figures come from the simplified-physics engine unless
  EnergyPlus is installed and `ENERGYPLUS_IDF_TEMPLATE` is set; the app
  states which engine produced every run.
- Upstream geometry loop adapted from ORCA (MIT,
  IssueLab-Limited/ORCA-hackathon-2026); GreenForge is the green-code
  verified-fix extension.


## 10. Addendum — 25 Nov 2026, late evening: Demo 2 + the 3D root cause

**Demo 2 added (commit 200a7d8, deployed and verified live):** following
ORCA's two-demo shape, GreenForge now ships a second demo — **Northbank
Building Services (MEP)**, the services model of the same building
(`green/samples/Northbank-MEP.ifc`, served at `/demo2.ifc`). Its profile
inverts Demo 1's story: the envelope passes, the services fail. Verified
live run (Green Mark): findings 4 pass / 4 fail (HVAC COP, HVAC kW/RT,
renewables, lighting LPD), bundle takes it to **8/8, EUI 178.2 → 113.6**,
5 verified fixes, payback 4.7 yr, 12 steps. The landing demo section is
now "Two demos, four codes" with two cards; the workspace Menu switches
demos in place.

**The 3D-not-loading defect — root cause proven, and it was not the
site.** After the self-hosting change the viewer still failed in the
user's Chrome; instrumenting the viewer (commit 90f11f1) made it report
its own error verbatim: **"Error creating WebGL context."** The user's
Chrome GPU process had died during the evening (it rendered fine at
21:11–21:16, then never again); every later failure — CDN theory, cache
theory, tab theory — was that one dead process. The site renders
correctly in a clean browser, and the analysis loop runs even in the
affected Chrome. The remedy is a full browser quit (Cmd+Q) and restart
on the user's side; the viewer now names this failure itself instead of
spinning silently. Two real bugs were found and fixed along the way:
the missing `web-ifc-mt.worker.js` (7b7c1b2) and the unguarded renderer
creation (90f11f1). README note: per the user's clarification, README.md
is ORCA's original with GreenForge as an additional module (banner);
GREENFORGE.md carries the module documentation — this supersedes the
README description in §9.
