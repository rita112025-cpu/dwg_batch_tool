"""Drawing-unit names and length conversion for mapping parameters.

Coordinates in the output stay in drawing units. A polyline width is copied
into a mapping parameter only when ``$INSUNITS`` is a known length and the
mapping names the parameter unit. Otherwise the mapping default is kept.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# Metres per one drawing unit, keyed by DXF $INSUNITS.
_SURVEY_FOOT = Decimal(1200) / Decimal(3937)
_TO_METRES = {
    1: Decimal("0.0254"),
    2: Decimal("0.3048"),
    3: Decimal("1609.344"),
    4: Decimal("0.001"),
    5: Decimal("0.01"),
    6: Decimal("1"),
    7: Decimal("1000"),
    8: Decimal("0.0000000254"),
    9: Decimal("0.0000254"),
    10: Decimal("0.9144"),
    14: Decimal("0.1"),
    15: Decimal("10"),
    16: Decimal("100"),
    21: _SURVEY_FOOT,
    22: _SURVEY_FOOT / Decimal(12),
    23: _SURVEY_FOOT * Decimal(3),
    24: _SURVEY_FOOT * Decimal(5280),
}

_PARAMETER_TO_METRES = {
    "mm": Decimal("0.001"),
    "cm": Decimal("0.01"),
    "m": Decimal("1"),
    "in": Decimal("0.0254"),
    "ft": Decimal("0.3048"),
}


def insunits_name(code) -> str:
    """Human-readable name for a DXF ``$INSUNITS`` code."""
    try:
        from ezdxf import units as ez_units

        return str(ez_units.unit_name(int(code)))
    except Exception:
        return f"Unknown({code})"


def convert_length(value, insunits, unit):
    """Convert ``value`` drawing units into ``unit``.

    Returns None when either unit is unknown (including unitless ``0``), so
    the caller keeps the mapping default instead of guessing a scale.
    """
    try:
        code = int(insunits)
    except (TypeError, ValueError):
        return None
    if code not in _TO_METRES or unit not in _PARAMETER_TO_METRES:
        return None
    metres = Decimal(str(value)) * _TO_METRES[code]
    converted = metres / _PARAMETER_TO_METRES[unit]
    quantized = converted.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return float(quantized)
