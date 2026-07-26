
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

## 📈 Measured Results
Project Envelope is designed for long-term stability. Performance was monitored over 10,000+ simulation steps across varying diurnal cycles.

*   **Peak Observed Savings:** ~10.2% (During transient peak-shaving events)
*   **Steady-State Savings Range:** 1.5% – 4.5%
*   **Comfort Reliability:** 100% (Zero ASHRAE-55 PMV boundary violations)

### Representative Performance Snapshot (Step 4392)
The following data reflects the building state during a high-fidelity control period as shown in the project dashboard.

| Metric | AI Instance | Baseline Instance |
| :--- | :--- | :--- |
| **Cumulative Energy** | 3,551.10 kWh | 3,694.90 kWh |
| **Measured Savings %** | **3.9%** | -- |
| **Indoor Temp (AI)** | 20.00°C | 20.15°C |

### Explainable Decision Log (Actual Output)
The following sequence from `control_log.jsonl` demonstrates the **Sentinel Gate's Stability Hysteresis**—preventing energy-wasting setpoint "jitter" while maintaining a precise comfort score (CCS).

| Step | Source | Setpoint | Reason / Status |
| :--- | :--- | :--- | :--- |
| 9000 | AI (Stabilized) | 21.00°C | **HOLD:** Change of 0.10°C is too small. |
| 9600 | AI (Stabilized) | 21.00°C | **HOLD:** Change of 0.20°C is too small. |
| 9900 | AI | 21.15°C | **APPROVED:** CCS 0.85 \| PMV -0.21 (Clo 1.09) |
| 10200| AI | 22.00°C | **APPROVED:** CCS 0.80 \| PMV -0.02 (Clo 1.05) |


## 📊 Project Dashboard
The following screenshots show the live interaction between the EnergyPlus physics engine and the AI Strategist.

![Dashboard Overview](blob:https://web.whatsapp.com/1190a544-69c7-4db8-85f0-0132f0bdea33)
*Figure 1: Real-time alignment of AI vs. Baseline energy curves and indoor temperatures.*

![Decision Log](blob:https://web.whatsapp.com/fa33cc95-5c2f-4819-9083-77612e3a5331)
*Figure 2: The Explainable AI decision log showing the Sentinel Gate in action.*


## 🛡️ Safety & Failsafes
*   **Network Loss:** If the Groq API fails/timeouts, the system instantly switches to the `FailsafeController`, maintaining the building between 21°C and 23°C.
*   **Gate Override:** If the AI fails to produce a safe setpoint even after correction, the Governor forces a "Hold Steady" command to prevent equipment damage.
