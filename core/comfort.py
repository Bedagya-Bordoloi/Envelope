"""
core/comfort.py

Fix: pythermalcomfort v3+ removed the standalone `pmv()` function that
used to live at pythermalcomfort.models.pmv. The current API is
pmv_ppd_iso(), which also returns a result OBJECT (with .pmv / .ppd
attributes) instead of a raw number/dict. This caused:
    ImportError: cannot import name 'pmv' from 'pythermalcomfort.models'
because requirements.txt pinned pythermalcomfort>=2.10.0 with no upper
bound, so pip installed the latest 4.x release, which dropped it.

Second fix (found via `python -m core.sentinel_gate`): pmv_ppd_iso()
silently CLAMPS any PMV value outside its documented applicability range
[-2, 2] to NaN -- it discards the real (more extreme) computed value
entirely, there is no way to recover it from the result object. A caller
that treats the return value as an ordinary float will find that
`abs(nan) <= band` is False AND `nan > 0` is False in Python, so a
comfort check built on this naively fails OPEN on exactly the inputs
where it matters most (e.g. a 10C/80%RH zone -> real PMV ~ -3.46,
silently reported as "0 violation" instead of "extreme violation").

Fix: raise PMVOutOfRangeError when pythermalcomfort reports NaN, so the
caller (core/sentinel_gate.py) can treat "out of the model's valid
range" as what it actually means -- worse than any in-range value, i.e.
maximal violation severity -- instead of accidentally treating it as no
violation at all.
"""
import math

from pythermalcomfort.models import pmv_ppd_iso


class PMVOutOfRangeError(Exception):
    """
    Raised when pythermalcomfort reports the true PMV would fall outside
    its documented applicability range [-2, 2] (and so returns NaN
    rather than the real value). This means the zone is MORE
    uncomfortable than anything the model considers well-defined -- it
    should be scored as a maximal violation, not skipped.
    """
    pass


# core/comfort.py

def calculate_pmv(temp, humidity, clo=1.0):  # Change default to 1.0 (Standard Winter)
    """Calculates Predicted Mean Vote (PMV) index."""
    result = pmv_ppd_iso(
        tdb=temp,
        tr=temp,
        vr=0.1,
        rh=humidity,
        met=1.2,
        clo=clo,  # Use the passed clothing value
        model="7730-2005",
    )
    pmv_value = float(result.pmv)
    if math.isnan(pmv_value):
        raise PMVOutOfRangeError(
            f"PMV at temp={temp}C, humidity={humidity}% falls outside "
            f"pythermalcomfort's documented applicability range [-2, 2] "
            f"-- the zone is more extreme than the model considers "
            f"well-defined."
        )
    return pmv_value