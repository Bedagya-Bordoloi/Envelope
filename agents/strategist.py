"""
agents/strategist.py

Real tool-calling agent (Feature 8 / Agentic Autonomy), not a text-only
prompt. When main.py attaches a live bridge, the Strategist gets an
mcp.tools.ToolContext and can call get_state/get_weather/get_carbon_intensity
to gather its own context, then commits its decision by calling set_hvac —
the same tool a human operator or any other MCP client would use.

Falls back to a single-shot JSON-only mode if no tool_context is attached
(e.g. quick standalone testing), so `python agents/strategist.py` still
works without a live simulation.

Also times its own end-to-end latency into self.last_latency_s, which
main.py logs as real control-step latency.

Fix vs. the previous version:

5. MALFORMED TOOL-CALL NAME BUG: llama-3.1-8b-instant occasionally emits
   a tool call whose name isn't a clean match for anything in
   TOOL_SCHEMAS — e.g. "get_state /" (a stray space/slash, most likely
   the model echoing pseudo-XML "<function=...>" syntax into what should
   have been a clean structured tool call). The previous version trusted
   tc.function.name unconditionally: it appended the malformed call into
   `messages` and tried to execute it via call_tool(). Because that
   malformed name stayed in the conversation history, the *next* API
   call in the same tool-round loop re-sent it to Groq — which then
   rejected the entire request with a 400 ("tool call validation failed:
   attempted to call tool 'get_state /' which was not in request.tools"),
   since that name was never one of the declared tools. This repeated on
   every subsequent round until MAX_TOOL_ROUNDS was hit.

   Fix: every tool_call name is checked against the known set of valid
   tool names (built from TOOL_SCHEMAS) before anything is appended to
   `messages` or executed. If ANY tool call in a turn has an invalid
   name, the whole turn is rejected immediately with a clean ValueError
   -- nothing malformed is ever written into the conversation history,
   so nothing malformed can be resent to Groq on a later round. That
   ValueError propagates up to main.py's existing exception handling,
   which already falls back to the correction round / rule-based
   failsafe -- no changes needed there.
"""

import os
import re
import json
import time
from groq import Groq

from bms_mcp.tools import TOOL_SCHEMAS, call_tool

MAX_TOOL_ROUNDS = 4  # hard cap so a confused model can't loop forever

# Built once from the declared schemas, so this file has a single source
# of truth for "what is a real tool name" instead of re-deriving it ad hoc.
_VALID_TOOL_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}

# Real tool names are plain identifiers (letters/digits/underscore only).
# Anything else -- stray whitespace, slashes, angle brackets, etc. -- is
# almost certainly leaked formatting from the model's raw output, not a
# genuine tool-call attempt.
_VALID_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_valid_tool_name(name):
    return bool(name) and _VALID_NAME_PATTERN.match(name) and name in _VALID_TOOL_NAMES


