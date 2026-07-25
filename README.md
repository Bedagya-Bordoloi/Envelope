
# README.md — Project Envelope

A closed-loop Building Management System: EnergyPlus runs the real building physics, a Groq-hosted LLM (Llama-3.1-8B) proposes energy-saving HVAC setpoints via real tool-calling, and a calibrated safety gate (Sentinel Gate) decides whether to trust each proposal — rejecting with a plain-language reason, feeding that reason back so the LLM corrects itself, and falling back to a local rule-based controller if the LLM disappears.

Built for the Honeywell Hackathon — Eco-Loop Building Agents (PS1: AI-Powered Autonomous Smart Building Optimization).

## Setup

**Prerequisites**
- Python 3.11+ (project was built/tested against 3.13)
- EnergyPlus (Docker image or official installer) — set `EPLUS_DIR` to your install directory if it isn't `C:\EnergyPlusV24-1-0`
- A Groq API key

**Install**
```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
Configure
Create a .env file in the project root:
code
Code
GROQ_API_KEY=your_key_here
Set EPLUS_DIR as an environment variable if EnergyPlus isn't at the default path.
Prepare the model
models/baseline.idf alone has no exposed thermostat actuators. Generate the actuator-ready version once:
code
Bash
python scripts/patch_idf.py
This writes models/controlled.idf, which main.py actually runs against.
Running it
Three terminals (venv activated in each):
code
Bash
# Terminal 1 — AI-controlled instance
python main.py

# Terminal 2 — fixed-schedule baseline instance
python main.py --baseline

# Terminal 3 — live dashboard
streamlit run ui/app.py
Each instance writes to its own log (logs/ai/control_log.jsonl / logs/baseline/control_log.jsonl) and output directory, so they never collide. Standalone component tests:
code
Bash
python agents/strategist.py     # Strategist in JSON-only fallback mode (no live bridge)
python core/sentinel_gate.py    # Gate sanity checks (approve / reject-bounds / reject-PMV)
Directory structure
code
Code
Envelope/
├── config/building_policy.yaml   # Operator-editable hard bounds — no code edits needed
├── models/                       # baseline.idf/.epw (unmodified) + controlled.idf (patched)
├── core/
│   ├── energyplus_bridge.py      # pyenergyplus runtime callbacks, dual-actuator control
│   ├── comfort.py                # ASHRAE-55 PMV (pythermalcomfort)
│   ├── sentinel_gate.py          # CCS scoring, explanation, correction packet
│   ├── failsafe_controller.py    # Local rule-based setback controller
│   └── baseline_controller.py    # Fixed-schedule comparison instance
├── mcp/
│   ├── tools.py                  # get_state / set_hvac / get_weather / get_carbon_intensity
│   └── server.py                 # Optional real MCP server over stdio (pip install mcp)
├── agents/strategist.py          # Groq/Llama tool-calling + JSON-only fallback + self-correction
├── ui/app.py                     # Streamlit dashboard: overlay chart, energy savings, decision log
├── logs/{ai,baseline}/control_log.jsonl
├── main.py                       # Dual-instance closed-loop orchestrator
└── requirements.txt
Measured results
From a completed run (AI: 20,306 control steps · Baseline: 21,160 control steps):
Metric	AI instance	Baseline instance
Cumulative energy	8,665.16 kWh	9,213.87 kWh
Live savings vs. baseline	6.0%	—
Indoor temp (snapshot)	21.50°C	—
Outdoor temp (snapshot)	26.95°C	—
Sample decision-log entries (logs/ai/control_log.jsonl), showing a real reject → correction → approve sequence:
Step	Source	Setpoint	Reason
20040	AI	20.00°C	APPROVED — CCS 0.71
20100	FAILSAFE (gate override)	22.00°C	REJECTED — sensed zone comfort outside ASHRAE-55 acceptable PMV band (±0.5) even though setpoint itself was within raw °C bounds
20160	AI	23.00°C	APPROVED — CCS 0.74
20220	AI	22.50°C	APPROVED — CCS 0.81
20280	AI (Corrected)	21.50°C	CORRECTED: APPROVED — CCS 0.77
Honesty note on what's not yet measured: the run above predates a fix that wires real MCP tool-calling into the Strategist and logs per-decision latency (latency_s) and which tools were actually invoked (tool_calls) into control_log.jsonl. Those two fields — and a full-run comfort-violation count — should be pulled from a fresh run on the current main.py rather than reused from this run. This README will be updated with those numbers once that run completes; no latency or tool-call-count figures are stated here because none were captured at the time this run was taken.
Known limitations
See ARCHITECTURE.md §11 for the full list — stated here briefly: Groq dependency (mitigated by failsafe), forecast is the simulation's own known-future .epw weather rather than live data, CCS threshold is a starting prior rather than a production-calibrated value, carbon intensity is explicitly simulated, single-zone scope, single-step self-correction, and the dashboard currently re-reads the full log file each poll rather than tailing it.
Future scope
Multi-zone coordination, real weather-forecast API integration, edge-quantized local LLM, real grid carbon-intensity API, occupancy sensor fusion, multi-step self-correction negotiation, and a shadow-mode-to-live CCS recalibration pipeline.
code
Code
Both are ready to save as-is. Once you run the updated `main.py`, re-pull the energy/savings numbers plus the new `latency_s`/`tool_calls` fields from the log and drop them into the README table — that'll close the last honest gap I flagged.