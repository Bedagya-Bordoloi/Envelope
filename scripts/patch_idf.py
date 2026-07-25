"""
scripts/patch_idf.py

CRITICAL FINDING beyond the code bugs: models/baseline.idf is the stock
EnergyPlus "1ZoneUncontrolled" example file. It contains a Zone object and
envelope surfaces, but literally zero HVAC objects - no
ZoneControl:Thermostat, no ZoneHVAC:EquipmentConnections, no conditioning
equipment of any kind. That's *why* indoor temp tracked outdoor temp 1:1
in the demo log (down to -18C): there was never anything in the building
model capable of holding a setpoint, regardless of what the Python side
does. Even with every code fix applied, `get_actuator_handle(..., "Zone
Temperature Control", "Cooling Setpoint", ...)` will return -1 forever
against this file.

This script appends a minimal-but-real HVAC system to a COPY of
baseline.idf (never edits the original, so it stays valid as the
untouched baseline for Feature 2's comparison):
  - ScheduleTypeLimits: Temperature, Control Type
  - Schedule:Compact: constant heating/cooling setpoint schedules + a
    dual-setpoint control-type schedule
  - ThermostatSetpoint:DualSetpoint
  - ZoneControl:Thermostat  (this is what makes the
    "Zone Temperature Control"/"Cooling Setpoint" actuator exist)
  - Zone air/return nodes, NodeList, ZoneHVAC:EquipmentConnections
  - ZoneHVAC:IdealLoadsAirSystem + ZoneHVAC:EquipmentList
    (a textbook "perfect" HVAC unit - satisfies the thermostat setpoint
    exactly, which is standard practice for control-algorithm testing
    where you want to isolate the control logic from a specific chiller/
    coil model)

Usage:
    python scripts/patch_idf.py
Produces:
    models/controlled.idf

Point the AI-controlled EnergyPlusBridge instance at controlled.idf.
Keep baseline.idf as-is for whatever you use as the comparison instance
(see README.md for why you likely want a THIRD file - a schedule-only
baseline - rather than the literally-uncontrolled original, if you want
Feature 2's "baseline vs AI" comparison to mean anything energy-wise).

--- Design-day patch: v2, regex-based ---
The original version of this script matched the SimulationControl block
with an exact multi-line string. That's brittle: any whitespace drift,
line-ending difference, or reformatting in baseline.idf (including ones
introduced by opening/resaving the file in some editors, or a different
EnergyPlus version's example-file export) makes the exact match silently
fail. It prints a console warning in that case, but a printed warning is
easy to miss, and the result is controlled.idf quietly keeping the
design-day sizing-period pollution with no hard failure.

This version instead:
  1. Finds the SimulationControl object as a whole (by its object header,
     case-insensitive, up to the terminating ';') rather than assuming
     exact formatting.
  2. Within that block, finds the specific field whose trailing IDF field
     comment says "Run Simulation for Sizing Periods" (case-insensitive,
     whitespace-tolerant) rather than matching the field's *value* text,
     since the comment is the stable identifier and the Yes/No value is
     exactly what we're changing.
  3. Reports how many fields it actually patched, so "0 found" and
     "found 2+ (unexpected duplicate object)" are both visible instead of
     silently doing nothing or the wrong thing.
"""

import os
import re

ZONE_NAME = "ZONE ONE"

