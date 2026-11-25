<h1>GreenForge</h1>

> **Compliance, not just audits. Every fix verified before you see it.**<br>
> *The AI green-code coordinator for buildings: GreenForge reads the model, checks it against the green building
> code, proposes a real fix for every failing rule, and proves each fix — rules re-checked, energy re-simulated,
> carbon recounted — before it reaches you. Observe. Resolve. Check. Apply.*

[![Live demo](https://img.shields.io/badge/Live_Demo-Open_GreenForge-2EA44F?style=for-the-badge&logoColor=white&labelColor=555555)](https://greenforge.arifin.uk)
[![GreenForge: Observe · Resolve · Check · Apply](https://img.shields.io/badge/GreenForge-Observe_%C2%B7_Resolve_%C2%B7_Check_%C2%B7_Apply-0B3D2E?style=for-the-badge&labelColor=11161B)](#how-it-works)
[![Fixes: verified by re-check](https://img.shields.io/badge/Every_Fix-Verified_by_Re--check-00897B?style=for-the-badge&labelColor=555555)](#what-verified-means)
[![Codes: Green Mark · Estidama · Mostadam · Greenship](https://img.shields.io/badge/Codes-Green_Mark_%C2%B7_Estidama_%C2%B7_Mostadam_%C2%B7_Greenship-5C6BC0?style=for-the-badge&labelColor=555555)](#the-codes)
[![Formats: IFC in · HTML report out](https://img.shields.io/badge/Formats-IFC_in_%C2%B7_HTML_report_out-5C6BC0?style=for-the-badge&labelColor=555555)](#try-it)
[![Ranking: your own LLM, OpenAI-compatible](https://img.shields.io/badge/Ranking-Your_Own_LLM_%28OpenAI--compatible%29-0668E1?style=for-the-badge&labelColor=555555)](#how-it-works)
[![three.js and web-ifc viewer](https://img.shields.io/badge/three.js-web--ifc_Viewer-000000?style=for-the-badge&logo=threedotjs&logoColor=white&labelColor=555555)](#the-agent-camera)
[![FastAPI and IfcOpenShell](https://img.shields.io/badge/FastAPI-IfcOpenShell-009688?style=for-the-badge&logo=fastapi&logoColor=white&labelColor=555555)](#run-locally)
[![Tests](https://img.shields.io/badge/pytest-5_Passing-2EA44F?style=for-the-badge&logo=pytest&logoColor=white&labelColor=555555)](#run-locally)
[![License: MIT](https://img.shields.io/badge/License-MIT-D4A72C?style=for-the-badge&labelColor=555555)](LICENSE)

**[Open the live demo](https://greenforge.arifin.uk)** · [See it work](#see-it-work) · [Try it](#try-it) ·
[How it works](#how-it-works) · [The codes](#the-codes) · [Run locally](#run-locally) · [Walkthrough](WALKTHROUGH.md)

---

**For the sustainability consultant, who makes a building pass its green code.** Envelope, HVAC and lighting are
designed by different teams against different assumptions, and the building that reaches the assessor fails on the
interactions: the glass that overheats the plant, the plant that eats the EUI budget, the materials that blow the
carbon cap. A findings PDF does not fix that. GreenForge proposes a fix for every failing rule — glazing, shading,
insulation, plant, PV, material swaps — and proves it before anyone sees it: the fix is re-run through the whole
rule set and the energy model, and it only counts if it repairs a failure without regressing anything else.

GreenForge stands for its loop: **Observe** the building against the code, **Resolve** it with proposed fixes,
**Check** every fix by re-running the numbers, **Apply** the verified bundle, costed.

The demo building is **Northbank**, the riverside-office baseline asset from the ORCA project: as modelled it fails
all 8 BCA Green Mark rules (EUI 195.9 kWh/m²/yr, ETTV 51.8 W/m², 3,129,108 kgCO₂e embodied). The verified bundle
takes it to **8/8, EUI 115.5, −45% embodied carbon, 6.2-year simple payback** — and under Greenship (Indonesia) the
same building gets a *different* verified bundle (5 fixes, 1/5 → 5/5), because the verifier's judgement follows the
code, not the catalogue.

## See it work

Open [greenforge.arifin.uk](https://greenforge.arifin.uk) and press **Watch GreenForge fix a building**:

1. **Landing → loading → workspace.** A dark landing in the ORCA demo's shape, a loading checklist, then a
   three-panel workspace: findings on the left, the model in 3D in the middle, the finding and its fixes on the
   right, Activity along the bottom.
2. **Pick a finding.** Every rule, its value against its limit, grouped by system. Clicking one flies the 3D
   camera to that system and lights it red.
3. **Propose fixes.** Candidate cards appear; the verifier checks each one in the open — watch the roof-insulation
   card go **REJECTED** (*fixes no failing rule*) while nine others verify. Accept the fixes you keep.
4. **Apply.** A Before/After compare card, the rules table before and after, and a submission-ready HTML report:
   capex, annual saving, payback, per-rule results.

The full scripted run, with the exact status lines and figures observed on the production deployment, is in
[WALKTHROUGH.md](WALKTHROUGH.md) — including the failure log of what broke during the demo session and how each
failure was fixed.

### The agent camera

The workings are staged in 3D, not just listed. Running the loop auto-loads the model; **Observe** flies to an
overview; each **Check** flies the camera to the system under test — facade, walls, roof plate, floor slabs,
structure, frame — and pulses it **green for VERIFIED, red for REJECTED** with the rest of the model dimmed;
**Apply** pulls back and glows the whole verified bundle green. You can orbit freely at any point; the camera
resumes staging at the next step.

## Try it

- **Live:** [greenforge.arifin.uk](https://greenforge.arifin.uk) — the Northbank demo, or upload your own `.ifc`
  (parsed in the browser for 3D, and server-side with IfcOpenShell for the assessment).
- **API:**

```bash
curl https://greenforge.arifin.uk/api/health
curl https://greenforge.arifin.uk/api/codes
curl -X POST https://greenforge.arifin.uk/api/run \
  -H 'Content-Type: application/json' \
  -d '{"code_id":"green_mark_sg","use_llm":true}'
```

## How it works

| Stage | What happens |
|---|---|
| **Observe** | IFC ingest (IfcOpenShell server-side; web-ifc in the browser). The building is simulated (simplified-physics engine — EnergyPlus is *not* bundled, and every run says so) and checked against the chosen code's rules. |
| **Resolve** | A deterministic catalogue proposes fixes for the failures: WWR reduction, low-e glazing, shading fins, wall/roof insulation, HVAC upgrade, LED, rooftop PV, GGBS concrete, EAF steel (EPD factors in `green/epd.json`). An LLM may **re-rank** the candidates — bring your own OpenAI-compatible endpoint (the demo runs on the owner's Opencode Go subscription). It never invents numbers; if it is unreachable, the catalogue order is used and the run says so. |
| **Check** | Each candidate is applied alone and the *entire* rule set + simulation + carbon count are re-run. Verified requires: fixes ≥1 failing rule **or** ≥1% embodied-carbon cut (carbon track), **no** rule regression, **no** EUI increase. Rejections stay visible with their reasons. |
| **Apply** | Verified fixes are combined, re-checked as a bundle, and costed: capex, annual energy saving, simple payback, carbon delta. Output: the workspace compare card + a submission-ready HTML report at `/api/report/<run_id>`. |

## The codes

| Code | Authority | Country | Rules in the demo set |
|---|---|---|---|
| Green Mark | BCA | Singapore | ETTV, WWR, SHGC, HVAC COP, kW/RT, EUI, renewable share, lighting |
| Estidama / Al Sa'fat | Abu Dhabi UPC / Dubai | UAE | envelope, EUI, renewables, lighting (simplified) |
| Mostadam | MOMRAH | Saudi Arabia | envelope, EUI, renewables, lighting (simplified) |
| Greenship | GBCI | Indonesia | EUI, WWR, SHGC, renewables, lighting (simplified) |

Rule sets live in `green/codes.json` and are **simplified extracts for demonstration** — verify against the current
edition before any real submission. Context that makes this timely: Singapore's CORENET X makes Green Mark a
filing gate, mandatory from 1 Oct 2026 for new projects with GFA ≥ 5,000 m².

## What “verified” means

A fix is verified when, on a fresh re-run of the whole model: **the failure it targets is repaired** (or embodied
carbon falls ≥1%), **nothing that passed starts failing**, and **EUI does not rise**. The bundle is then re-checked
as a whole before it is costed. Numbers in a card are the re-run's numbers, never the proposer's.

## Run locally

```bash
cd green
python -m venv .venv && . .venv/bin/activate   # or your usual env
pip install -r requirements.txt
uvicorn app:app --port 8000
# open http://localhost:8000 — tests: python -m pytest test_green.py -q
```

Optional LLM ranking (any OpenAI-compatible endpoint):

```bash
export OPENAI_API_KEY=... OPENAI_MODEL=... OPENAI_BASE_URL=...
# Opencode Go works out of the box via these variables (the app adds the
# session header Go requires when the base URL points at opencode.ai).
```

Deploy: the repo root `docker-compose.yml` runs the `green/` service; the production instance runs on Coolify
behind `greenforge.arifin.uk`.

## Said plainly: what it doesn't do yet

- Rule sets are simplified extracts; a QP / licensed professional must verify the model and the report before any
  real submission.
- Energy and cost figures come from the simplified-physics engine unless EnergyPlus is installed and
  `ENERGYPLUS_IDF_TEMPLATE` is set — the app states which engine produced every run.
- Accepted fixes are kept for the session and the report; GreenForge does not yet write the edits back into your
  IFC file.
- Uploaded IFCs whose parameters can't be read fall back to the form inputs, and the ingest note says so.

## Provenance

GreenForge is the green-code verified-fix extension of **[ORCA](https://github.com/IssueLab-Limited/ORCA-hackathon-2026)**
by IssueLab (MIT) — same loop, same demo shape, a different verifier: green building codes, an energy model and an
embodied-carbon count instead of clash geometry. The upstream ORCA application (clash coordination: `web/`,
`server/`, `docs/`) is preserved in this repository unmodified; GreenForge itself lives in `green/` with its
walkthrough in [WALKTHROUGH.md](WALKTHROUGH.md). Northbank is ORCA's pre-existing synthetic baseline asset.

## License

MIT — see [LICENSE](LICENSE). Upstream ORCA code remains under its own MIT license and attribution.
