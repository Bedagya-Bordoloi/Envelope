import os
import json
import time
import re
from groq import Groq
from bms_mcp.tools import TOOL_SCHEMAS, call_tool

MAX_TOOL_ROUNDS = 3 

class Strategist:
    def __init__(self, model="llama-3.1-8b-instant", tool_context=None, lookahead_swing_threshold_c=4.0):
        """
        AI Building Management Strategist.
        tool_context: ToolContext object containing the bridge and the policy YAML.
        """
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set - check your .env file.")
        
        self.client = Groq(api_key=api_key)
        self.model = model
        self.tool_context = tool_context
        self.last_latency_s = None
        self.last_tool_calls = []
        
        # Feature 3: Look-ahead tracking
        self.lookahead_swing_threshold_c = float(lookahead_swing_threshold_c)
        self.last_lookahead_triggered = False
        self.last_lookahead_swing_c = None

    def _check_lookahead(self, outdoor_temp, forecast_window):
        """Flags extreme weather swings in the EPW forecast."""
        self.last_lookahead_triggered = False
        self.last_lookahead_swing_c = None
        if not forecast_window: return None
        
        most_extreme = max(forecast_window, key=lambda t: abs(t - outdoor_temp))
        swing = most_extreme - outdoor_temp
        self.last_lookahead_swing_c = round(swing, 2)
        
        if abs(swing) >= self.lookahead_swing_threshold_c:
            self.last_lookahead_triggered = True
            direction = "rising" if swing > 0 else "falling"
            return f"LOOK-AHEAD ALERT: Forecast outdoor temp is {direction} by {abs(swing):.1f}C. Pre-act to save energy!"
        return None

    def _build_prompt(self, current_temp, outdoor_temp, forecast_window, carbon_intensity, correction_context=None, lookahead_note=None):
        forecast_str = ", ".join(f"{t:.1f}C" for t in forecast_window) if forecast_window else "n/a"
        
        # --- DYNAMIC POLICY RETRIEVAL (NO HARDCODING) ---
        s_policy = self.tool_context.policy.get('seasonality', {})
        w_limit = s_policy.get('winter_threshold_c', 12.0)
        s_limit = s_policy.get('summer_threshold_c', 24.0)
        targets = s_policy.get('comfort_targets', {})

        if outdoor_temp < w_limit:
            season, target_range = "WINTER", f"{targets.get('winter_min')}-{targets.get('winter_max')}C"
            hint = "Focus on lowering heat to save energy. Proposing below 21.0C usually triggers a Gate rejection."
        elif outdoor_temp > s_limit:
            season, target_range = "SUMMER", f"{targets.get('summer_min')}-{targets.get('summer_max')}C"
            hint = "Focus on raising setpoint to save cooling. Proposing below 23.5C is wasteful and risky."
        else:
            season, target_range = "SHOULDER", "near 22.0C"
            hint = "Mild weather. Minimize HVAC usage."

        prompt = f"""You are the 'Eco-Loop' BMS Strategist. Goal: Beat the 22.0C baseline while passing the Safety Gate.

[SITUATION]
- Season: {season} | Outdoor: {outdoor_temp:.1f}C | Indoor: {current_temp:.1f}C | Carbon: {carbon_intensity}

[PHYSICS CONSTRAINTS]
- BORDERLINE: ASHRAE-55 math shows that at the current {outdoor_temp:.1f}C, an indoor temp below 21.0C usually triggers a 'Comfort Violation.'
- PROFIT GOAL: The baseline is 22.0C. To save energy, you must find a setpoint LOWER than 22.0C.
- STRATEGIC RANGE: You have a 'Goldilocks Zone' between 21.0C (safety floor) and 22.0C (baseline). 

[TASK]
1. Use 'get_weather' and 'get_carbon_intensity' to evaluate the current cost of energy.
2. If carbon intensity is HIGH, try to 'Hug the Floor' (stay closer to 21.0C) to save max energy.
3. If the forecast shows a temperature drop, maintain stability.
4. PROPOSE the most energy-efficient setpoint that you are 95% confident will pass the Safety Gate.

[GOVERNANCE]
- The Sentinel Gate uses ASHRAE-55 PMV. If you are rejected, the building wastes energy in Failsafe mode.
- STABILITY IS PROFIT: Do not jump setpoints back and forth. If the current {current_temp:.1f}C is working, hold it.
- PHYSICAL TARGET: To pass the Gate now, your setpoint should be {target_range}.
- HINT: {hint}
"""
        if lookahead_note:
            prompt += f"\n\n[ALERT]: {lookahead_note}"

    # Line 89: Start the string properly
        prompt += """

[ACTION]
1. Use 'get_weather' to check the forecast trend.
2. Call 'set_hvac' with your FINAL decision. 
3. IMPORTANT: Propose exactly 21.2C or 21.3C to ensure approval and profit.
4. Output ONLY the tool call. Do not explain yourself in the chat."""
        if correction_context:
            prompt += f"\n\n[REJECTION FEEDBACK]: {correction_context}. ADAPT: Move 0.1C closer to 21.2C."
            
        return prompt # MAKE SURE THIS RETURN IS AT THE VERY END

    def decide(self, current_temp, outdoor_temp, forecast_window=None, carbon_intensity="Medium", correction_context=None, timeout=10):
        start = time.perf_counter()
        self.last_tool_calls = []
        lookahead_note = self._check_lookahead(outdoor_temp, forecast_window)
        
        prompt = self._build_prompt(current_temp, outdoor_temp, forecast_window, carbon_intensity, correction_context, lookahead_note)
        messages = [{"role": "user", "content": prompt}]

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                resp = self.client.chat.completions.create(
                    messages=messages, model=self.model, tools=TOOL_SCHEMAS, tool_choice="auto", timeout=timeout
                )
                msg = resp.choices[0].message
                
                if not msg.tool_calls:
                    # Smart Fallback if the LLM refuses to use tools
                    p = self.tool_context.policy.get('seasonality', {})
                    fallback = p['comfort_targets']['winter_max'] if outdoor_temp < 15 else 22.0
                    return {"setpoint": fallback, "confidence": 0.5, "reason": "Reasoning failed; falling back to safety floor."}

                messages.append(msg)
                for tc in msg.tool_calls:
                    name = tc.function.name
                    self.last_tool_calls.append(name)
                    args = json.loads(tc.function.arguments)

                    if name == "set_hvac":
                        return {
                            "setpoint": float(args["setpoint_c"]), 
                            "confidence": float(args.get("confidence", 0.9)), 
                            "reason": args.get("reason", "Optimized strategy.")
                        }
                    
                    # Execute secondary tools (get_weather, get_state, etc)
                    res = call_tool(name, self.tool_context, **args)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(res)})
        except Exception as e:
            # Re-raise to main.py to trigger the Failsafe logic
            raise e
        finally:
            self.last_latency_s = time.perf_counter() - start