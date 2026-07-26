import os
import json
import time
import re
from groq import Groq
from bms_mcp.tools import TOOL_SCHEMAS, call_tool

MAX_INTERNAL_RETRIES = 2 

class Strategist:
    def __init__(self, model="llama-3.1-8b-instant", tool_context=None):
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set - check your .env file.")
        
        self.client = Groq(api_key=api_key)
        self.model = model
        self.tool_context = tool_context
        self.last_latency_s = 0
        self.last_tool_calls = []

    def _build_prompt(self, t_in, t_out, forecast, carbon, internal_error=None):
        """Cognitive Strategy Engine: Determines the financial path based on seasonal physics."""
        
        # PHYSICAL LOGIC ENGINE
        if t_out < 14:
            season, advice = "WINTER", "Lowering temp saves heating. TARGET: 21.0C to 21.3C."
        elif t_out > 22:
            season, advice = "SUMMER", "PROFIT RULE: Raising temp saves AC energy. Aim for 24.5C to 25.5C."
            target_hint = "Propose ~25.0C to turn off the AC and beat the 22.0C baseline."
        else:
            # The 'Shoulder Season' fix: Match baseline to stop energy-wasting transitions
            season, advice = "SHOULDER (Mild)", "MATCH BASELINE (22.0C) to avoid wasting energy on setpoint jitter."

        prompt = f"""You are the 'Eco-Loop' BMS Strategist. GOAL: Beat 22.0C Baseline profit.

[SITUATION] 
- Outdoor: {t_out:.1f}C | Indoor: {t_in:.1f}C | Season: {season}
- Strategy: {advice}
- Stability: Do not change setpoint unless weather or carbon trends shift significantly.

[TASK]
Propose a setpoint that maximizes profit while ensuring 100% Gate Approval. 
You MUST call the 'set_hvac' tool.

IMPORTANT: Output valid JSON tool calls only. Do not provide setpoints as plain text."""

        if internal_error:
            prompt += f"\n\n[RETRY ALERT]: Your previous attempt had an error: {internal_error}. Use correct keys: 'setpoint_c', 'confidence', 'reason'."
            
        return prompt

    def decide(self, t_in, t_out, forecast=None, carbon="Medium", correction_context=None, timeout=10):
        """Requests a decision with forced tool use and defensive parsing."""
        start = time.perf_counter()
        self.last_tool_calls = []
        current_error = None

        for attempt in range(MAX_INTERNAL_RETRIES):
            try:
                prompt = self._build_prompt(t_in, t_out, forecast, carbon, internal_error=current_error)
                
                resp = self.client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model=self.model,
                    tools=TOOL_SCHEMAS,
                    # FORCED TOOL CHOICE: Prevents Error 400 by making 'set_hvac' mandatory
                    tool_choice={"type": "function", "function": {"name": "set_hvac"}},
                    timeout=timeout
                )
                
                message = resp.choices[0].message
                if not message.tool_calls:
                    current_error = "No tool call detected."
                    continue

                self.last_tool_calls = [tc.function.name for tc in message.tool_calls]
                args = json.loads(message.tool_calls[0].function.arguments)
                
                # DEFENSIVE PARSING: Accept 'setpoint' or 'setpoint_c'
                val = args.get("setpoint_c") or args.get("setpoint")
                
                if val is not None:
                    return {
                        "setpoint": float(val),
                        "confidence": float(args.get("confidence", 0.9)),
                        "reason": str(args.get("reason", "Adaptive optimization."))
                    }
                else:
                    current_error = "Missing 'setpoint_c' key."
                    
            except Exception as e:
                current_error = str(e)
            
            time.sleep(0.5)

        # Smart Policy-Driven Fallback (Reduces 'Failsafe' losses)
        fallback = 21.2 if t_out < 15 else (24.5 if t_out > 24 else 22.0)
        return {"setpoint": fallback, "confidence": 0.5, "reason": "System-enforced physical target."}

    def _check_lookahead(self, outdoor_temp, forecast_window):
        self.last_lookahead_triggered = False
        return None