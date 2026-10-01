"""Unit handling that refuses to guess.

The audit rule this module exists to enforce: never attach a unit to a number
that the data did not declare. A plausible-looking wrong unit is worse than an
absent one, because a reviewer cannot tell it apart from a correct value.

A column's own name is the only evidence the tool trusts. ``oil_bbl_per_d``
declares itself; ``BORE_OIL_VOL`` does not, and a bare fluid name is *not* a
unit declaration. When a unit is needed but undeclared, callers skip the check
and record the column name so the run stays honest about what was not covered.

Conversions are stated, not derived, and each carries the conditions it holds
under. Field operators routinely disagree about standard conditions, so the
table names them rather than hiding a choice.
"""

from __future__ import annotations

# --- canonical comparison units -------------------------------------------
# One canonical rate unit per fluid, so a ceiling is always stated in the same
# terms regardless of the source file's convention.
CANONICAL_UNIT = {
    "oil": "bbl",
    "water": "bbl",
    "gas": "MCF",
}

# --- source volume units ---------------------------------------------------
# Value of 1 source unit expressed in the canonical unit.
#
# Oil and water are stock-tank volumes, so only a small set of liquid units
# apply. Gas is the awkward one: it depends on the reference conditions used to
# define a "standard" cubic foot, and that choice moves the number by several
# percent. The table therefore states conditions instead of assuming them.
VOLUME_TO_CANONICAL: dict[str, dict[str, tuple[float, str]]] = {
    "oil": {
        # 1 stock tank barrel = 0.158987294928 m^3 (API MPMS 14.3.2).
        "bbl": (1.0, ""),
        "stb": (1.0, ""),
        "Sm3": (1.0 / 0.158987294928, "API MPMS 14.3.2 stock tank"),
        "m3": (1.0 / 0.158987294928, "API MPMS 14.3.2 stock tank"),
    },
    "water": {
        "bbl": (1.0, ""),
        "stb": (1.0, ""),
        "Sm3": (1.0 / 0.158987294928, "API MPMS 14.3.2 stock tank"),
        "m3": (1.0 / 0.158987294928, "API MPMS 14.3.2 stock tank"),
    },
    "gas": {
        "MCF": (1.0, "as reported"),
        "Mcf": (1.0, "as reported"),
        "scf": (1.0 / 1000.0, "Mcf = 1000 scf, as reported"),
        "MMscf": (1000.0, "MMscf = 1000 Mcf, as reported"),
        # 1 m^3 = 35.3146667215 ft^3 exactly, by the international foot.
        # This is a geometric conversion and carries no pressure or temperature
        # assumption: it converts a volume to a volume. Reading the resulting
        # figure as a gas quantity still requires the file to say what
        # conditions the source used, which is why callers must declare Sm3 gas
        # as "reported" rather than have this module infer standard conditions.
        "Sm3": (0.0353146667215, "geometric ft3 conversion; source conditions unverified"),
        "m3": (0.0353146667215, "geometric ft3 conversion; source conditions unverified"),
    },
}

# Unit tokens recognised in a column name, mapped to the unit they declare.
#
# Every token here must have an entry in every applicable VOLUME_TO_CANONICAL
# table. An energy token such as MMBtu is deliberately absent: this tool has no
# energy conversion, so accepting the token and then skipping the column would
# advertise coverage it does not have.
UNIT_TOKENS: tuple[tuple[str, str], ...] = (
    ("sm3", "Sm3"),
    ("m3", "m3"),
    ("scf", "scf"),
    ("mcf", "MCF"),
    ("bbl", "bbl"),
    ("stb", "bbl"),
)


def is_convertible(kind: str, unit: str) -> bool:
    """True when ``unit`` can be converted for ``kind``."""
    return conversion_factor(kind, unit) is not None


def normalise_unit_token(text: str) -> str | None:
    """Map a user-supplied or in-name unit token to a supported unit.

    Returns ``None`` for anything unrecognised, so an unknown unit is skipped
    rather than treated as the canonical one.
    """
    t = text.strip()
    if not t:
        return None
    lowered = t.lower()
    for token, unit in UNIT_TOKENS:
        if lowered == token:
            return unit
    aliases = {
        "m3/d": "m3",
        "sm3/d": "Sm3",
        "bbl/d": "bbl",
        "stb/d": "bbl",
        "mcf/d": "MCF",
        "scf/d": "scf",
        "barrel": "bbl",
        "barrels": "bbl",
        "cubic_meter": "Sm3",
        "cubic_metre": "Sm3",
    }
    return aliases.get(lowered)


def conversion_factor(kind: str, source_unit: str) -> tuple[float, str] | None:
    """Factor converting one ``source_unit`` into the canonical unit for ``kind``.

    Returns ``None`` when the pair is not convertible, which is the signal to
    skip a check rather than to assume a default.
    """
    table = VOLUME_TO_CANONICAL.get(kind)
    if table is None:
        return None
    return table.get(source_unit)


def to_canonical_rate(
    value: float, kind: str, source_unit: str
) -> tuple[float | None, str | None, str | None]:
    """Convert a per-day rate into the canonical unit for ``kind``.

    Returns ``(value, canonical_unit, note)``. ``value`` is ``None`` when the
    unit cannot be converted, in which case the caller must skip the check.
    """
    factor = conversion_factor(kind, source_unit)
    if factor is None:
        return None, None, None
    scale, note = factor
    return value * scale, CANONICAL_UNIT[kind], note
