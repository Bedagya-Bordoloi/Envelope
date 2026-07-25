"""
main.py - Project Envelope closed-loop orchestrator.

Fixes vs. the previous version:

1. FLAT-INDOOR-TEMP BUG: EnergyPlusBridge now actuates both heating and
   cooling setpoints (see core/energyplus_bridge.py's header for the
   full explanation) using comfort.deadband_c from the policy file. This
   file passes that value through when constructing the bridge.

2. PMV COMFORT MODEL: decide() now receives humidity from the bridge and
   passes (indoor_temp, humidity) into SentinelGate.check(), so proposals
   are gated on real ASHRAE-55 PMV, not just raw degC bounds.

3. LIVE COUNTERFACTUAL OVERLAY (Feature 2, previously entirely missing):
   this file now runs in one of two modes, selected by a CLI flag:

       python main.py              -> AI-controlled instance
       python main.py --baseline   -> fixed-schedule baseline instance

   Run BOTH, each in its own terminal (with the venv activated in both),
   for however long you want the demo run to last. They are two separate
   OS processes rather than two EnergyPlus states in one process/thread,
   because EnergyPlus's runtime API is not documented as safe for two
   states running concurrently in the same interpreter — two processes
   sidesteps that risk entirely and mirrors how you already run main.py
   and the Streamlit dashboard as separate processes.

   Each mode writes to its own log file (paths.control_log_ai /
   paths.control_log_baseline) and its own output directory, so the two
   runs never collide. ui/app.py reads both logs to build the overlay
   chart and the live energy-savings number.

4. REAL TOOL-CALLING WIRED IN (Feature 8, previously dead code): the
   Strategist and mcp/tools.py both fully implemented real tool-calling
   (get_state/get_weather/get_carbon_intensity/set_hvac), but nothing
   ever attached a live mcp.tools.ToolContext to the Strategist, so
   self.strategist.tool_context stayed None and every run silently used
   the single-shot JSON-only fallback path instead. attach_bridge() now
   builds a ToolContext from the live bridge + policy and sets it on the
   Strategist, so the model genuinely calls tools this run.

5. MEASURED LATENCY + TOOL-CALL EVIDENCE LOGGED: agents/strategist.py
   already timed itself into last_latency_s and recorded last_tool_calls,
   but nothing wrote either to the control log, so there was no way to
   verify from the logs that tool-calling happened or what it cost.
   log_step() now records both per step (summed across the correction
   round when one occurs), giving Section 1.1's "every number measured,
   not asserted" requirement something real to point to for latency.
"""

import argparse
import os
import sys
import json

import yaml
from dotenv import load_dotenv

from core.energyplus_bridge import EnergyPlusBridge
from core.sentinel_gate import SentinelGate
from core.failsafe_controller import FailsafeController
from core.baseline_controller import BaselineController
from agents.strategist import Strategist
from bms_mcp.tools import ToolContext

load_dotenv()


