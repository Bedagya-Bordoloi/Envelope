"""
core/energyplus_bridge.py

Fix vs. the previous version (on top of the heating-actuator / humidity /
energy-metering fixes already in place):

4. DUPLICATE-CALLBACK / STEP-INFLATION BUG: "callback_begin_system_timestep
   _before_predictor" is NOT guaranteed to fire exactly once per zone
   timestep. EnergyPlus internally subdivides a zone timestep into shorter
   HVAC "system timesteps" when the system needs finer resolution to
   converge — which happens most during exactly the conditions you'd
   expect (large temp swings, setpoint changes). Every one of those
   sub-firings was previously incrementing step_counter and calling
   log_step(), which is why:
     - the sidebar's "AI Control Step" (last row's step value) could read
       ~8x higher than the actual number of rows in the log/chart,
     - the AI and baseline instances' step counts were never comparable
       (each accumulates sub-steps at a different, weather-dependent
       rate),
     - cadence_steps in the policy ("call the Strategist every N steps")
       fired at wildly uneven real-simulated-time intervals instead of a
       consistent cadence,
     - the outdoor-temp line looked like dense noise instead of a clean
       diurnal curve — it's real data, just heavily oversampled during
       transients.

   Fix: track api.exchange.current_sim_time(state) (cumulative simulated
   hours since the run started) and skip the callback body entirely if
   it fires again for the same simulated instant. step_counter now
   advances once per distinct simulated moment, which makes it directly
   comparable between the AI and baseline processes and makes
   cadence_steps behave as documented.
"""

import sys
import os


_HEATING_METER_CANDIDATES = ["DistrictHeatingWater:Facility", "DistrictHeating:Facility"]
_COOLING_METER_CANDIDATES = ["DistrictCooling:Facility"]

# Simulated-time values are floats; two firings for "the same instant" can
# differ by float noise in the 1e-9 range. Round before comparing.
_SIM_TIME_ROUND_DP = 6


def _load_energyplus_api():
    eplus_dir = os.environ.get("EPLUS_DIR", r"C:\EnergyPlusV24-1-0")
    if eplus_dir not in sys.path:
        sys.path.insert(0, eplus_dir)
    try:
        from pyenergyplus.api import EnergyPlusAPI
        return EnergyPlusAPI
    except ImportError as e:
        raise ImportError(
            f"Could not import pyenergyplus from {eplus_dir}. "
            f"Set the EPLUS_DIR environment variable to your EnergyPlus "
            f"install directory."
        ) from e


