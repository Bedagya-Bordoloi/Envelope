#ARCHITECTURE.md — Project Envelope

## 1. System overview

Four roles, three processes:
┌─────────────────────┐        ┌─────────────────────┐
│  AI instance         │        │  Baseline instance   │
│  python main.py       │        │  python main.py       │
│                       │        │  --baseline           │
│  EnergyPlusBridge     │        │  EnergyPlusBridge     │
│  → Strategist (Groq)  │        │  → BaselineController │
│  → SentinelGate       │        │    (fixed setpoint,   │
│  → FailsafeController │        │     no AI, no gate)   │
│  writes logs/ai/       │        │  writes logs/baseline/│
│  control_log.jsonl     │        │  control_log.jsonl     │
└──────────┬────────────┘        └──────────┬────────────┘
│                                  │
└──────────────┬───────────────────┘
▼
┌─────────────────────────┐
│  streamlit run ui/app.py │
│  polls both logs every    │
│  2s, renders overlay      │
└─────────────────────────┘
code
Code
The AI and baseline instances are two separate OS processes, not two states in one process, because EnergyPlus's runtime API isn't documented as safe for two concurrent states in one interpreter. Two processes sidesteps that risk entirely.

## 2. The closed loop (per EnergyPlus timestep)

`core/energyplus_bridge.py` registers `_callback` on `callback_begin_system_timestep_before_predictor`. Every timestep:

1. Reads indoor temp, outdoor temp, zone relative humidity via `pyenergyplus.api.exchange.get_variable_value`.
2. Reads cumulative heating/cooling meter values (Joules → kWh) for the energy-overlay metric.
3. Calls `decision_callback(t_in, t_out, humidity)` — this is `ProjectEnvelope.decide()` or `BaselineOrchestrator.decide()`, injected from `main.py` so the bridge has no dependency on orchestration logic.
4. Actuates **both** the cooling setpoint and the derived heating setpoint (`heating = target - deadband_c/2`) via `set_actuator_value`, so the loop has a real physical effect regardless of whether the zone is heating- or cooling-dominated at that point in the run.

`decide()` only runs the full Strategist → Gate pipeline every `cadence_steps` (60, from `building_policy.yaml`) timesteps; in between it holds the last approved setpoint. This bounds Groq API call volume over a multi-hour run.

## 3. Tool-calling (Feature 8 / Agentic Autonomy)

The Strategist is a real tool-calling agent, not a text-only JSON prompt.

**Tools** (`mcp/tools.py`): `get_state`, `get_weather`, `get_carbon_intensity`, `set_hvac`. Each is a plain Python function taking a `ToolContext` (holds a reference to the live `EnergyPlusBridge`, the policy dict, and the carbon profile) plus JSON-schema `TOOL_SCHEMAS` compatible with Groq/OpenAI function-calling.

**Wiring** (`main.py`): `ProjectEnvelope.attach_bridge()` builds the `ToolContext` once the bridge exists and assigns it to `self.strategist.tool_context`. Before this was wired, the Strategist silently ran in a JSON-only fallback mode every step — `mcp/server.py`'s own docstring claimed tool-calling was "the path actually used," which wasn't true until this fix.

