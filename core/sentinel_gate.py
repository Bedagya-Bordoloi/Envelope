"""
core/sentinel_gate.py

Generic YAML-driven rule engine. Every hard/soft bound the gate enforces
(comfort band, PMV, rate limit, and any future bound such as a CO2
ceiling) is declared under gate.rules in config/building_policy.yaml.
This file interprets those rules generically -- it doesn't hardcode what
a "temperature bound" or "CO2 ceiling" looks like, it just knows how to
evaluate a small set of rule TYPES (range / pmv / rate_limit) against
whatever values are handed to it. Add a new bound, tighten an existing
one, or disable one entirely by editing the YAML -- no code changes, no
redeploy.

Carbon intensity is a real weighted CCS term now (gate.weights.carbon +
carbon.weight_map in the YAML), not prompt-only decoration -- a setpoint
proposed during a "High" carbon window is scored more conservatively
than the identical proposal during a "Low" window.
"""

from collections import deque

from core.comfort import calculate_pmv, PMVOutOfRangeError

_REQUIRED_WEIGHT_KEYS = ("violation", "rate_penalty", "llm_confidence", "override_rate", "carbon")


# ---------------------------------------------------------------------------
# Generic rule engine -- knows about rule TYPES, not specific bounds.
# ---------------------------------------------------------------------------
class RuleEngine:
    """
    Evaluates a list of YAML-declared rules against a dict of sensed/
    proposed values. Each rule contributes a severity in [0, 1] (0 =
    fully satisfied, 1 = maximally violated). Unknown rule types are
    skipped rather than crashing the gate on a policy typo -- but that
    skip is printed so it isn't silently invisible.
    """

    def __init__(self, rules):
        self.rules = [r for r in (rules or []) if r.get("enabled", True)]
        self._warned_unknown_types = set()

    def _range_severity(self, rule, values):
        value = values.get(rule.get("value_source"))
        if value is None:
            return 0.0, None
        lo, hi = rule.get("min"), rule.get("max")
        if lo is not None and hi is not None:
            if lo <= value <= hi:
                return 0.0, value
            overshoot = max(lo - value, value - hi)
            span = max(hi - lo, 1e-6)
            return min(overshoot / span, 1.0), value
        if hi is not None:
            if value <= hi:
                return 0.0, value
            return min((value - hi) / max(abs(hi), 1e-6), 1.0), value
        if lo is not None:
            if value >= lo:
                return 0.0, value
            return min((lo - value) / max(abs(lo), 1e-6), 1.0), value
        return 0.0, value


    def _pmv_severity(self, rule, values):
        indoor_temp = values.get("indoor_temp")
        if indoor_temp is None:
            return 0.0, None
        humidity = values.get("humidity")
        band = float(rule.get("band", 0.5))
        
        # Read the dynamic clothing value or fall back to 1.0
        clo = values.get("clo", 1.0) 
        
        try:
            pmv_value = calculate_pmv(
                indoor_temp, 
                humidity if humidity is not None else 50.0, 
                clo=clo
            )
        except PMVOutOfRangeError:
            return 1.0, "out_of_range"
        # ... rest of your code ...
        if abs(pmv_value) <= band:
            return 0.0, pmv_value
        return min((abs(pmv_value) - band) / band, 1.0), pmv_value

    
    def _rate_limit_severity(self, rule, values):
        proposed, last = values.get("setpoint"), values.get("last_setpoint")
        max_delta = float(rule.get("max_delta_per_step", 0))
        if proposed is None or last is None or max_delta <= 0:
            return 0.0, None
        delta = abs(proposed - last)
        return min(delta / max_delta, 1.0), delta

    _DISPATCH = {
        "range": _range_severity,
        "pmv": _pmv_severity,
        "rate_limit": _rate_limit_severity,
    }

    def evaluate(self, values):
        """
        Returns a list of dicts: {id, type, hard, severity, raw_value,
        description}. Caller (SentinelGate) decides how to fold these
        into the CCS formula and the plain-language explanation.
        """
        results = []
        for rule in self.rules:
            rtype = rule.get("type")
            fn = self._DISPATCH.get(rtype)
            if fn is None:
                if rtype not in self._warned_unknown_types:
                    print(f"[SentinelGate] WARNING: unknown rule type "
                          f"'{rtype}' for rule '{rule.get('id', '?')}' in "
                          f"building_policy.yaml -- skipping this rule. "
                          f"Valid types: {sorted(self._DISPATCH)}.")
                    self._warned_unknown_types.add(rtype)
                continue
            severity, raw_value = fn(self, rule, values)
            results.append({
                "id": rule.get("id", rtype),
                "type": rtype,
                "hard": bool(rule.get("hard", True)),
                "severity": round(severity, 4),
                "raw_value": raw_value,
                "description": rule.get("description", rule.get("id", rtype)),
            })
        return results


