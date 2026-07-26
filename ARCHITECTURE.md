
# Technical Architecture: Project Envelope

## 1. System Overview
Project Envelope is built on a **Supervisory Control** architecture. It does not replace the local Building Management System (BMS); instead, it acts as an intelligent "wrapper" that optimizes setpoints based on real-time weather, grid carbon intensity, and thermal comfort physics.

The system follows a strict **SENSE → REASON → VERIFY → ACT** control loop.

---

## 2. Core Components

### A. The Simulator (EnergyPlus Bridge)
Unlike traditional approaches that use batch-mode simulations (editing an IDF and restarting), Project Envelope utilizes the **EnergyPlus Python Runtime API** (`pyenergyplus`).
*   **Runtime Callbacks:** Our logic is injected into the `callback_begin_system_timestep_before_predictor` hook. This ensures a true closed-loop where the AI reacts to the simulation state as it evolves.
*   **Dual-Instance Orchestration:** `main.py` manages two parallel simulation threads:
    1. **AI-Gated:** The optimized instance.
    2. **Baseline:** A "Counterfactual" instance running a fixed 22°C schedule.
    *This allows for the 1:1 "Live Savings" measurement shown on the dashboard.*

### B. The Strategist (Groq-Llama Cognitive Engine)
The reasoning engine is powered by **Llama-3.1-8B via Groq**. 
*   **Tool Calling:** The Strategist uses the **Model Context Protocol (MCP)** to interact with the building tools (`get_state`, `set_hvac`).
*   **Forward Reasoning:** By consuming the simulation’s `.epw` weather file as a forecast proxy, the Strategist can perform **proactive pre-cooling** before outdoor heat spikes, rather than reacting after the indoor temp rises.

### C. The Governor (Sentinel Gate)
The Governor (`core/sentinel_gate.py`) provides the "Safety Envelope." Every LLM proposal is scored against the **Critical Control Score (CCS)**.

**The CCS Formula:**
$$CCS = (w_{v} \cdot \text{Comfort}) + (w_{r} \cdot \text{Stability}) + (w_{c} \cdot \text{LLM\_Conf}) + (w_{ca} \cdot \text{Carbon})$$

*   **Comfort (ASHRAE-55):** We calculate the **Predicted Mean Vote (PMV)** using the `pythermalcomfort` library. If a proposal pushes PMV outside the $\pm0.5$ range, the gate triggers an immediate **REJECT**.
*   **Stability:** Prevents rapid setpoint oscillations (jitter) that would cause mechanical wear on HVAC actuators.

---

## 3. Agentic Autonomy: The Self-Correction Loop
A key requirement of PS1 is "Self-Correction." Project Envelope implements this via a **Recursive Feedback Loop**:
1.  **Rejection:** If the Sentinel Gate rejects a proposal (e.g., "Setpoint 19°C violates comfort limits"), the rejection reason is captured.
2.  **Context Injection:** This reason is fed back into the Strategist's "Correction Context."
3.  **Refinement:** The LLM re-evaluates its strategy and submits a revised proposal (e.g., "21.5°C") within the same timestep.
4.  **Verification:** The Gate re-evaluates. If it fails again, the system defaults to a **Local Rule-Based Failsafe**.

---

## 4. Resilience & Failsafes
To meet the **30% System Integration** requirement, the system must survive network loss:
*   **Local Failsafe:** If the Groq API exceeds the 10s timeout, the `FailsafeController` takes over. It uses a zero-dependency rule-based setback (e.g., Heat to 21°C / Cool to 23°C) to keep the building "in-bounds" until connectivity is restored.
*   **State Deduplication:** The `EnergyPlusBridge` includes a fix for "Step Inflation," ensuring that EnergyPlus internal sub-timesteps do not cause duplicate AI calls or incorrect energy metering.

---

## 5. Justification of Thresholds
Our **0.65 CCS Threshold** is not a hardcoded guess. Using `scripts/calibrate_ccs.py`, we analyzed over 1,000 AI decisions:
*   **Mean Score:** 0.79
*   **Standard Deviation ($\sigma$):** 0.07
*   **Logic:** Setting the threshold at **0.65 (approx. Mean - 2$\sigma$)** creates a "Safety Floor" that permits creative optimization while blocking 95% of statistically likely "hallucinated" or unsafe moves.

---

## 6. Technical Stack
*   **Language:** Python 3.11+
*   **Physics:** EnergyPlus API v24.1.0
*   **LLM:** Llama-3.1-8B (Groq)
*   **Communication:** Model Context Protocol (MCP)
*   **Comfort Model:** ASHRAE-55 (PMV/PPD)
*   **Dashboard:** Streamlit + Plotly (Real-time JSONL streaming)
