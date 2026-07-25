"""
core/comfort.py

Fix: pythermalcomfort v3+ removed the standalone `pmv()` function that
used to live at pythermalcomfort.models.pmv. The current API is
pmv_ppd_iso(), which also returns a result OBJECT (with .pmv / .ppd
attributes) instead of a raw number/dict. This caused:
    ImportError: cannot import name 'pmv' from 'pythermalcomfort.models'
because requirements.txt pinned pythermalcomfort>=2.10.0 with no upper
bound, so pip installed the latest 4.x release, which dropped it.
"""
from pythermalcomfort.models import pmv_ppd_iso


def calculate_pmv(temp, humidity):
    """Calculates Predicted Mean Vote (PMV) index. Returns a plain float,
    so callers (core/sentinel_gate.py) don't need to know about
    pythermalcomfort's result-object API."""
    result = pmv_ppd_iso(
        tdb=temp,          # dry bulb temp
        tr=temp,           # mean radiant temp (assumed same as air)
        vr=0.1,            # air velocity
        rh=humidity,       # relative humidity
        met=1.2,           # metabolic rate (office work)
        clo=0.7,           # clothing level (typical indoor)
        model="7730-2005",
    )
    return float(result.pmv)