class EnergyPlusBridge:
    def __init__(self, idf, epw, output, decision_callback, label="AI",
                 zone_name="ZONE ONE", deadband_c=2.0, track_energy=True):
        """
        decision_callback: function(indoor_temp_c, outdoor_temp_c, humidity_pct)
                            -> (setpoint_c, source_str)
                            Called once per DISTINCT simulated timestep
                            (see the dedupe fix above) — owns all
                            Strategist/Gate/Failsafe or baseline-schedule
                            logic. Injected so this file has no dependency
                            on main.py.
        deadband_c: the AI/baseline setpoint is treated as the COOLING
                    target; heating target = setpoint - deadband. Comes
                    from config/building_policy.yaml's comfort.deadband_c.
        """
        EnergyPlusAPI = _load_energyplus_api()
        self.api = EnergyPlusAPI()
        self.state = self.api.state_manager.new_state()
        self.idf = idf
        self.epw = epw
        self.output = output
        self.decision_callback = decision_callback
        self.label = label
        self.zone_name = zone_name
        self.deadband_c = float(deadband_c)
        self.track_energy = track_energy

        # Exposed for mcp/tools.py's get_state()/get_weather() and for
        # ui/app.py's dashboard.
        self.last_indoor_temp = None
        self.last_outdoor_temp = None
        self.last_humidity = None
        self.last_setpoint = 22.0
        self.step_counter = 0
        self.pending_setpoint = None
        self.cumulative_kwh = 0.0

        self._epw_forecast = None
        self._handles_resolved = False
        self._t_in_handle = None
        self._t_out_handle = None
        self._humidity_handle = None
        self._cooling_actuator_handle = None
        self._heating_actuator_handle = None
        self._heating_meter_handle = -1
        self._cooling_meter_handle = -1
        self._energy_meter_warned = False

        # THE FIX: last simulated instant we actually processed. Used to
        # skip repeat callback firings for the same simulated timestep.
        self._last_sim_time_key = None

    def get_forward_weather(self, hours=3):
        """
        Returns the next `hours` outdoor dry-bulb temps from the .epw file
        already loaded by EnergyPlus - a deterministic proxy for a live
        forecast, honestly labeled as such wherever it's shown/logged.
        """
        if self._epw_forecast is None:
            self._epw_forecast = self._parse_epw_drybulb(self.epw)
        hour_index = min(self.step_counter // 6, len(self._epw_forecast) - 1)
        return self._epw_forecast[hour_index: hour_index + hours]

    @staticmethod
    def _parse_epw_drybulb(epw_path):
        temps = []
        with open(epw_path, "r", encoding="latin-1") as f:
            lines = f.readlines()
        for line in lines[8:]:
            parts = line.strip().split(",")
            if len(parts) > 6:
                try:
                    temps.append(float(parts[6]))
                except ValueError:
                    continue
        return temps or [20.0]

    def _resolve_handles(self, state_ptr):
        """Resolve all variable/actuator/meter handles once, after warmup."""
        self._t_in_handle = self.api.exchange.get_variable_handle(
            state_ptr, "Zone Mean Air Temperature", self.zone_name
        )
        self._t_out_handle = self.api.exchange.get_variable_handle(
            state_ptr, "Site Outdoor Air Drybulb Temperature", "Environment"
        )
        self._humidity_handle = self.api.exchange.get_variable_handle(
            state_ptr, "Zone Air Relative Humidity", self.zone_name
        )
        self._cooling_actuator_handle = self.api.exchange.get_actuator_handle(
            state_ptr, "Zone Temperature Control", "Cooling Setpoint", self.zone_name
        )
        self._heating_actuator_handle = self.api.exchange.get_actuator_handle(
            state_ptr, "Zone Temperature Control", "Heating Setpoint", self.zone_name
        )

        if self._t_in_handle == -1 or self._t_out_handle == -1:
            raise RuntimeError(
                f"[{self.label}] Variable handle not found for zone "
                f"'{self.zone_name}'. Check the zone name matches the IDF."
            )
        if self._cooling_actuator_handle == -1 or self._heating_actuator_handle == -1:
            raise RuntimeError(
                f"[{self.label}] Thermostat actuator(s) not found for zone "
                f"'{self.zone_name}'. This means the loaded IDF ({self.idf}) "
                f"has no ZoneControl:Thermostat exposing them. Run "
                f"scripts/patch_idf.py to generate models/controlled.idf "
                f"and point this instance at that file."
            )
        if self._humidity_handle == -1:
            print(f"[{self.label}] Note: 'Zone Air Relative Humidity' variable "
                  f"not found for zone '{self.zone_name}'; PMV comfort scoring "
                  f"will fall back to a fixed 50% RH assumption.")

        if self.track_energy:
            for name in _HEATING_METER_CANDIDATES:
                h = self.api.exchange.get_meter_handle(state_ptr, name)
                if h != -1:
                    self._heating_meter_handle = h
                    break
            for name in _COOLING_METER_CANDIDATES:
                h = self.api.exchange.get_meter_handle(state_ptr, name)
                if h != -1:
                    self._cooling_meter_handle = h
                    break
            if self._heating_meter_handle == -1 and self._cooling_meter_handle == -1:
                print(f"[{self.label}] WARNING: no heating/cooling facility meter "
                      f"resolved from candidates {_HEATING_METER_CANDIDATES + _COOLING_METER_CANDIDATES}. "
                      f"Energy tracking will report 0.0 kWh. Check "
                      f"{os.path.join(self.output, 'eplusout.rdd')} after a run "
                      f"for the exact meter names available in your EnergyPlus "
                      f"version and update _HEATING_METER_CANDIDATES / "
                      f"_COOLING_METER_CANDIDATES in this file.")

        self._handles_resolved = True

    def _callback(self, state_ptr):
        if self.api.exchange.warmup_flag(state_ptr):
            return

        # THE FIX: this callback can legitimately fire more than once for
        # the same simulated instant (EnergyPlus subdividing a zone
        # timestep into shorter HVAC system timesteps). Only process the
        # first firing for each distinct simulated time; skip repeats.
        # This is what was previously inflating step_counter (and every
        # log row) by roughly an order of magnitude, and made AI vs
        # baseline step counts incomparable.
        sim_time_key = round(self.api.exchange.current_sim_time(state_ptr), _SIM_TIME_ROUND_DP)
        if sim_time_key == self._last_sim_time_key:
            return
        self._last_sim_time_key = sim_time_key

        self.step_counter += 1

        if not self._handles_resolved:
            self._resolve_handles(state_ptr)

        t_in = self.api.exchange.get_variable_value(state_ptr, self._t_in_handle)
        t_out = self.api.exchange.get_variable_value(state_ptr, self._t_out_handle)
        humidity = (
            self.api.exchange.get_variable_value(state_ptr, self._humidity_handle)
            if self._humidity_handle != -1 else 50.0
        )
        self.last_indoor_temp = t_in
        self.last_outdoor_temp = t_out
        self.last_humidity = humidity

        # Cumulative energy (kWh), summed every distinct timestep so it's
        # a running total by the time this shows up on the dashboard.
        # (Also fixed by the dedupe above — this was previously double/
        # triple-counting the same real energy draw across repeat
        # firings for the same simulated instant.)
        if self.track_energy:
            step_j = 0.0
            if self._heating_meter_handle != -1:
                step_j += self.api.exchange.get_meter_value(state_ptr, self._heating_meter_handle)
            if self._cooling_meter_handle != -1:
                step_j += self.api.exchange.get_meter_value(state_ptr, self._cooling_meter_handle)
            self.cumulative_kwh += step_j / 3_600_000.0

        # SENSE -> REASON -> VERIFY lives in main.py; this file only calls
        # into it and applies whatever setpoint comes back.
        setpoint, source = self.decision_callback(t_in, t_out, humidity)
        self.last_setpoint = setpoint
        heating_setpoint = setpoint - (self.deadband_c / 2.0)

        self.api.exchange.set_actuator_value(state_ptr, self._cooling_actuator_handle, setpoint)
        self.api.exchange.set_actuator_value(state_ptr, self._heating_actuator_handle, heating_setpoint)

    def run(self):
        self.api.runtime.callback_begin_system_timestep_before_predictor(
            self.state, self._callback
        )
        self.api.runtime.run_energyplus(self.state, [
            "-d", self.output,
            "-w", self.epw,
            self.idf,
        ])