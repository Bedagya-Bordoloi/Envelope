"""
core/failsafe_controller.py

Zero-network-dependency rule-based setback controller. Used whenever the
Groq call times out, errors, or the Sentinel Gate rejects a corrected
proposal a second time. Holds the building in-bounds indefinitely.
"""


class FailsafeController:
    def __init__(self, policy: dict):
        fs = policy["failsafe"]
        self.low = float(fs["target_low_c"])
        self.high = float(fs["target_high_c"])
        self.setback = float(fs["setback_c"])

    def decide(self, current_temp):
        """Simple rule-based logic. Returns a setpoint, never raises."""
        if current_temp > self.high:
            return round(self.high - self.setback, 2)   # cool it down
        if current_temp < self.low:
            return round(self.low + self.setback, 2)    # heat it up
        return round(current_temp, 2)                    # hold steady