class Strategist:
    def __init__(self, model="llama-3.1-8b-instant", tool_context=None):
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set - check your .env file.")
        self.client = Groq(api_key=api_key)
        self.model = model
        self.tool_context = tool_context  # set by main.py after bridge attach
        self.last_latency_s = None
        self.last_tool_calls = []  # names of tools called this decision, for logging

    def _build_prompt(self, current_temp, outdoor_temp, forecast_window,
                       carbon_intensity, correction_context=None):
        forecast_str = ", ".join(f"{t:.1f}C" for t in forecast_window) if forecast_window else "n/a"
        prompt = f"""You are an AI Building Management System strategist.

Current indoor temp: {current_temp:.1f}C
Current outdoor temp: {outdoor_temp:.1f}C
Forward outdoor temp look-ahead (next hours, from known simulation weather, NOT a live forecast): {forecast_str}
Grid carbon intensity (simulated): {carbon_intensity}

Goal: minimize energy/carbon while keeping the zone within the comfort band.
You have tools available: get_state, get_weather, get_carbon_intensity (to look
up live values yourself if you want to double check), and set_hvac (to submit
your final proposed cooling setpoint, confidence, and reason).
Call set_hvac exactly once with your final decision.
Only call the tools listed above, using their exact names. Do not write
function-call syntax as plain text -- use the structured tool-calling
mechanism only."""

        if correction_context:
            prompt += (
                f"\n\nYour previous proposal was rejected by the safety gate: "
                f"\"{correction_context}\"\n"
                f"Propose a revised setpoint that directly addresses this reason, "
                f"then call set_hvac with the revised values."
            )
        return prompt

    def decide(self, current_temp, outdoor_temp, forecast_window=None,
               carbon_intensity="Medium", correction_context=None, timeout=8):
        """
        Returns {"setpoint": float, "confidence": float, "reason": str}.
        """
        start = time.perf_counter()
        self.last_tool_calls = []
        try:
            if self.tool_context is not None:
                result = self._decide_with_tools(
                    current_temp, outdoor_temp, forecast_window,
                    carbon_intensity, correction_context, timeout,
                )
            else:
                result = self._decide_json_only(
                    current_temp, outdoor_temp, forecast_window,
                    carbon_intensity, correction_context, timeout,
                )
            return result
        finally:
            self.last_latency_s = time.perf_counter() - start

    def _decide_with_tools(self, current_temp, outdoor_temp, forecast_window,
                            carbon_intensity, correction_context, timeout):
        prompt = self._build_prompt(current_temp, outdoor_temp, forecast_window or [],
                                     carbon_intensity, correction_context)
        messages = [{"role": "user", "content": prompt}]

        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.chat.completions.create(
                messages=messages,
                model=self.model,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                timeout=timeout,
            )
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            if not tool_calls:
                raise ValueError(
                    f"Strategist did not call set_hvac; got text instead: {msg.content!r}"
                )

            # THE FIX: validate every tool call's name BEFORE appending
            # anything to `messages` or executing anything. If even one
            # call in this turn is malformed, reject the whole turn here
            # -- nothing bad ever gets written into the conversation
            # history, so nothing bad can be resent to Groq on the next
            # round (which is what previously caused the repeating 400s).
            bad_names = [tc.function.name for tc in tool_calls
                         if not _is_valid_tool_name(tc.function.name)]
            if bad_names:
                raise ValueError(
                    f"Strategist emitted invalid tool call name(s) {bad_names!r} "
                    f"(not in declared tools {sorted(_VALID_TOOL_NAMES)}); "
                    f"likely malformed/echoed function-call syntax from the "
                    f"model. Discarding this turn rather than resending it."
                )

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in tool_calls],
            })

            for tc in tool_calls:
                name = tc.function.name
                self.last_tool_calls.append(name)
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if name == "set_hvac":
                    setpoint = float(args["setpoint_c"])
                    confidence = float(args.get("confidence", 0.8))
                    reason = str(args.get("reason", "")).strip() or "No reason given."
                    return {"setpoint": setpoint, "confidence": confidence, "reason": reason}

                # Any other (already-validated) tool: execute it and feed
                # the result back so the model can keep reasoning before
                # its final set_hvac call.
                try:
                    tool_result = call_tool(name, self.tool_context, **args)
                except Exception as e:
                    tool_result = {"error": str(e)}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_result),
                })

        raise ValueError(
            f"Strategist used {MAX_TOOL_ROUNDS} tool-call rounds without "
            f"submitting a final set_hvac decision."
        )

    def _decide_json_only(self, current_temp, outdoor_temp, forecast_window,
                           carbon_intensity, correction_context, timeout):
        """Fallback with no live ToolContext attached (e.g. standalone
        testing via `python agents/strategist.py`) - single-shot JSON,
        no tool-calling."""
        prompt = self._build_prompt(current_temp, outdoor_temp, forecast_window or [],
                                     carbon_intensity, correction_context)
        prompt += ('\n\n(No tools available in this mode.) Respond ONLY with JSON: '
                   '{"setpoint": <number>, "confidence": <0-1>, "reason": "<short>"}')
        resp = self.client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=self.model,
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        raw = resp.choices[0].message.content
        try:
            parsed = json.loads(raw)
            setpoint = float(parsed["setpoint"])
            confidence = float(parsed.get("confidence", 0.8))
            reason = str(parsed.get("reason", "")).strip() or "No reason given."
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Malformed Strategist output: {raw!r}") from e
        return {"setpoint": setpoint, "confidence": confidence, "reason": reason}


if __name__ == "__main__":
    brain = Strategist()  # no tool_context -> JSON-only fallback mode
    print(brain.decide(25.0, 30.0, forecast_window=[31.0, 32.5, 33.0], carbon_intensity="High"))
    print(f"latency: {brain.last_latency_s:.3f}s")