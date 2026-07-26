import argparse
import os
import sys
import json
import yaml
import time
from dotenv import load_dotenv

from core.energyplus_bridge import EnergyPlusBridge
from core.sentinel_gate import SentinelGate
from core.failsafe_controller import FailsafeController
from core.baseline_controller import BaselineController
from agents.strategist import Strategist
from bms_mcp.tools import ToolContext, carbon_intensity_level

load_dotenv()

def load_policy(path="config/building_policy.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)

class ProjectEnvelope:
    """AI-controlled instance with adaptive safety gating."""
    def __init__(self, policy, control_log_path):
        self.policy = policy
        self.control_log_path = control_log_path
        self.gate = SentinelGate(policy)
        self.failsafe = FailsafeController(policy)
        self.strategist = Strategist(model=policy["strategist"]["model"])
        self.cadence_steps = policy["strategist"]["cadence_steps"]
        self.last_setpoint = 22.0
        self._bridge = None

    def attach_bridge(self, bridge):
        self._bridge = bridge
        self.strategist.tool_context = ToolContext(bridge=bridge, policy=self.policy)

    def _get_carbon_intensity(self, step):
        return carbon_intensity_level(step, self.policy)

    def decide(self, t_in, t_out, humidity):
        step = self._bridge.step_counter
        if step % self.cadence_steps != 0: 
            return self.last_setpoint, "Holding"

        forecast = self._bridge.get_forward_weather(3)
        carbon = self._get_carbon_intensity(step)
        
        source = "FAILSAFE"
        reason = "System Init"
        final_ccs = None
        lookahead_triggered = False

        try:
            # 1. AI Reasoning
            proposal = self.strategist.decide(t_in, t_out, forecast, carbon)
            lookahead_triggered = getattr(self.strategist, "last_lookahead_triggered", False)

            # 2. Gating
            outcome, ccs, reason, _ = self.gate.check(
                proposal["setpoint"], self.last_setpoint, proposal["confidence"],
                t_in, humidity, t_out
            )
            final_ccs = ccs

            if outcome == "APPROVED":
                self.last_setpoint, source = proposal["setpoint"], "AI"
            elif outcome == "HOLD":
                source = "AI (Stabilized)"
                final_ccs = 1.0
            else:
                # 3. Correction
                corrected = self.strategist.decide(t_in, t_out, forecast, carbon, correction_context=reason)
                outcome2, ccs2, reason2, _ = self.gate.check(
                    corrected["setpoint"], self.last_setpoint, corrected["confidence"],
                    t_in, humidity, t_out
                )
                final_ccs = ccs2
                if outcome2 == "APPROVED":
                    self.last_setpoint, source, reason = corrected["setpoint"], "AI (Corrected)", reason2
                else:
                    self.last_setpoint, source, reason = self.failsafe.decide(t_in), "FAILSAFE", f"Gate Override: {reason2}"
        except Exception as e:
            self.last_setpoint, source, reason = self.failsafe.decide(t_in), "FAILSAFE", f"Error: {e}"

        # 4. Logging
        log_entry = {
            "step": int(step), "t_in": round(t_in, 2), "t_out": round(t_out, 2),
            "setpoint": float(self.last_setpoint), "source": source, "reason": reason,
            "ccs": round(final_ccs, 3) if final_ccs is not None else None,
            "lookahead_triggered": lookahead_triggered,
            "cumulative_kwh": round(self._bridge.cumulative_kwh, 4)
        }
        with open(self.control_log_path, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
            
        return self.last_setpoint, source

class BaselineOrchestrator:
    """Non-AI comparison instance."""
    def __init__(self, policy, control_log_path):
        self.controller = BaselineController(policy)
        self.control_log_path = control_log_path
        self._bridge = None
    def attach_bridge(self, bridge):
        self._bridge = bridge
    def decide(self, t_in, t_out, humidity):
        step = self._bridge.step_counter
        setpoint = self.controller.decide(t_in)
        with open(self.control_log_path, "a") as f:
            f.write(json.dumps({"step": int(step), "t_in": t_in, "t_out": t_out, "setpoint": setpoint, 
                               "source": "Baseline", "reason": "Standard Schedule", 
                               "cumulative_kwh": self._bridge.cumulative_kwh}) + "\n")
        return setpoint, "Baseline"

def main():
    policy = load_policy()
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    
    mode = "baseline" if args.baseline else "ai"
    log_path = f"logs/{mode}/control_log.jsonl"
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    
    orchestrator = ProjectEnvelope(policy, log_path) if mode == "ai" else BaselineOrchestrator(policy, log_path)
    
    bridge = EnergyPlusBridge(
        idf="models/controlled.idf", 
        epw=policy["paths"]["epw"], 
        output=f"logs/{mode}", 
        decision_callback=orchestrator.decide
    )
    orchestrator.attach_bridge(bridge)
    bridge.run()

if __name__ == "__main__":
    main()