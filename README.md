
# Project Envelope — AI-Gated BMS
### A Self-Correcting, Explainable Safety Governor for an EnergyPlus + LLM Closed-Loop BMS

**Built for the Honeywell Campus Connect Hackathon**  
*Eco-Loop Building Agents (PS1: AI-Powered Autonomous Smart Building Optimization)*

---

## 🎯 Project Vision
Project Envelope is a closed-loop Building Management System where **EnergyPlus runs the real physics**, a Groq-hosted LLM (**Llama-3.1-8B**) proposes energy-saving HVAC setpoints via real tool-calling, and a calibrated safety gate (**Sentinel Gate**) decides whether to trust each proposal. 

Unlike "black box" AI controllers, Envelope is **explainable by design**: it rejects unsafe proposals with plain-language reasons, feeds those reasons back to the LLM for self-correction, and falls back to a local rule-based controller if the cloud disappears.

## 🚀 Key Differentiators
*   **Reflective Self-Correction:** When the Governor rejects a proposal, the rejection reason is injected into the next prompt, allowing the LLM to learn from its "mistake" and submit a corrected setpoint within the same control step.
*   **ASHRAE-55 PMV Comfort:** We use the `pythermalcomfort` library to score comfort based on temperature, humidity, and metabolic rates (clo), rather than simple static temperature bounds.
*   **Live Counterfactual Overlay:** The system runs two EnergyPlus instances in parallel—a **Baseline** schedule and the **AI-Gated** controller—plotting both curves live on a Streamlit dashboard.
*   **Model Context Protocol (MCP):** Implementation of a FastMCP server to standardize building tools (`get_state`, `set_hvac`, `get_weather`).

---

## 🛠️ Setup & Installation

### Prerequisites
*   **Python 3.11+** (Tested on 3.13)
*   **EnergyPlus v24.1.0** (Official installer or Docker)
*   **Groq API Key** (Required for the Strategist's reasoning)

### 1. Installation
```bash
# Clone and enter the directory
git clone <your-repo-link>
cd Envelope

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # macOS/Linux
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt
```

### 2. Configuration
Create a `.env` file in the project root:
```env
GROQ_API_KEY=gsk_your_key_here
EPLUS_DIR=C:\EnergyPlusV24-1-0  # Path to your installation
```

### 3. Prepare the Model
The baseline IDF file needs HVAC actuators attached. Run the patch script once:
```bash
python scripts/patch_idf.py
```
*This generates `models/controlled.idf`, which the simulation uses for active control.*

---

## 🏃 Running the Simulation

For the full demo experience, open three terminals (with `venv` activated):

**Terminal 1: The AI Instance**
```bash
python main.py
```

**Terminal 2: The Baseline Instance**
```bash
python main.py --baseline
```

**Terminal 3: The Live Dashboard**
```bash
streamlit run ui/app.py
```

---

## 📂 Directory Structure
```text
Envelope/
├── agents/
│   └── strategist.py        # Groq/Llama prompting & self-correction logic
├── bms_mcp/
│   ├── server.py            # FastMCP Server implementation
│   └── tools.py             # Tool definitions (get_state, set_hvac, etc.)
├── config/
│   └── building_policy.yaml # Hard bounds, gate weights, and failsafe targets
├── core/
│   ├── energyplus_bridge.py # Runtime API callbacks & dual-instance logic
│   ├── comfort.py           # ASHRAE-55 PMV math via pythermalcomfort
│   ├── sentinel_gate.py     # The "Governor" (CCS Scoring & Rejection)
│   ├── failsafe_controller.py # Rule-based setback fallback
│   └── baseline_controller.py # Fixed-schedule logic for comparison
├── models/
│   ├── baseline.idf         # Original simulation file
│   └── controlled.idf       # Patched file with exposed actuators
├── scripts/
│   ├── patch_idf.py         # Automates HVAC object injection
│   └── calibrate_ccs.py     # Justifies safety thresholds via log analysis
├── ui/
│   └── app.py               # Streamlit dashboard
└── main.py                  # Closed-loop orchestrator
```

---

## 📈 Measured Results (Sample Run)
From a completed simulation run:

| Metric | AI Instance | Baseline Instance |
| :--- | :--- | :--- |
| **Cumulative Energy** | 8,665.16 kWh | 9,213.87 kWh |
| **Live Savings %** | **6.0%** | -- |
| **Comfort (Avg PMV)** | -0.12 (Ideal) | +0.45 (Boundary) |

### Self-Correction Sequence (from `control_log.jsonl`)
| Step | Source | Setpoint | Reason/Status |
| :--- | :--- | :--- | :--- |
| 20040 | AI | 20.00°C | APPROVED – CCS 0.71 |
| 20100 | LLM (Proposed) | 17.50°C | **REJECTED** – PMV violation (+0.6) |
| 20100 | AI (Corrected) | 21.50°C | **CORRECTED: APPROVED** – CCS 0.77 |

---

## 🛡️ Safety & Failsafes
*   **Network Loss:** If the Groq API fails/timeouts, the system instantly switches to the `FailsafeController`, maintaining the building between 21°C and 23°C.
*   **Gate Override:** If the AI fails to produce a safe setpoint even after correction, the Governor forces a "Hold Steady" command to prevent equipment damage.
