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
"""

import os

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


def patch(src="models/baseline.idf", dst="models/controlled.idf"):
    with open(src, "r", encoding="latin-1") as f:
        content = f.read()
    with open(dst, "w", encoding="latin-1") as f:
        f.write(content)
        f.write("\n")
        f.write(HVAC_BLOCK)
    print(f"Wrote {dst} ({os.path.getsize(dst)} bytes). "
          f"Original {src} left untouched.")


if __name__ == "__main__":
    patch()