HVAC_BLOCK = f"""
!-   ===========  ALL OBJECTS IN CLASS: SCHEDULETYPELIMITS ===========
ScheduleTypeLimits,
    Temperature,             !- Name
    -60,                     !- Lower Limit Value
    200,                     !- Upper Limit Value
    CONTINUOUS;              !- Numeric Type

ScheduleTypeLimits,
    Control Type,            !- Name
    0,                       !- Lower Limit Value
    4,                       !- Upper Limit Value
    DISCRETE;                !- Numeric Type

!-   ===========  ALL OBJECTS IN CLASS: SCHEDULE:COMPACT ===========
Schedule:Compact,
    HeatingSetpointSchedule, !- Name
    Temperature,             !- Schedule Type Limits Name
    Through: 12/31,
    For: AllDays,
    Until: 24:00, 20.0;      !- constant 20C heating setpoint

Schedule:Compact,
    CoolingSetpointSchedule, !- Name
    Temperature,             !- Schedule Type Limits Name
    Through: 12/31,
    For: AllDays,
    Until: 24:00, 26.0;      !- constant 26C cooling setpoint (overridden by actuator at runtime)

Schedule:Compact,
    ZoneControlTypeSched,    !- Name
    Control Type,            !- Schedule Type Limits Name
    Through: 12/31,
    For: AllDays,
    Until: 24:00, 4;         !- 4 = DualSetpoint control every hour

!-   ===========  ALL OBJECTS IN CLASS: THERMOSTATSETPOINT:DUALSETPOINT ===========
ThermostatSetpoint:DualSetpoint,
    ZoneDualSetpoint,        !- Name
    HeatingSetpointSchedule, !- Heating Setpoint Temperature Schedule Name
    CoolingSetpointSchedule; !- Cooling Setpoint Temperature Schedule Name

!-   ===========  ALL OBJECTS IN CLASS: ZONECONTROL:THERMOSTAT ===========
ZoneControl:Thermostat,
    {ZONE_NAME} Thermostat,  !- Name
    {ZONE_NAME},             !- Zone or ZoneList Name
    ZoneControlTypeSched,    !- Control Type Schedule Name
    ThermostatSetpoint:DualSetpoint,  !- Control 1 Object Type
    ZoneDualSetpoint;        !- Control 1 Name

!-   ===========  ALL OBJECTS IN CLASS: NODELIST / ZONE AIR NODES ===========
NodeList,
    {ZONE_NAME} Inlets,      !- Name
    {ZONE_NAME} Supply Inlet Node;  !- Node 1 Name

!-   ===========  ALL OBJECTS IN CLASS: ZONEHVAC:EQUIPMENTCONNECTIONS ===========
ZoneHVAC:EquipmentConnections,
    {ZONE_NAME},                       !- Zone Name
    {ZONE_NAME} Equipment,             !- Zone Conditioning Equipment List Name
    {ZONE_NAME} Inlets,                !- Zone Air Inlet Node or NodeList Name
    ,                                  !- Zone Air Exhaust Node or NodeList Name
    {ZONE_NAME} Zone Air Node,         !- Zone Air Node Name
    {ZONE_NAME} Return Outlet;         !- Zone Return Air Node or NodeList Name

!-   ===========  ALL OBJECTS IN CLASS: ZONEHVAC:EQUIPMENTLIST ===========
ZoneHVAC:EquipmentList,
    {ZONE_NAME} Equipment,             !- Name
    SequentialLoad,                    !- Load Distribution Scheme
    ZoneHVAC:IdealLoadsAirSystem,      !- Zone Equipment 1 Object Type
    {ZONE_NAME} Ideal Loads,           !- Zone Equipment 1 Name
    1,                                 !- Zone Equipment 1 Cooling Sequence
    1;                                 !- Zone Equipment 1 Heating Sequence

!-   ===========  ALL OBJECTS IN CLASS: ZONEHVAC:IDEALLOADSAIRSYSTEM ===========
!-   A "perfect" HVAC unit: satisfies the active thermostat setpoint exactly.
!-   Standard practice for control-algorithm testing so the demo measures
!-   the Strategist/Gate logic, not a specific chiller/coil model.
ZoneHVAC:IdealLoadsAirSystem,
    {ZONE_NAME} Ideal Loads,           !- Name
    ,                                  !- Availability Schedule Name
    {ZONE_NAME} Supply Inlet Node,     !- Zone Supply Air Node Name
    ,                                  !- Zone Exhaust Air Node Name
    ,                                  !- System Inlet Air Node Name
    50,                                !- Maximum Heating Supply Air Temperature {{C}}
    13,                                !- Minimum Cooling Supply Air Temperature {{C}}
    0.0156,                            !- Maximum Heating Supply Air Humidity Ratio {{kgWater/kgDryAir}}
    0.0077,                            !- Minimum Cooling Supply Air Humidity Ratio {{kgWater/kgDryAir}}
    NoLimit,                           !- Heating Limit
    autosize,                          !- Maximum Heating Air Flow Rate {{m3/s}}
    ,                                  !- Maximum Sensible Heating Capacity {{W}}
    NoLimit,                           !- Cooling Limit
    autosize,                          !- Maximum Cooling Air Flow Rate {{m3/s}}
    ,                                  !- Maximum Total Cooling Capacity {{W}}
    ,                                  !- Heating Availability Schedule Name
    ,                                  !- Cooling Availability Schedule Name
    ConstantSupplyHumidityRatio,       !- Dehumidification Control Type
    ,                                  !- Cooling Sensible Heat Ratio {{dimensionless}}
    ConstantSupplyHumidityRatio,       !- Humidification Control Type
    ,                                  !- Design Specification Outdoor Air Object Name
    ,                                  !- Outdoor Air Inlet Node Name
    ,                                  !- Demand Controlled Ventilation Type
    ,                                  !- Outdoor Air Economizer Type
    ,                                  !- Heat Recovery Type
    ,                                  !- Sensible Heat Recovery Effectiveness {{dimensionless}}
    ;                                  !- Latent Heat Recovery Effectiveness {{dimensionless}}
"""

