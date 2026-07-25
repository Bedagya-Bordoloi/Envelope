"""
main.py - Project Envelope closed-loop orchestrator.

Adaptive System Update: Implements seasonal clothing insulation (clo) 
to ensure comfort safety while maximizing efficiency in all weather conditions.
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
from bms_mcp.tools import ToolContext, carbon_intensity_level

load_dotenv()


def load_policy(path="config/building_policy.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


class ProjectEnvelope:
    """AI-controlled instance: Strategist -> SentinelGate -> Failsafe, with
    self-correction on rejection (Feature 1) and PMV-aware, carbon-weighted
    gating."""

    def __init__(self, policy, control_log_path):
        self.policy = policy
        self.gate = SentinelGate(policy)
        self.failsafe = FailsafeController(policy)

        lookahead_cfg = policy["strategist"].get("lookahead", {})
        self.strategist = Strategist(
            model=policy["strategist"]["model"],
            lookahead_swing_threshold_c=lookahead_cfg.get("swing_threshold_c", 4.0),
        )

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
        self._bridge = bridge
        carbon_profile = self.policy.get("carbon", {}).get("profile", "flat_medium")
        self.strategist.tool_context = ToolContext(
            bridge=bridge, policy=self.policy, carbon_profile=carbon_profile,
        )

    def log_step(self, step, t_in, t_out, humidity, setpoint, source, reason,
                 latency_s=None, tool_calls=None, lookahead_triggered=None,
                 ccs=None, carbon_intensity=None):
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
                "latency_s": round(latency_s, 3) if latency_s is not None else None,
                "tool_calls": tool_calls or None,
                "lookahead_triggered": lookahead_triggered,
                "ccs": ccs,
                "carbon_intensity": carbon_intensity,
            }) + "\n")

    def _get_carbon_intensity(self, step):
        return carbon_intensity_level(step, self.policy)

    def decide(self, t_in, t_out, humidity):
        step = self._bridge.step_counter if self._bridge else 0

        # 1. CADENCE CHECK
        if step % self.cadence_steps != 0:
            self.current_source = "Holding"
            self.log_step(step, t_in, t_out, humidity, self.last_setpoint,
                          self.current_source, self.current_reason)
            return self.last_setpoint, self.current_source

        # 2. DATA GATHERING
        forecast = self._bridge.get_forward_weather(self.forecast_hours) if self._bridge else []
        carbon = self._get_carbon_intensity(step)
        
        # --- ADAPTIVE PHYSICS (3-Stage Clothing Model) ---
        if t_out < 10.0:
            clo_level = 1.0  # Winter
        elif t_out < 22.0:
            clo_level = 0.7  # Shoulder (Spring/Fall)
        else:
            clo_level = 0.5  # Summer

        total_latency_s = 0.0
        all_tool_calls = []
        lookahead_triggered = None
        final_ccs = None

        try:
            # 3. AI REASONING
            proposal = self.strategist.decide(
                t_in, t_out, forecast_window=forecast,
                carbon_intensity=carbon, timeout=self.call_timeout,
            )
            total_latency_s += self.strategist.last_latency_s or 0.0
            all_tool_calls += self.strategist.last_tool_calls
            lookahead_triggered = getattr(self.strategist, "last_lookahead_triggered", None)

            # 4. GATE VERIFICATION (Passing dynamic clo_level)
            approved, ccs, reason, _breakdown = self.gate.check(
                proposal["setpoint"], self.last_setpoint, proposal["confidence"],
                indoor_temp=t_in, humidity=humidity, carbon_intensity=carbon,
                extra_values={"clo": clo_level}
            )
            final_ccs = ccs

            if approved:
                self.last_setpoint = proposal["setpoint"]
                self.current_reason = f"{reason} (CCS {ccs:.2f})"
                self.current_source = "AI"
            else:
                try:
                    # 5. SELF-CORRECTION LOOP
                    corrected = self.strategist.decide(
                        t_in, t_out, forecast_window=forecast,
                        carbon_intensity=carbon, correction_context=reason,
                        timeout=self.correction_timeout,
                    )
                    total_latency_s += self.strategist.last_latency_s or 0.0
                    all_tool_calls += self.strategist.last_tool_calls

                    approved2, ccs2, reason2, _breakdown2 = self.gate.check(
                        corrected["setpoint"], self.last_setpoint, corrected["confidence"],
                        indoor_temp=t_in, humidity=humidity, carbon_intensity=carbon,
                        extra_values={"clo": clo_level}
                    )
                    final_ccs = ccs2

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
                      tool_calls=all_tool_calls or None,
                      lookahead_triggered=lookahead_triggered,
                      ccs=final_ccs, carbon_intensity=carbon)
        return self.last_setpoint, self.current_source


class BaselineOrchestrator:
    """Baseline instance for Feature 2: fixed schedule, no AI, no gate."""

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
        print(f"'{controlled_idf}' not found. Run `python scripts/patch_idf.py` first.")
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