**Round trip** (`agents/strategist.py._decide_with_tools`):
1. Send the prompt (current state + forecast + carbon signal already inlined, so the model isn't forced to call tools just to get started) with `tools=TOOL_SCHEMAS, tool_choice="auto"`.
2. If the model calls `get_state`/`get_weather`/`get_carbon_intensity`, execute via `call_tool()` and feed the JSON result back as a `role: tool` message.
3. If the model calls `set_hvac`, that's the terminal action — extract `setpoint_c`/`confidence`/`reason` and return immediately.
4. Hard cap of `MAX_TOOL_ROUNDS = 4` so a confused model can't loop forever; if it exhausts the cap without calling `set_hvac`, `decide()` raises and `main.py` routes to the failsafe.

`self.strategist.last_tool_calls` records every tool name invoked that step; `main.py` appends this list to the control log so tool-calling usage is directly auditable, not just claimed in prose.

## 4. Prompt & latency management

- **Cadence**: Strategist runs every 60 timesteps, not every timestep — bounds API cost/rate-limit exposure over an extended run.
- **Timeouts**: `call_timeout_s: 8` for the initial proposal, `correction_timeout_s: 5` for the correction retry (tighter, since a correction should be a smaller/faster reasoning step). Both are `groq.Client` per-call timeouts, not just soft heuristics.
- **Latency measurement**: `Strategist.decide()` wraps the entire call (including all tool round-trips) in `time.perf_counter()` and stores the result in `self.last_latency_s`. `main.py` sums this across the initial call and any correction retry into `total_latency_s` and writes it to `control_log.jsonl` as `latency_s` — real measured per-decision latency, not asserted.
- **Failure handling**: any exception from the Strategist (timeout, malformed tool args, rate limit, network error) is caught in `main.py` and routes to `FailsafeController`, which is pure local rule-based logic with zero network dependency. The building never blocks on a hung API call.

## 5. Self-correction loop (Feature 1)

1. Strategist proposes → `SentinelGate.check()` scores it.
2. If rejected, the gate's plain-language reason string is passed back into `Strategist.decide(correction_context=reason)`, which appends it verbatim to the next prompt: *"Your previous proposal was rejected by the safety gate: '{reason}'. Propose a revised setpoint that directly addresses this reason."*
3. The corrected proposal is scored again. If it now passes, `source = "AI (Corrected)"`. If it fails a second time, `main.py` does **not** retry a third time (bounded, per the blueprint's stated single-step-correction limitation) — it falls through to `FailsafeController` with `source = "FAILSAFE (gate override)"`.

This is a single corrective round, not open-ended negotiation with the gate — a deliberate latency/cost bound, not an oversight.

## 6. Sentinel Gate — Control Confidence Score (CCS)

`core/sentinel_gate.py` computes a weighted score in `[0, 1]`:
CCS = 0.40 × (1 − violation_severity)
+ 0.25 × (1 − rate_penalty)
+ 0.20 × llm_confidence
+ 0.15 × (1 − override_rate)
code
Code
- `violation_severity` = **worse of** a hard degC-bounds check and an ASHRAE-55 PMV check (see §7) — either alone can trigger rejection, so a setpoint can be technically in-bounds and still rejected for making the zone uncomfortable.
- `rate_penalty` = proposed change vs. `max_delta_c_per_step`, normalized.
- `llm_confidence` = the Strategist's self-reported confidence, taken from its `set_hvac` call.
- `override_rate` = fraction of the last 50 decisions that were rejected — a Strategist on a losing streak gets scored more conservatively.

Threshold is `ccs_threshold: 0.70` (`config/building_policy.yaml`) — a starting prior stated as such, not months of calibration data.

## 7. ASHRAE-55 PMV comfort model

`core/comfort.py` calls `pythermalcomfort.models.pmv_ppd_iso()` with indoor dry-bulb temp, sensed relative humidity, and fixed assumptions for air velocity (0.1 m/s), metabolic rate (1.2 met, office work), and clothing (0.7 clo). `sentinel_gate.py` folds `|PMV| > 0.5` (the ASHRAE-55 "acceptable" band boundary) into the same violation-severity term as the raw degC bounds, scored against the **currently sensed** indoor state rather than the proposed setpoint (which hasn't been realized by the building yet) — this catches a setpoint that's technically fine but isn't actually working.

## 8. Live counterfactual overlay (Feature 2)

Both instances start from the same `.idf`/`.epw`. The baseline instance runs `BaselineController` — a fixed schedule setpoint, no Strategist, no gate — as the honest "what would this building do without the AI" comparison line. `ui/app.py` polls both `control_log.jsonl` files every 2 seconds and plots indoor temp, setpoint, and cumulative kWh for both on the same chart, plus a live `(baseline_kwh − ai_kwh) / baseline_kwh` savings percentage.

## 9. Lengthy log handling

- **Writer side**: `main.py` opens `control_log.jsonl` in append mode (`"a"`) once per control step and writes one JSON object per line — never rewrites the whole file, so write cost stays O(1) per step regardless of run length.
- **Reader side**: `ui/app.py` currently re-reads the **entire** log file on every 2-second poll and rebuilds a pandas DataFrame from scratch. This is fine at the scale of the runs measured so far (tens of thousands of steps, low tens of MB) but is an honest O(n) cost that will get slower as a run grows — it is not a streaming/tailing reader.
- **What's NOT done yet, stated plainly**: no log rotation, no truncation, no incremental/tail-based reads. For a genuinely multi-day run this would need to switch to reading only new lines since the last poll (tracking a file offset) rather than the full file. Flagged here as a known limitation rather than silently left for someone to discover during a long demo run.
- Strategist prompts themselves never see the raw log — only the current state snapshot is inlined into the prompt, so log length has no effect on prompt size or token cost.

## 10. Failsafe (Feature 5)

`core/failsafe_controller.py` is pure Python, zero network calls, three rules: above `target_high_c` → cool to `target_high_c − setback_c`; below `target_low_c` → heat to `target_low_c + setback_c`; otherwise hold. Invoked on: Strategist exception/timeout, or a rejected correction. Demonstrated live by killing network access mid-run and observing the dashboard's source column flip to `FAILSAFE`.

## 11. Known limitations (stated unprompted)

- Groq dependency for strategic reasoning — mitigated by the failsafe, but look-ahead/optimization is lost until reconnection.
- The "forecast" is the simulation's own known future `.epw` weather, not a live external forecast API — a legitimate proxy, honestly labeled as such on the dashboard and in `get_weather`'s tool description.
- CCS threshold (0.70) is a defensible starting prior, not a production-calibrated value.
- Carbon intensity is an explicitly simulated cyclical profile (`flat_medium`), not a live grid feed.
- Single-zone scope, limited by the example `.idf`.
- Self-correction is single-step, not open-ended negotiation — bounded for latency/cost.
- `ui/app.py` re-reads the full log file each poll (see §9) — not yet a tailing reader.