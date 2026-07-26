from core.comfort import calculate_pmv, PMVOutOfRangeError

class SentinelGate:
    def __init__(self, policy: dict):
        self.policy = policy
        self.ccs_threshold = float(policy["gate"]["ccs_threshold"])
        self.w = policy["gate"]["weights"]
        
        # State memory for stability
        self._t_out_ewma = None
        self._last_active_setpoint = 22.0
        self._steps_since_change = 0

    def _update_ewma(self, t_out):
        alpha = self.policy["comfort"]["outdoor_temp_ewma_alpha"]
        if self._t_out_ewma is None: self._t_out_ewma = t_out
        else: self._t_out_ewma = (alpha * t_out) + ((1 - alpha) * self._t_out_ewma)
        return self._t_out_ewma

    def check(self, proposed, last, llm_conf, indoor_temp, humidity, t_out):
        t_out_ewma = self._update_ewma(t_out)
        
        # 1. THE HOLD LOGIC (Stability = Profit)
        delta = abs(proposed - self._last_active_setpoint)
        min_delta = self.policy["hysteresis"]["min_delta_c"]
        min_dwell = self.policy["hysteresis"]["min_dwell_steps"]

        if delta < min_delta and self._steps_since_change < min_dwell:
            self._steps_since_change += 1
            return "HOLD", 1.0, f"HOLD: Change of {delta:.2f}C is too small. Staying steady.", {}

        # 2. COMFORT CALCULATION
        try:
            pmv_val, clo = calculate_pmv(indoor_temp, humidity, t_out_ewma)
            violation_severity = max(0.0, (abs(pmv_val) - 0.5) / 0.5)
        except:
            violation_severity = 1.0 # Safety first on math error
            pmv_val, clo = 0, 1.0

        # 3. SCORING (CCS)
        rate_penalty = min(abs(proposed - last) / 2.0, 1.0)
        ccs = (self.w["violation"] * (1 - min(violation_severity, 1.0)) +
               self.w["rate_penalty"] * (1 - rate_penalty) +
               self.w["llm_confidence"] * float(llm_conf) +
               self.w["carbon"] * 0.8) # Simulated carbon weight

        approved = ccs >= self.ccs_threshold and violation_severity == 0
        
        if approved:
            self._last_active_setpoint = proposed
            self._steps_since_change = 0
            reason = f"APPROVED: CCS {ccs:.2f} | PMV {pmv_val:.2f} (Clo {clo:.2f})"
        else:
            reason = f"REJECTED: Comfort Violation (PMV {pmv_val:.2f}). Propose value closer to 22.0C."

        return "APPROVED" if approved else "REJECTED", ccs, reason, {"pmv": pmv_val}