# Matches the whole SimulationControl,...; object, however its internal
# whitespace/line-endings are formatted. DOTALL so '.' spans newlines;
# non-greedy up to the first ';' so it doesn't swallow the next object.
_SIM_CONTROL_RE = re.compile(
    r"SimulationControl\s*,.*?;",
    re.IGNORECASE | re.DOTALL,
)

# Matches a single IDF field line ending in the
# "!- Run Simulation for Sizing Periods" comment, capturing:
#   prefix  - leading whitespace (preserved so indentation doesn't shift)
#   value   - the current Yes/No token
#   sep     - the trailing comma/semicolon plus whitespace before '!-'
#   comment - the field comment itself (preserved verbatim)
# Matching on the comment (the stable identifier) rather than the value
# (the thing we're changing) is what makes this robust to the value
# already being "No", already being "Yes", or having odd spacing.
_SIZING_FIELD_RE = re.compile(
    r"""
    (?P<prefix>[ \t]*)
    (?P<value>Yes|No)
    (?P<sep>\s*[,;]\s*)
    (?P<comment>!-\s*Run\s+Simulation\s+for\s+Sizing\s+Periods)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _patch_sizing_periods(content):
    """
    Finds every SimulationControl object in `content` and flips its
    'Run Simulation for Sizing Periods' field value to 'No', matched by
    the stable trailing field comment rather than an exact string block.
    Returns (new_content, num_fields_patched).
    """
    patched_count = 0

    def _replace_field(field_match):
        nonlocal patched_count
        patched_count += 1
        return f"{field_match.group('prefix')}No{field_match.group('sep')}{field_match.group('comment')}"

    def _replace_block(block_match):
        block = block_match.group(0)
        return _SIZING_FIELD_RE.sub(_replace_field, block)

    new_content = _SIM_CONTROL_RE.sub(_replace_block, content)
    return new_content, patched_count


def patch(src="models/baseline.idf", dst="models/controlled.idf"):
    with open(src, "r", encoding="latin-1") as f:
        content = f.read()

    # Design-day sizing periods (e.g. Denver's 99% annual heating design
    # day) were being fully simulated -- burning control steps and Groq
    # calls on synthetic extreme-weather days that aren't part of the
    # real annual run -- even though Do Zone/System/Plant Sizing
    # Calculation are already "No" and never consume that sizing data.
    # Flip this one field so only the real .epw weather-file period runs.
    content, patched_count = _patch_sizing_periods(content)

    if patched_count == 0:
        print("WARNING: no 'Run Simulation for Sizing Periods' field was found "
              "inside any SimulationControl object in models/baseline.idf -- "
              "design-day sizing periods were NOT patched. Open "
              "models/controlled.idf, find the SimulationControl object by "
              "hand, and set that field to 'No'. Then check whether "
              "baseline.idf's SimulationControl object is formatted "
              "differently than expected (e.g. the field comment text "
              "itself was changed) and update _SIZING_FIELD_RE above to match.")
    elif patched_count > 1:
        print(f"WARNING: patched {patched_count} 'Run Simulation for Sizing "
              f"Periods' fields (expected exactly 1) -- models/baseline.idf "
              f"appears to contain more than one SimulationControl object. "
              f"Verify models/controlled.idf by hand before relying on it.")
    else:
        print("Design-day sizing periods disabled (SimulationControl patched, "
              "1 field matched).")

    with open(dst, "w", encoding="latin-1") as f:
        f.write(content)
        f.write("\n")
        f.write(HVAC_BLOCK)
    print(f"Wrote {dst} ({os.path.getsize(dst)} bytes). "
          f"Original {src} left untouched.")


if __name__ == "__main__":
    patch()