class SentinelGate:
    def __init__(self, policy: dict):
        gate = policy["gate"]
        self.ccs_threshold = float(gate["ccs_threshold"])
        self.w = gate["weights"]

        missing = [k for k in _REQUIRED_WEIGHT_KEYS if k not in self.w]
        if missing:
            raise ValueError(
                f"building_policy.yaml gate.weights is missing required "
                f"key(s) {missing}. Required: {_REQUIRED_WEIGHT_KEYS}."
            )
        weight_sum = sum(float(v) for v in self.w.values())
        if abs(weight_sum - 1.0) > 0.01:
            print(f"[SentinelGate] WARNING: gate.weights sums to "
                  f"{weight_sum:.3f}, not 1.0 -- CCS will not be on a "
                  f"clean [0,1] scale until this is fixed in the YAML.")

        self.rule_engine = RuleEngine(gate.get("rules", []))

        carbon_cfg = policy.get("carbon", {})
        self.carbon_weight_map = carbon_cfg.get(
            "weight_map", {"Low": 0.0, "Medium": 0.4, "High": 1.0}
        )
        self.carbon_default_penalty = float(carbon_cfg.get("unknown_level_penalty", 0.5))

        self._recent_decisions = deque(maxlen=50)

    # -- individual CCS components ---------------------------------------
    def _carbon_penalty(self, carbon_intensity):
        if carbon_intensity is None:
            return 0.0
        return float(self.carbon_weight_map.get(carbon_intensity, self.carbon_default_penalty))

    def _override_rate(self):
        if not self._recent_decisions:
            return 0.0
        rejected = sum(1 for ok in self._recent_decisions if not ok)
        return rejected / len(self._recent_decisions)

    # -- scoring -----------------------------------------------------------
    def score(self, proposed_setpoint, last_setpoint, llm_confidence=0.8,
              indoor_temp=None, humidity=None, carbon_intensity=None,
              extra_values=None):
        values = {
            "setpoint": proposed_setpoint,
            "last_setpoint": last_setpoint,
            "indoor_temp": indoor_temp,
            "humidity": humidity,
        }
        if extra_values:
            values.update(extra_values)

        rule_results = self.rule_engine.evaluate(values)

        hard_bound_results = [r for r in rule_results if r["type"] != "rate_limit" and r["hard"]]
        violation_severity = max((r["severity"] for r in hard_bound_results), default=0.0)

        rate_results = [r for r in rule_results if r["type"] == "rate_limit"]
        rate_penalty = max((r["severity"] for r in rate_results), default=0.0)

        override_rate = self._override_rate()
        carbon_penalty = self._carbon_penalty(carbon_intensity)

        ccs = (
            self.w["violation"] * (1 - violation_severity)
            + self.w["rate_penalty"] * (1 - rate_penalty)
            + self.w["llm_confidence"] * float(llm_confidence)
            + self.w["override_rate"] * (1 - override_rate)
            + self.w["carbon"] * (1 - carbon_penalty)
        )

        return {
            "ccs": round(ccs, 4),
            "violation_severity": round(violation_severity, 4),
            "rate_penalty": round(rate_penalty, 4),
            "override_rate": round(override_rate, 4),
            "carbon_intensity": carbon_intensity,
            "carbon_penalty": round(carbon_penalty, 4),
            "rules": rule_results,
        }

    def explain(self, breakdown, proposed_setpoint, last_setpoint, approved):
        hard_hits = [r for r in breakdown["rules"]
                     if r["type"] != "rate_limit" and r["hard"] and r["severity"] > 0]
        if hard_hits:
            worst = max(hard_hits, key=lambda r: r["severity"])
            return (
                f"REJECTED - '{worst['description']}' violated "
                f"(sensed/proposed value {worst['raw_value']}, severity "
                f"{worst['severity']:.2f}). Propose a setpoint that "
                f"resolves this before anything else."
            )

        rate_hits = [r for r in breakdown["rules"] if r["type"] == "rate_limit" and r["severity"] >= 1.0]

        if not approved:
            if rate_hits:
                worst = max(rate_hits, key=lambda r: r["severity"])
                delta = abs(proposed_setpoint - last_setpoint)
                return (
                    f"REJECTED - '{worst['description']}' exceeded: setpoint "
                    f"change of {delta:.1f}C this step contributed to CCS "
                    f"{breakdown['ccs']:.2f} falling below the "
                    f"{self.ccs_threshold:.2f} threshold. Propose a smaller "
                    f"step from {last_setpoint:.1f}C."
                )
            carbon_note = ""
            if breakdown["carbon_penalty"] > 0.5:
                carbon_note = (
                    f" Grid carbon intensity is currently "
                    f"'{breakdown['carbon_intensity']}', which is scored "
                    f"conservatively -- a more confident or more "
                    f"conservative proposal is preferred right now."
                )
            return (
                f"REJECTED - CCS {breakdown['ccs']:.2f} is below the "
                f"{self.ccs_threshold:.2f} threshold even though no single "
                f"hard bound was breached.{carbon_note} Propose a setpoint "
                f"closer to {last_setpoint:.1f}C with higher stated confidence."
            )

        # Approved overall, but still worth flagging a maxed-out rate
        # penalty -- it's a soft rule (didn't block approval on its own,
        # since rate_limit is excluded from violation_severity), but a
        # judge/operator should still see it happened.
        if rate_hits:
            return (
                f"APPROVED - CCS {breakdown['ccs']:.2f} is within safe "
                f"operational limits, though this step's setpoint change was "
                f"at or above the max recommended rate (soft penalty only, "
                f"did not block approval)."
            )
        return f"APPROVED - CCS {breakdown['ccs']:.2f} is within safe operational limits."

    # -- single entry point used by main.py ---------------------------------
    def check(self, proposed_setpoint, last_setpoint, llm_confidence=0.8,
              indoor_temp=None, humidity=None, carbon_intensity=None,
              extra_values=None):
        """
        Returns (approved: bool, ccs: float, reason: str, breakdown: dict).
        breakdown carries every rule that fired plus the carbon/override
        terms, so a rejection is fully auditable from the log -- not just
        a pass/fail number.
        """
        breakdown = self.score(proposed_setpoint, last_setpoint, llm_confidence,
                                indoor_temp, humidity, carbon_intensity, extra_values)
        approved = breakdown["ccs"] >= self.ccs_threshold and breakdown["violation_severity"] == 0.0
        reason = self.explain(breakdown, proposed_setpoint, last_setpoint, approved)
        self._recent_decisions.append(approved)
        return approved, breakdown["ccs"], reason, breakdown


if __name__ == "__main__":
    import yaml
    with open("config/building_policy.yaml") as f:
        policy = yaml.safe_load(f)
    gate = SentinelGate(policy)
    print(gate.check(23.0, 24.0, indoor_temp=23.0, humidity=45.0, carbon_intensity="Low"))
    print(gate.check(35.0, 24.0, indoor_temp=23.0, humidity=45.0, carbon_intensity="High"))
    print(gate.check(23.0, 24.0, indoor_temp=10.0, humidity=80.0, carbon_intensity="Medium"))
    print(gate.check(24.5, 20.0, indoor_temp=22.0, humidity=45.0, carbon_intensity="Low"))