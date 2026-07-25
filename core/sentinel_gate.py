"""
core/sentinel_gate.py

The Governor. Scores every Strategist/baseline proposal against the
operator-editable policy in config/building_policy.yaml, returns a
calibrated Control Confidence Score (CCS) plus a plain-language reason,
and tracks a rolling override rate so that a Strategist that keeps
getting rejected is scored more conservatively over time.

Fix vs. the previous version: comfort was checked purely as a raw degC
band. core/comfort.py's ASHRAE-55 PMV model existed in the file tree but
nothing called it — dead code, same category of bug as the earlier unused
SentinelGate itself. This version calls calculate_pmv(temp, humidity) and
folds |PMV| > 0.5 (the ASHRAE-55 "acceptable" comfort band boundary) into
the same violation-severity term as the hard degC bounds, so a proposal
can now be rejected for being uncomfortable even if it's technically
inside the raw temp_min/temp_max rails. The raw degC bounds are kept as a
hard safety backstop -- PMV augments them, it doesn't replace them.
"""

from collections import deque

from core.comfort import calculate_pmv

PMV_ACCEPTABLE_BAND = 0.5  # ASHRAE-55: |PMV| <= 0.5 is the "acceptable" comfort zone


class SentinelGate:
    def __init__(self, policy: dict):
        comfort = policy["comfort"]
        rate = policy["rate_limits"]
        gate = policy["gate"]

        self.min_temp = float(comfort["temp_min_c"])
        self.max_temp = float(comfort["temp_max_c"])
        self.max_delta = float(rate["max_delta_c_per_step"])
        self.ccs_threshold = float(gate["ccs_threshold"])
        self.w = gate["weights"]

        self._recent_decisions = deque(maxlen=50)

    def _bounds_violation_severity(self, proposed_setpoint):
        """0.0 = fully within the hard degC bounds, 1.0 = maximally out."""
        if self.min_temp <= proposed_setpoint <= self.max_temp:
            return 0.0
        overshoot = max(self.min_temp - proposed_setpoint,
                         proposed_setpoint - self.max_temp)
        span = max(self.max_temp - self.min_temp, 1e-6)
        return min(overshoot / span, 1.0)

    def _pmv_violation_severity(self, indoor_temp, humidity):
        """
        0.0 = PMV within the +/-0.5 ASHRAE-55 acceptable band, scaling up
        to 1.0 as |PMV| grows. Uses the CURRENT sensed indoor temp (not
        the proposed setpoint, which hasn't been realized by the building
        yet) — this scores "is the zone comfortable right now", which is
        what should gate an AI that's been holding a setpoint that isn't
        actually working.
        """
        if indoor_temp is None:
            return 0.0
        try:
            result = calculate_pmv(indoor_temp, humidity if humidity is not None else 50.0)
            pmv_value = result["pmv"] if isinstance(result, dict) else float(result)
        except Exception:
            # pythermalcomfort can raise on out-of-range inputs (e.g. deep
            # sub-zero indoor temps during a failsafe transient). Don't let
            # a comfort-model edge case block an otherwise-safe proposal —
            # fall back to the hard degC bounds alone for this step.
            return 0.0
        if abs(pmv_value) <= PMV_ACCEPTABLE_BAND:
            return 0.0
        overshoot = abs(pmv_value) - PMV_ACCEPTABLE_BAND
        return min(overshoot / PMV_ACCEPTABLE_BAND, 1.0)

    def _rate_penalty(self, proposed_setpoint, last_setpoint):
        if self.max_delta <= 0:
            return 0.0
        return abs(proposed_setpoint - last_setpoint) / self.max_delta

    def _override_rate(self):
        if not self._recent_decisions:
            return 0.0
        rejected = sum(1 for ok in self._recent_decisions if not ok)
        return rejected / len(self._recent_decisions)

    def score(self, proposed_setpoint, last_setpoint, llm_confidence=0.8,
              indoor_temp=None, humidity=None):
        """
        Calibrated CCS in [0, 1]. Higher is more trustworthy.
        violation_severity is the WORSE of the hard-bounds check and the
        PMV comfort check, so either one alone can trigger a rejection.
        """
        bounds_violation = self._bounds_violation_severity(proposed_setpoint)
        pmv_violation = self._pmv_violation_severity(indoor_temp, humidity)
        violation_severity = max(bounds_violation, pmv_violation)
        rate_penalty = min(self._rate_penalty(proposed_setpoint, last_setpoint), 1.0)
        override_rate = self._override_rate()

        ccs = (
            self.w["violation"] * (1 - violation_severity)
            + self.w["rate_penalty"] * (1 - rate_penalty)
            + self.w["llm_confidence"] * float(llm_confidence)
            + self.w["override_rate"] * (1 - override_rate)
        )
        return round(ccs, 4), violation_severity, rate_penalty, override_rate, bounds_violation, pmv_violation

    def explain(self, proposed_setpoint, last_setpoint, ccs, violation_severity,
                rate_penalty, approved, bounds_violation, pmv_violation):
        if bounds_violation > 0:
            return (
                f"REJECTED - proposed setpoint {proposed_setpoint:.1f}C breaches "
                f"the {self.min_temp:.0f}-{self.max_temp:.0f}C comfort band. "
                f"Propose a setpoint inside that range."
            )
        if pmv_violation > 0:
            return (
                f"REJECTED - sensed zone comfort is outside the ASHRAE-55 "
                f"acceptable PMV band (+/-{PMV_ACCEPTABLE_BAND}) at the current "
                f"indoor temperature/humidity, even though the setpoint itself "
                f"is within the raw degC bounds. Propose a setpoint that moves "
                f"the zone back toward neutral thermal sensation."
            )
        if rate_penalty >= 1.0:
            delta = abs(proposed_setpoint - last_setpoint)
            return (
                f"REJECTED - setpoint change of {delta:.1f}C meets or exceeds the "
                f"max allowed rate of {self.max_delta:.1f}C/step. "
                f"Propose a smaller step from {last_setpoint:.1f}C."
            )
        if not approved:
            return (
                f"REJECTED - CCS {ccs:.2f} is below the {self.ccs_threshold:.2f} "
                f"threshold even though no single hard bound was breached. "
                f"Propose a setpoint closer to {last_setpoint:.1f}C with higher "
                f"stated confidence."
            )
        return f"APPROVED - CCS {ccs:.2f} is within safe operational limits."

    def check(self, proposed_setpoint, last_setpoint, llm_confidence=0.8,
              indoor_temp=None, humidity=None):
        """
        Single entry point used by main.py.
        Returns (approved: bool, ccs: float, reason: str).
        indoor_temp/humidity are optional — pass the currently sensed zone
        state to enable the PMV comfort check; omit to fall back to the
        hard degC bounds only (e.g. for a first call before any state has
        been sensed).
        """
        ccs, violation_severity, rate_penalty, _, bounds_violation, pmv_violation = self.score(
            proposed_setpoint, last_setpoint, llm_confidence, indoor_temp, humidity
        )
        approved = ccs >= self.ccs_threshold and violation_severity == 0.0
        reason = self.explain(proposed_setpoint, last_setpoint, ccs, violation_severity,
                               rate_penalty, approved, bounds_violation, pmv_violation)
        self._recent_decisions.append(approved)
        return approved, ccs, reason


if __name__ == "__main__":
    import yaml
    with open("config/building_policy.yaml") as f:
        policy = yaml.safe_load(f)
    gate = SentinelGate(policy)
    print(gate.check(23.0, 24.0, indoor_temp=23.0, humidity=45.0))   # expect APPROVED
    print(gate.check(35.0, 24.0, indoor_temp=23.0, humidity=45.0))   # expect REJECTED - out of bounds
    print(gate.check(23.0, 24.0, indoor_temp=10.0, humidity=80.0))   # expect REJECTED - PMV