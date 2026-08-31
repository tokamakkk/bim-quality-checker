# BIM Quality Checker (AI-Agent Web Prototype)

A web-based micro prototype that performs compliance and soundness checks on architectural IFC models, with a chat agent that can explain findings and apply guided attribute-level fixes.

Part of the **HKU AI Agent Technical Test** — 2 rules, 3 deliverables, deadline 2026-08-31 (HKT). See [doc/DESIGN.md](doc/DESIGN.md) for the full design document. **Status: complete** — full integration verified end-to-end (33/33 tests, HTTP-API smoke incl. the agent repair flow).

## The two rules

| Rule | Target | Verdict logic |
|---|---|---|
| **R1 Attribute Completeness** | IfcWall, IfcDoor | `R1a` empty `Name` → **fail** · `R1b` missing/empty `FireRating` across all psets → **warn** (cannot judge) |
| **R2 Exit Door Width** | IfcDoor | `OverallWidth` ≥ 900 mm → **pass** · < 900 mm → **fail** · missing → **warn** |

Three-tier verdicts (pass / warn / fail) apply everywhere — UI cards, colored 3D viewer, chat context, and the exported HTML report share one verdict list and one color language (green `#22c55e` / amber `#eab308` / red `#ef4444`).

**Acceptance numbers (§8.1)** — `sample_data/bad_model.ifc` (2 walls + 4 doors) yields exactly 16 verdicts and **5 findings: 4 fail / 1 warn** (empty-name wall, missing FireRating, 3 narrow doors 800/700/800 mm incl. a FireExit-flagged one); `good_model.ifc` is all-green. The right panel reads `✅ 11 通过 · ⚠️ 1 警告 · ❌ 4 违规`.

## Tech stack

- **Python 3** + **IFCOpenShell** (`ifcopenshell`) — IFC parsing, model generation, and agent edits (`ifcopenshell.api`)
- **Gradio** (`gr.Blocks`) — 3-column web UI with file upload, `gr.Model3D` viewer, and chat
- **trimesh** — per-element meshes colored by verdict, exported to GLB for the viewer
- **pytest** — engine / report / mesh / end-to-end test suites
- **DeepSeek API** (OpenAI-compatible) — optional chat-provider for the agent; without `DEEPSEEK_API_KEY` the agent answers deterministically offline (demo never depends on an external API)

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python src/app.py          # starts the Gradio server on http://127.0.0.1:7860 (fixed port)
GRADIO_SHARE=1 python src/app.py
                           # optional: also print a temporary public share link.
                           # Off by default — the share page loads JS from an overseas
                           # CDN and can white-screen on restricted networks.
```

Workflow: upload an `.ifc` file (rules are optional — `config/rules.json` is the default) → click 运行检查 (Run) → findings appear in the right panel and as colors in the 3D viewer → toggle 仅显示违规 to keep only warn/fail elements → ask the chat agent about findings or ask it to fix them → re-check shows updated verdicts → export the single-file HTML report.

Sample models live in `sample_data/` (`good_model.ifc` = healthy baseline, `bad_model.ifc` = demo model with the 5 §8.1 defects). Run `python generate_models.py` to regenerate them.

## Chat agent (DESIGN.md §6)

One chat window, two capabilities:

- **Capability A — Q&A (read-only).** "哪些门太窄了？", "哪些构件没有名字？", "哪些构件缺少防火等级？" — answered from the current verdicts (deterministic offline; DeepSeek LLM when `DEEPSEEK_API_KEY` is set).
- **Capability B — guided fixes.** Three narrow tools, applied to a **working copy** (the uploaded file is never touched — re-uploading it is the undo mechanism):
  - `set_property` — allowlisted `Name` / `FireRating` ("把空名称的构件都补上名字", "给缺少防火等级的构件补上防火等级")
  - `set_door_width` — clamped to [600, 3000] mm ("把所有小于900mm的门改成1000mm")
  - `rerun_check` — re-verify after each fix and refresh the UI panels

Demo flow: ask "哪些门太窄了？" → 3 doors listed → fix them → R2 green → fix the remaining attribute issues → **全部通过, all panels green**, 3D re-colored.

## Tests

```bash
pytest tests/ -v            # 33 tests: engine (mock + §8.1 sample acceptance),
                            # report HTML structure, GLB colors, e2e pipeline + repair flow
```

`tests/test_e2e.py` runs the full pipeline offline (load → check → GLB → HTML report) and the agent repair flow (door width → names → fire ratings → all green, original file byte-identical).

## Design simplifications & assumptions (stated honestly)

- **R2 uses nominal width, not clear width.** `IfcDoor.OverallWidth` is the opening width; actual pass-through (clear) width after leaf + frame is smaller. The prototype checks nominal width as a documented proxy.
- **R2 threshold is configurable.** 900 mm follows China's **GB 50016** (疏散门净宽 ≥ 0.9 m); the US IBC equivalent is 813 mm (32 in). The threshold is a rule parameter in `config/rules.json`, so the same engine serves either jurisdiction.
- **`FireRating` is searched across all property sets.** It is not a direct attribute of IfcWall/IfcDoor — it lives in psets reachable via `IsDefinedBy → IfcRelDefinesByProperties`. The checker traverses **every** pset (standard and vendor, e.g. `Pset_Revit_...`) and accepts the first non-empty value, because real exports vary.
- **"Exit doors" have no native concept in IFC.** Default behavior checks all IfcDoor; a door with `Pset_DoorCommon.FireExit = true` is grouped and flagged "EXIT" so a stricter threshold could apply later.
- **No clash detection, no custom rule language, no Revit/AutoCAD input, no multi-user/cloud.** IFC only, runs locally, rules are data (JSON), 1–2 rules done well. Full non-goals list in §2.4 of the design doc.

## Design reference

The engine reuses battle-tested patterns from the open-source **BIM Quality Checker (BQC)** by T. Kang (MIT): config-driven rule engine (`validation_rules` JSON with typed conditions), pset-property lookup helpers, and verdict-colored GLB export. See [doc/DESIGN.md](doc/DESIGN.md) (§4.4) for attribution details.

## Repository layout

```
├── config/rules.json          # the 2 rules as data (uploadable)
├── prompts/                   # agent system prompt, tool schemas, context builder
├── sample_data/               # good_model.ifc, bad_model.ifc (synthetic, defective by design)
├── generate_models.py         # regenerates the two sample models
├── requirements.txt
├── src/
│   ├── app.py                 # Gradio UI (3 columns + chat), server entry point
│   ├── core/                  # engine.py, rules_impl.py, ifc_utils.py, verdict.py
│   ├── viz/                   # mesh_exporter.py (colored GLB)
│   ├── report/                # report_html.py (single-file HTML)
│   └── agent/                 # __init__.py = chat() integration point (tools + DeepSeek
│                              #   fallback inlined; provider.py / tools.py / context.py
│                              #   are the DESIGN §6 modularization target)
├── tests/                     # test_engine / test_report_html / test_mesh_exporter / test_e2e
└── .work/                     # runtime artifacts: upload copies, GLBs, reports, fixed models
```
