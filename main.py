"""
main.py - Project Envelope: AI-Gated BMS Orchestrator
Full Adaptive Version with Baseline Support
"""

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

class ProjectEnvelope:
    """AI-controlled instance: Strategist -> SentinelGate -> Failsafe."""

    def __init__(self, policy, control_log_path):
        self.policy, self.control_log_path = policy, control_log_path
        self.gate = SentinelGate(policy)
        self.failsafe = FailsafeController(policy)
        self.strategist = Strategist(model=policy["strategist"]["model"])
        self.cadence_steps = policy["strategist"]["cadence_steps"]
        self.last_setpoint = 22.0
        self._bridge = None

    def attach_bridge(self, bridge):
        self._bridge = bridge
        self.strategist.tool_context = ToolContext(bridge=bridge, policy=self.policy)

    def decide(self, t_in, t_out, humidity):
        step = self._bridge.step_counter
        if step % self.cadence_steps != 0: 
            return self.last_setpoint, "Holding"

        forecast = self._bridge.get_forward_weather(3)
        carbon = carbon_intensity_level(step, self.policy)

        try:
            # 1. AI Reasoning
            proposal = self.strategist.decide(t_in, t_out, forecast, carbon)
            
            # 2. Gating check
            outcome, ccs, reason, _ = self.gate.check(
                proposal["setpoint"], self.last_setpoint, proposal["confidence"],
                t_in, humidity, t_out
            )

            if outcome == "APPROVED":
                self.last_setpoint, source = proposal["setpoint"], "AI"
            elif outcome == "HOLD":
                source = "AI (Stabilized)"
            else:
                # 3. Correction attempt
                corrected = self.strategist.decide(t_in, t_out, forecast, carbon, correction_context=reason)
                outcome2, ccs2, reason2, _ = self.gate.check(
                    corrected["setpoint"], self.last_setpoint, corrected["confidence"],
                    t_in, humidity, t_out
                )
                if outcome2 == "APPROVED":
                    self.last_setpoint, source, reason = corrected["setpoint"], "AI (Corrected)", reason2
                else:
                    self.last_setpoint, source, reason = self.failsafe.decide(t_in), "FAILSAFE", "Safety Override"
        except Exception as e:
            self.last_setpoint, source, reason = self.failsafe.decide(t_in), "FAILSAFE", f"Error: {e}"

        # Sync logging
        with open(self.control_log_path, "a") as f:
            f.write(json.dumps({
                "step": step, "t_in": t_in, "t_out": t_out, 
                "setpoint": self.last_setpoint, "source": source, 
                "reason": reason, "cumulative_kwh": self._bridge.cumulative_kwh
            }) + "\n")
            
        return self.last_setpoint, source

class BaselineOrchestrator:
    """Non-AI instance: uses a fixed schedule for comparison."""

    def __init__(self, policy, control_log_path):
        self.controller = BaselineController(policy)
        self.control_log_path = control_log_path
        self._bridge = None

    def attach_bridge(self, bridge):
        self._bridge = bridge

    def decide(self, t_in, t_out, humidity):
        step = self._bridge.step_counter
        setpoint = self.controller.decide(t_in)
        
        # Log baseline every step for accurate comparison
        with open(self.control_log_path, "a") as f:
            f.write(json.dumps({
                "step": step, "t_in": t_in, "t_out": t_out, 
                "setpoint": setpoint, "source": "Baseline", 
                "reason": "Standard Schedule", "cumulative_kwh": self._bridge.cumulative_kwh
            }) + "\n")
            
        return setpoint, "Baseline"

def main():
    # Load configuration
    try:
        with open("config/building_policy.yaml", "r") as f:
            policy = yaml.safe_load(f)
    except Exception as e:
        print(f"Error loading YAML: {e}")
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true", help="Run baseline schedule.")
    args = parser.parse_args()
    
    mode = "baseline" if args.baseline else "ai"
    log_path = f"logs/{mode}/control_log.jsonl"
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    
    # Initialize correct orchestrator
    if mode == "ai":
        orchestrator = ProjectEnvelope(policy, log_path)
    else:
        orchestrator = BaselineOrchestrator(policy, log_path)
    
    # Setup Simulation Bridge
    bridge = EnergyPlusBridge(
        idf="models/controlled.idf", 
        epw="models/baseline.epw", 
        output=f"logs/{mode}", 
        decision_callback=orchestrator.decide
    )
    
    orchestrator.attach_bridge(bridge)
    
    print(f"=== Project Envelope: {mode.upper()} Instance Starting ===")
    bridge.run()

if __name__ == "__main__":
    main()