"""
mcp/tools.py

The four tools named in the problem statement: get_state, set_hvac,
get_weather, get_carbon_intensity.

Fix vs. the previous version: set_hvac now accepts optional confidence
and reason fields, not just setpoint_c. This is what makes real
tool-calling possible in agents/strategist.py (Feature 8) — the model
can call get_state/get_weather/get_carbon_intensity to gather its own
context, then call set_hvac as its final action, the same way it would
against any other tool-calling BMS integration. Previously set_hvac only
took a bare number, which wasn't enough for the gate to score the
proposal (it also needs a stated confidence).
"""

from dataclasses import dataclass


@dataclass
class ToolContext:
    """Shared handle to whatever the tools need to read/write."""
    bridge: object            # core.energyplus_bridge.EnergyPlusBridge instance
    policy: dict
    carbon_profile: str = "flat_medium"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def get_state(ctx: ToolContext) -> dict:
    """Return the latest sensed state from the live EnergyPlus instance."""
    b = ctx.bridge
    return {
        "indoor_temp_c": b.last_indoor_temp,
        "outdoor_temp_c": b.last_outdoor_temp,
        "humidity_pct": getattr(b, "last_humidity", None),
        "last_setpoint_c": b.last_setpoint,
        "cumulative_kwh": round(getattr(b, "cumulative_kwh", 0.0), 4),
        "step": b.step_counter,
    }


def set_hvac(ctx: ToolContext, setpoint_c: float, confidence: float = 0.8,
             reason: str = "") -> dict:
    """
    Stage a new HVAC cooling setpoint proposal for Sentinel Gate review.
    This is the Strategist's ACTION tool — calling it is how the model
    commits to a final decision for this cadence tick, the same way a
    human operator would submit a setpoint change request. It does NOT
    bypass the Sentinel Gate: main.py's control loop is what actually
    calls set_actuator_value after the gate approves it.
    """
    ctx.bridge.pending_setpoint = float(setpoint_c)
    return {
        "staged_setpoint_c": float(setpoint_c),
        "confidence": float(confidence),
        "reason": reason,
        "status": "pending_gate_review",
    }


def get_weather(ctx: ToolContext, hours: int = 3) -> dict:
    """
    Forward look-ahead using the simulation's OWN known future weather from
    the loaded .epw file — a deterministic proxy for a live forecast, not
    live data.
    """
    window = ctx.bridge.get_forward_weather(hours=hours)
    return {
        "hours": hours,
        "outdoor_temp_forecast_c": window,
        "source": "epw_lookahead (simulated future, not a live forecast)",
    }


def get_carbon_intensity(ctx: ToolContext) -> dict:
    """
    Explicitly simulated grid carbon-intensity signal, not a live API.
    """
    profile = ctx.carbon_profile
    step = getattr(ctx.bridge, "step_counter", 0)
    cycle = (step // 30) % 3
    level = ["Low", "Medium", "High"][cycle] if profile == "flat_medium" else "Medium"
    return {"carbon_intensity": level, "source": "simulated"}


# ---------------------------------------------------------------------------
# JSON-schema tool definitions (OpenAI/Groq function-calling compatible)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "Get the current sensed indoor/outdoor temperature, humidity, last setpoint, and cumulative energy use.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_hvac",
            "description": (
                "Submit your FINAL decision: a proposed HVAC cooling setpoint, "
                "your confidence in it, and a short reason. Call this exactly "
                "once, after you've gathered whatever state/weather/carbon "
                "context you need. This is how you commit to an action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "setpoint_c": {"type": "number", "description": "Proposed cooling setpoint in Celsius"},
                    "confidence": {"type": "number", "description": "Your confidence in this proposal, 0.0-1.0", "default": 0.8},
                    "reason": {"type": "string", "description": "One short sentence explaining the proposal"},
                },
                "required": ["setpoint_c"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the next N hours of outdoor temperature from the simulation's known future weather (a forecast proxy, not live data).",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "Look-ahead window in hours", "default": 3}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_carbon_intensity",
            "description": "Get the current simulated grid carbon-intensity level (Low/Medium/High).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

DISPATCH = {
    "get_state": get_state,
    "set_hvac": set_hvac,
    "get_weather": get_weather,
    "get_carbon_intensity": get_carbon_intensity,
}


def call_tool(name: str, ctx: ToolContext, **kwargs) -> dict:
    """Dispatch a tool call by name. Raises KeyError if the tool is unknown."""
    if name not in DISPATCH:
        raise KeyError(f"Unknown tool: {name}")
    return DISPATCH[name](ctx, **kwargs)