def load_policy(path="config/building_policy.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


class ProjectEnvelope:
    """AI-controlled instance: Strategist -> SentinelGate -> Failsafe, with
    self-correction on rejection (Feature 1) and PMV-aware gating."""

    def __init__(self, policy, control_log_path):
        self.policy = policy
        self.gate = SentinelGate(policy)
        self.failsafe = FailsafeController(policy)
        self.strategist = Strategist(model=policy["strategist"]["model"])

        self.cadence_steps = int(policy["strategist"]["cadence_steps"])
        self.call_timeout = float(policy["strategist"]["call_timeout_s"])
        self.correction_timeout = float(policy["strategist"]["correction_timeout_s"])
        self.forecast_hours = int(policy["strategist"]["forecast_lookahead_hours"])

        self.last_setpoint = 22.0
        self.current_reason = "Initializing..."
        self.current_source = "Holding"
        self.control_log_path = control_log_path

        self._bridge = None

    def attach_bridge(self, bridge):
        """
        Wires the live EnergyPlusBridge into the Strategist as a real
        mcp.tools.ToolContext (Feature 8). Until this runs, self.strategist
        .tool_context stays None and decide() silently falls back to
        single-shot JSON mode — tool-calling is fully implemented in
        agents/strategist.py and mcp/tools.py, but was never actually
        connected to a live bridge anywhere. This is that connection.
        """
        self._bridge = bridge
        carbon_profile = self.policy.get("carbon", {}).get("profile", "flat_medium")
        self.strategist.tool_context = ToolContext(
            bridge=bridge, policy=self.policy, carbon_profile=carbon_profile,
        )

    def log_step(self, step, t_in, t_out, humidity, setpoint, source, reason,
                 latency_s=None, tool_calls=None):
        kwh = round(self._bridge.cumulative_kwh, 4) if self._bridge else None
        with open(self.control_log_path, "a") as f:
            f.write(json.dumps({
                "step": step,
                "t_in": round(t_in, 2),
                "t_out": round(t_out, 2),
                "humidity": round(humidity, 1) if humidity is not None else None,
                "setpoint": setpoint,
                "source": source,
                "reason": reason,
                "cumulative_kwh": kwh,
                # Real, measured evidence (Section 1.1: "every number
                # measured, not asserted") that tool-calling actually ran
                # this step, not just that the code path exists.
                "latency_s": round(latency_s, 3) if latency_s is not None else None,
                "tool_calls": tool_calls or None,
            }) + "\n")

    def _get_carbon_intensity(self, step):
        cycle = (step // 30) % 3
        return ["Low", "Medium", "High"][cycle]

    def decide(self, t_in, t_out, humidity):
        """
        SENSE -> REASON -> VERIFY/SELF-CORRECT -> return (setpoint, source).
        Called once per EnergyPlus timestep by EnergyPlusBridge._callback.
        """
        step = self._bridge.step_counter if self._bridge else 0

        if step % self.cadence_steps != 0:
            self.current_source = "Holding"
            self.log_step(step, t_in, t_out, humidity, self.last_setpoint,
                          self.current_source, self.current_reason)
            return self.last_setpoint, self.current_source

        forecast = self._bridge.get_forward_weather(self.forecast_hours) if self._bridge else []
        carbon = self._get_carbon_intensity(step)

        # Accumulated across however many Strategist calls happen this
        # step (one on approval, two if a correction round runs), so the
        # log reflects the real end-to-end cost of the decision, not just
        # the first call.
        total_latency_s = 0.0
        all_tool_calls = []

        try:
            proposal = self.strategist.decide(
                t_in, t_out, forecast_window=forecast,
                carbon_intensity=carbon, timeout=self.call_timeout,
            )
            total_latency_s += self.strategist.last_latency_s or 0.0
            all_tool_calls += self.strategist.last_tool_calls
            approved, ccs, reason = self.gate.check(
                proposal["setpoint"], self.last_setpoint, proposal["confidence"],
                indoor_temp=t_in, humidity=humidity,
            )

            if approved:
                self.last_setpoint = proposal["setpoint"]
                self.current_reason = f"{reason} (CCS {ccs:.2f})"
                self.current_source = "AI"
            else:
                try:
                    corrected = self.strategist.decide(
                        t_in, t_out, forecast_window=forecast,
                        carbon_intensity=carbon, correction_context=reason,
                        timeout=self.correction_timeout,
                    )
                    total_latency_s += self.strategist.last_latency_s or 0.0
                    all_tool_calls += self.strategist.last_tool_calls
                    approved2, ccs2, reason2 = self.gate.check(
                        corrected["setpoint"], self.last_setpoint, corrected["confidence"],
                        indoor_temp=t_in, humidity=humidity,
                    )
                    if approved2:
                        self.last_setpoint = corrected["setpoint"]
                        self.current_reason = f"CORRECTED: {reason2} (CCS {ccs2:.2f})"
                        self.current_source = "AI (Corrected)"
                    else:
                        self.last_setpoint = self.failsafe.decide(t_in)
                        self.current_reason = f"GATE OVERRIDE after correction: {reason2}"
                        self.current_source = "FAILSAFE (gate override)"
                except Exception as e:
                    self.last_setpoint = self.failsafe.decide(t_in)
                    self.current_reason = f"REJECTED: {reason}. Correction attempt failed: {e}"
                    self.current_source = "FAILSAFE (gate override)"

        except Exception as e:
            self.last_setpoint = self.failsafe.decide(t_in)
            self.current_reason = f"Strategist unavailable ({e}); holding via rule-based failsafe."
            self.current_source = "FAILSAFE"

        print(f"[AI step {step}] in={t_in:.1f}C out={t_out:.1f}C -> "
              f"{self.last_setpoint:.1f}C ({self.current_source})")

        self.log_step(step, t_in, t_out, humidity, self.last_setpoint,
                      self.current_source, self.current_reason,
                      latency_s=total_latency_s or None,
                      tool_calls=all_tool_calls or None)
        return self.last_setpoint, self.current_source


class BaselineOrchestrator:
    """Baseline instance for Feature 2: fixed schedule, no AI, no gate.
    Exists purely as the honest comparison line for the overlay chart and
    the live energy-savings number."""

    def __init__(self, policy, control_log_path):
        self.controller = BaselineController(policy)
        self.control_log_path = control_log_path
        self._bridge = None

    def attach_bridge(self, bridge):
        self._bridge = bridge

    def log_step(self, step, t_in, t_out, humidity, setpoint):
        kwh = round(self._bridge.cumulative_kwh, 4) if self._bridge else None
        with open(self.control_log_path, "a") as f:
            f.write(json.dumps({
                "step": step,
                "t_in": round(t_in, 2),
                "t_out": round(t_out, 2),
                "humidity": round(humidity, 1) if humidity is not None else None,
                "setpoint": setpoint,
                "source": "Baseline",
                "reason": "Fixed schedule setpoint (no AI, no gate) - Feature 2 comparison instance.",
                "cumulative_kwh": kwh,
            }) + "\n")

    def decide(self, t_in, t_out, humidity):
        step = self._bridge.step_counter if self._bridge else 0
        setpoint = self.controller.decide(t_in)
        if step % 20 == 0:
            print(f"[Baseline step {step}] in={t_in:.1f}C out={t_out:.1f}C -> {setpoint:.1f}C")
        self.log_step(step, t_in, t_out, humidity, setpoint)
        return setpoint, "Baseline"


def _prep_run(policy, mode):
    control_log_path = policy["paths"][f"control_log_{mode}"]
    log_dir = os.path.dirname(control_log_path)
    os.makedirs(log_dir, exist_ok=True)
    if os.path.exists(control_log_path):
        os.remove(control_log_path)
    return control_log_path, log_dir


def main():
    parser = argparse.ArgumentParser(description="Project Envelope closed-loop orchestrator.")
    parser.add_argument("--baseline", action="store_true",
                         help="Run the fixed-schedule baseline instance instead of the AI instance.")
    args = parser.parse_args()

    policy = load_policy()
    mode = "baseline" if args.baseline else "ai"
    label = "Baseline" if mode == "baseline" else "AI"

    control_log_path, log_dir = _prep_run(policy, mode)

    controlled_idf = policy["paths"]["controlled_idf"]
    if not os.path.exists(controlled_idf):
        print(f"'{controlled_idf}' not found. Run `python scripts/patch_idf.py` "
              f"first - see that file's docstring for why baseline.idf alone "
              f"can't be actuated.")
        sys.exit(1)

    deadband_c = float(policy["comfort"]["deadband_c"])

    if mode == "ai":
        orchestrator = ProjectEnvelope(policy, control_log_path)
    else:
        orchestrator = BaselineOrchestrator(policy, control_log_path)

    bridge = EnergyPlusBridge(
        idf=controlled_idf,
        epw=policy["paths"]["epw"],
        output=log_dir,
        decision_callback=orchestrator.decide,
        label=label,
        deadband_c=deadband_c,
    )
    orchestrator.attach_bridge(bridge)

    print(f"=== Project Envelope: {label} instance starting. "
          f"Logging to {control_log_path} ===")
    bridge.run()


if __name__ == "__main__":
    main()