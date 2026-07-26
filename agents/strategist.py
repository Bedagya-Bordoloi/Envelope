import os, json, time
from groq import Groq
from bms_mcp.tools import TOOL_SCHEMAS, call_tool

class Strategist:
    def __init__(self, model="llama-3.1-8b-instant", tool_context=None, lookahead_swing_threshold_c=4.0):
        self.client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        self.model, self.tool_context = model, tool_context
        self.last_latency_s, self.last_tool_calls = 0, []

    def _build_prompt(self, t_in, t_out, forecast, carbon, correction=None):
        # High-level Physical Reasoning
        if t_out < 12: season, strategy = "WINTER", "Lowering temp saves heating. Aim for 21.0C - 21.3C."
        elif t_out > 24: season, strategy = "SUMMER", "Raising temp saves cooling. Aim for 24.5C - 25.5C."
        else: season, strategy = "SHOULDER", "Stay stable at 22.0C."

        prompt = f"""You are the 'Eco-Loop' AI BMS. GOAL: Beat 22.0C baseline.
[PHYSICS] Season: {season} | Outdoor: {t_out}C | Indoor: {t_in}C
[STRATEGY] {strategy} Stability is key. Do not hunt. 
[TASK] Analyze forecast {forecast} and carbon {carbon}. Propose an efficient setpoint.
[ACTION] Call 'set_hvac' with setpoint, confidence, and reason. Output ONLY the tool call."""
        if correction: prompt += f"\n\n[RETRY]: {correction}. Adjust to be safer."
        return prompt

    def decide(self, t_in, t_out, forecast=None, carbon="Medium", correction_context=None, timeout=10):
        start = time.perf_counter()
        self.last_tool_calls = []
        prompt = self._build_prompt(t_in, t_out, forecast, carbon, correction_context)
        try:
            resp = self.client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=self.model, tools=TOOL_SCHEMAS, tool_choice="auto", timeout=timeout
            )
            tc = resp.choices[0].message.tool_calls
            if tc:
                args = json.loads(tc[0].function.arguments)
                return {"setpoint": float(args["setpoint_c"]), "confidence": 0.9, "reason": args.get("reason", "Optimized")}
            return {"setpoint": 22.0, "confidence": 0.5, "reason": "Defaulting to safety."}
        finally:
            self.last_latency_s = time.perf_counter() - start