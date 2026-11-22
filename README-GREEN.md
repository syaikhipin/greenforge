# ORCA-Green — Green-code verified-fix agent

A direct extension of [ORCA by IssueLab](https://github.com/IssueLab-Limited/ORCA-hackathon-2026)
(3rd place, Startup track, Meta Global AI Developer Hackathon 2026; MIT licence, preserved in
this repo). This repo is an independent copy — not a fork — with the Green agent added under
[`green/`](green/).

> ORCA: *Fixes, not just findings. Every fix proven before you see it.*
> ORCA-Green keeps the loop and swaps the verifier.

## The idea

ORCA's loop is **Observe → Resolve → Check → Apply**, where the Check is geometry:
a proposed edit is applied to the model and only reaches a human if the clash is gone,
clearance holds, and nothing new is hit.

ORCA-Green runs the **same loop** with a different verifier — **green building codes +
energy simulation + embodied carbon** instead of geometry:

| ORCA (upstream) | ORCA-Green (this extension) |
|---|---|
| Observe: detect clashes in IFC/STEP | Observe: ingest IFC (IfcOpenShell, same library ORCA uses), extract envelope/HVAC/material quantities, rules-check |
| Resolve: LLM proposes a model edit | Resolve: agent proposes glazing spec, shading fins, HVAC sizing/COP, low-carbon material swaps from an EPD database |
| Check: geometry re-verifies the edit | Check: energy simulation + embodied-carbon calc + full rules re-check verify the edit |
| Apply: edited IFC + report | Apply: costed, compliant, submission-ready report (capex, annual saving, payback, carbon delta) |

A fix is **verified** only if it passes the Check, on one of two tracks:

1. **Compliance track** — fixes at least one failing rule, regresses no passing rule,
   and does not increase EUI.
2. **Carbon track** — cuts embodied carbon by ≥ 1%, regresses no rule, and does not
   increase EUI. (Material swaps fix carbon, not a rule; they still have to prove it.)

Rejected candidates stay in the Workings log with the reason — exactly like ORCA shows
a failed geometry check. The LLM never invents numbers: when configured, Opencode Go
only *ranks* candidate ids; every number comes from the verifier.

## Codes covered (simplified thresholds — verify before real use)

- **Green Mark** — BCA, Singapore (SGD)
- **Estidama Pearl / Al Sa'fat** — UAE (AED)
- **Mostadam** — Saudi Arabia (SAR)
- **Greenship** — GBCI, Indonesia (IDR)

Rule sets live in [`green/codes.json`](green/codes.json); EPD-style material options
(concrete, steel, aluminium, glass, insulation) in [`green/epd.json`](green/epd.json).

## Why now — the Singapore tailwind

From **1 October 2026**, CORENET X model-based submission is mandatory in Singapore for
all new projects with **GFA ≥ 5,000 m²** (the threshold was ≥ 30,000 m² from Oct 2025),
with Green Mark compliance assessed on the submitted IFC-SG model
([BCA CORENET X](https://info.corenet.gov.sg)). Every mid-size project in Singapore now
needs a model that passes green checks *before* submission — and a failed gateway costs
a redesign cycle. ORCA-Green is the pre-submission fixer for that moment, and the same
loop ports to Estidama/Al Sa'fat, Mostadam and Greenship across the Gulf and Indonesia.

**Buyers:** sustainability / MEP consultants and developers, per-project pricing.

## Run it

```bash
cd green
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
# open http://localhost:8000  — or try GET /api/demo for a full loop run as JSON
```

Docker (this is what Coolify deploys):

```bash
docker build -t orca-green green/
docker run -p 8000:8000 orca-green
```

Or from the repo root: `docker compose up --build` (see `docker-compose.yml`).

### Model (optional): Opencode Go

Set `OPENCODE_API_KEY` and the agent will ask Opencode Go
(`OPENCODE_GO_MODEL`, default `mimo-v2.6-flash`, endpoint
`https://opencode.ai/zen/go/v1`) to rank candidate fixes. Without a key it runs in
deterministic catalogue order — the verifier, and therefore every number, is unchanged.

### Energy engine — honest label

The verifier reports its engine on `/api/health`. The bundled engine is a simplified
physics model (ETTV/OTTV, envelope + HVAC + lighting EUI). If an `energyplus` binary is
present it is detected, and `ENERGYPLUS_IDF_TEMPLATE` is the hook for a full IDF run —
the app never claims an EnergyPlus run that did not happen.

### IFC ingest

`POST /api/ifc` (multipart) parses an uploaded IFC with IfcOpenShell (element counts,
space areas → GFA, window/wall ratio → WWR estimate) and runs the loop on the derived
building. If a file cannot be parsed, the API says so and falls back to the demo
building — counts are never fabricated.

## Demo numbers (bundled demo building, Green Mark)

- Compliance **0/8 → 8/8** rules passing
- EUI **195.9 → 115.5 kWh/m²/yr**
- Embodied carbon **−1,417,885 kgCO₂e** (GGBS concrete + EAF steel swaps, carbon track)
- Capex **SGD 1,673,644**, annual saving **SGD 270,065**, simple payback **6.2 years**
- 9 verified fixes; roof insulation rejected in the Workings (fixes no failing rule)

Reproduce: `GET /api/demo`. Tests: `cd green && python -m pytest test_green.py -q` (5 tests).

## Layout

- `green/` — the Green-code verified-fix agent (new in this repo)
- `server/`, `web/`, `samples/`, `docs/` — upstream ORCA, unmodified
- Upstream README: [`README.md`](README.md) · Upstream licence: [`LICENSE`](LICENSE) (MIT)

## Disclaimer

Thresholds in `codes.json` are simplified for demonstration. Verify against the current
edition of each code, and have a Qualified Person / licensed professional verify any
model and report before a real regulatory submission.
