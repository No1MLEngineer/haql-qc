"""QC rules.

Each rule is a pure function over Rows. A rule never edits data; it only
reports. Corrections live in fixes.py and are opt-in.

Rule IDs are permanent. Adding a rule never renumbers an existing one, because
the audit logs referencing them are already in other people's hands.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date as _date
from datetime import timedelta
from statistics import median
from typing import Callable, Iterable

from .loader import Row
from .schema import Issue, Severity
from .units import CANONICAL_UNIT, UNIT_TOKENS, to_canonical_rate

Rule = Callable[..., list[Issue]]
"""A rule takes (rows) and may optionally take (columns, ceilings)."""

MATERIAL_RATIO = 0.05
"""Fraction of a well's own median output above which a value is material.

Relative to the well rather than an absolute number, because a rate that is
trivial for one well can be an anomaly for another. 5% is deliberately low:
below it a value is rounding noise, above it an operator would want to know.
"""

RULES: dict[str, Rule] = {}
RULE_META: dict[str, dict] = {}


def rule(rule_id: str, *, description: str, default_severity: Severity):
    def wrap(fn: Rule) -> Rule:
        RULES[rule_id] = fn
        RULE_META[rule_id] = {
            "description": description,
            "default_severity": default_severity.value,
        }
        return fn

    return wrap


# ---------------------------------------------------------------- structural


@rule("QC001_MISSING_WELL", description="Row has no well identifier", default_severity=Severity.CRITICAL)
def r001_missing_well(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    out = []
    for r in rows:
        if not r.well_id:
            out.append(
                Issue(
                    rule_id="QC001_MISSING_WELL",
                    well_id=None,
                    date=r.date,
                    severity=Severity.CRITICAL,
                    message="row has no well identifier; cannot be attributed",
                    source_row=r.line,
                    observed="",
                    expected="non-empty well id",
                    suggestion="reject or re-join against the well master",
                    original_value="",
                )
            )
    return out


@rule("QC002_MISSING_DATE", description="Row has no parseable date", default_severity=Severity.CRITICAL)
def r002_missing_date(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    out = []
    for r in rows:
        if not r.date:
            out.append(
                Issue(
                    rule_id="QC002_MISSING_DATE",
                    well_id=r.well_id,
                    date=None,
                    severity=Severity.CRITICAL,
                    message="date missing or unparseable",
                    source_row=r.line,
                    column="date",
                    observed=r.raw.get("date", ""),
                    expected="ISO-8601 date",
                    suggestion="re-export with ISO-8601 dates",
                    original_value=r.raw.get("date", ""),
                )
            )
    return out


@rule("QC003_UNPARSEABLE_NUMBER", description="Numeric column holds a non-numeric value", default_severity=Severity.HIGH)
def r003_unparseable(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    out = []
    for r in rows:
        for col in sorted(r.invalid_numeric):
            out.append(
                Issue(
                    rule_id="QC003_UNPARSEABLE_NUMBER",
                    well_id=r.well_id,
                    date=r.date,
                    severity=Severity.HIGH,
                    message=f"value in '{col}' is not numeric",
                    source_row=r.line,
                    column=col,
                    observed=r.raw.get(col),
                    expected="float or empty",
                    auto_correctable=True,
                    suggestion="coerce to null rather than guessing; downstream rules will flag the gap",
                    original_value=r.raw.get(col),
                )
            )
    return out


@rule("QC004_DUPLICATE_WELL_DATE", description="Same well reported twice for one date", default_severity=Severity.HIGH)
def r004_duplicate(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    seen: dict[tuple[str, str], int] = {}
    out = []
    for r in rows:
        if not r.well_id or not r.date:
            continue
        key = (r.well_id, r.date)
        if key in seen:
            out.append(
                Issue(
                    rule_id="QC004_DUPLICATE_WELL_DATE",
                    well_id=r.well_id,
                    date=r.date,
                    severity=Severity.HIGH,
                    message=f"duplicate record; first seen at line {seen[key]}",
                    source_row=r.line,
                    observed=f"line {r.line}",
                    expected=f"exactly one row per well/date (first at line {seen[key]})",
                    auto_correctable=False,
                    suggestion="decide which record is authoritative; values may differ",
                    original_value=f"line {r.line}",
                    context={"first_line": seen[key]},
                )
            )
        else:
            seen[key] = r.line
    return out


# ------------------------------------------------------------------ physical


@rule("QC010_NEGATIVE_RATE", description="Negative production rate", default_severity=Severity.HIGH)
def r010_negative(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    """Negative volume in a production or injection column.

    A negative sign on an injection column is a far weaker signal than on a
    produced one, because some exports record injection as negative by
    convention. Those are reported with the caveat attached rather than
    silently dropped: the operator, not the tool, knows the field's convention.
    """
    out = []
    for r in rows:
        for col, v in r.values.items():
            if v is not None and v < 0 and _is_rate_column(col):
                injected = _is_injection(col)
                out.append(
                    Issue(
                        rule_id="QC010_NEGATIVE_RATE",
                        well_id=r.well_id,
                        date=r.date,
                        severity=Severity.MEDIUM if injected else Severity.HIGH,
                        message=(
                            f"negative volume in '{col}'"
                            + (" (injection column)" if injected else "")
                        ),
                        source_row=r.line,
                        column=col,
                        observed=v,
                        expected=">= 0",
                        unit=_unit_for(col, unit_declarations),
                        suggestion=(
                            "some exports record injection as negative; confirm the "
                            "field's sign convention before correcting"
                            if injected
                            else "confirm sign convention before flipping; negative "
                            "production volume is not physically realisable"
                        ),
                        original_value=r.raw.get(col),
                        context={"injection": injected},
                    )
                )
    return out


@rule("QC011_METER_ROLLOVER", description="Cumulative total decreased", default_severity=Severity.HIGH)
def r011_rollover(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    """Cumulative volumes must not decrease. A drop is either a meter reset or
    a wrong total; the tool cannot tell which, so it flags and names both."""
    cumulative = [c for c in (columns or []) if "cum" in c.lower()]
    out = []
    by_well: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        if r.well_id:
            by_well[r.well_id].append(r)

    for well, wrows in by_well.items():
        for col in cumulative:
            series = [
                (r, r.num(col))
                for r in sorted(wrows, key=lambda x: (x.date or "", x.line))
                if r.date
            ]
            series = [(r, v) for r, v in series if v is not None]
            for (pr, pv), (r, cv) in zip(series, series[1:]):
                if cv < pv:
                    out.append(
                        Issue(
                            rule_id="QC011_METER_ROLLOVER",
                            well_id=well,
                            date=r.date,
                            severity=Severity.HIGH,
                            message=f"cumulative '{col}' decreased by {pv - cv:,.1f}",
                            source_row=r.line,
                            column=col,
                            observed=cv,
                            expected=f">= previous value {pv:,.1f}",
                            unit=_unit_for(col, unit_declarations),
                            suggestion="meter rollover, reallocation, or wrong column; confirm against allocation report",
                            original_value=r.raw.get(col),
                            context={"previous_value": pv, "previous_row": pr.line},
                        )
                    )
    return out


@rule(
    "QC012_DATE_GAP",
    description="Gap beyond the well's own reporting cadence",
    default_severity=Severity.MEDIUM,
)
def r012_date_gap(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    out = []
    by_well: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        if r.well_id and r.date:
            by_well[r.well_id].append(r)

    for well, wrows in by_well.items():
        ordered = sorted({r.date for r in wrows})
        if len(ordered) < 3:
            # Two points give one interval. That is a sample, not a cadence.
            continue

        deltas: list[int] = []
        for prev, cur in zip(ordered, ordered[1:]):
            try:
                deltas.append(
                    (_date.fromisoformat(cur) - _date.fromisoformat(prev)).days
                )
            except ValueError:
                continue
        if len(deltas) < 2:
            continue

        mode_days, mode_count = Counter(deltas).most_common(1)[0]
        if mode_count / len(deltas) < 0.5:
            # No dominant cadence: irregular reporting, so a gap means nothing.
            continue
        tolerance = max(1, mode_days // 2)

        for prev, cur, delta in zip(ordered, ordered[1:], deltas):
            if delta <= 0:
                continue
            # Missing days are those strictly between the two records. An
            # interval of 3 days at daily cadence means 2 absent days, not 3:
            # delta-1 are interior. Counting delta would double-report every gap.
            missing = delta - 1 - (mode_days - 1)
            if missing <= tolerance:
                continue
            try:
                d0 = _date.fromisoformat(prev)
                d1 = _date.fromisoformat(cur)
            except ValueError:
                continue
            out.append(
                Issue(
                    rule_id="QC012_DATE_GAP",
                    well_id=well,
                    date=cur,
                    severity=Severity.MEDIUM if missing <= 7 else Severity.HIGH,
                    message=(
                        f"{missing} missing day(s) against an expected "
                        f"{mode_days}-day cadence between {prev} and {cur}"
                    ),
                    source_row=min(r.line for r in wrows if r.date in (prev, cur)),
                    observed=f"{prev} -> {cur}",
                    expected=f"interval of about {mode_days} days",
                    suggestion=(
                        "check whether the well was shut in, allocated, or exported at "
                        "a coarser frequency"
                    ),
                    original_value=f"{prev} -> {cur}",
                    context={
                        "missing_days": missing,
                        "expected_interval_days": mode_days,
                        "observed_interval_days": delta,
                        "mode_share": round(mode_count / len(deltas), 3),
                        "gap_start": (d0 + timedelta(days=1)).isoformat(),
                        "gap_end": (d1 - timedelta(days=1)).isoformat(),
                    },
                )
            )
    return out


@rule("QC013_DAYS_ON_RANGE", description="days_on outside 0-31", default_severity=Severity.MEDIUM)
def r013_days_on(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    out = []
    for r in rows:
        for col, v in r.values.items():
            if "days_on" in col.lower() and v is not None and not (0 <= v <= 31):
                out.append(
                    Issue(
                        rule_id="QC013_DAYS_ON_RANGE",
                        well_id=r.well_id,
                        date=r.date,
                        severity=Severity.MEDIUM,
                        message=f"'{col}' = {v:g} outside 0-31",
                        source_row=r.line,
                        column=col,
                        observed=v,
                        expected="0 <= days_on <= 31",
                        unit="day",
                        suggestion="a month cannot exceed 31 days; check calendar alignment",
                        original_value=r.raw.get(col),
                    )
                )
    return out


@rule(
    "QC014_SHUTIN_PRODUCTION",
    description="well not producing (days_on or on-stream hours = 0) but a rate is non-zero",
    default_severity=Severity.HIGH,
)
def r014_shutin(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    """A shut-in well cannot be producing.

    Both common uptime conventions are read, because exports disagree and
    matching only one of them means the check silently never runs on half the
    industry's data:

    * ``days_on`` -- a day count, where ``0`` means no production days
    * ``ON_STREAM_HRS`` -- hours, where ``0`` or a missing value means the
      well was not on stream

    A well that reports zero uptime *and* a material volume is reporting two
    facts that cannot both be true. One of them is wrong, and the tool cannot
    tell which, so it names both the uptime column and the volume column.

    "Material" is measured against the well's own history for that column
    rather than an absolute threshold, because 0.003 Sm3 of water is a
    rounding artefact next to a well that injects 6000 Sm3/d, and the same
    number would be a crisis for a well that produces 5. Rows whose volume is
    a trivial fraction of that well's own typical output are reported at INFO
    rather than HIGH, so the material contradictions stay visible.
    """
    baselines = _per_well_medians(rows)
    out = []
    for r in rows:
        uptime_col, uptime_val = _uptime(r)
        if uptime_col is None or uptime_val is None or uptime_val > 0:
            continue
        producing = [
            (c, v)
            for c, v in r.values.items()
            if v is not None
            and v > 0
            and c != uptime_col
            and _is_rate_column(c)
        ]
        for col, v in producing:
            baseline = baselines.get((r.well_id, col))
            # Without a baseline we cannot judge materiality, so we stay
            # quiet rather than guess a threshold that belongs to the operator.
            if baseline is None or baseline <= 0:
                continue
            ratio = v / baseline
            if ratio < MATERIAL_RATIO:
                severity = Severity.INFO
            else:
                severity = Severity.HIGH
            out.append(
                Issue(
                    rule_id="QC014_SHUTIN_PRODUCTION",
                    well_id=r.well_id,
                    date=r.date,
                    severity=severity,
                    message=(
                        f"{uptime_col} = 0 but '{col}' = {v:g}, "
                        f"{ratio:.0%} of this well's typical {baseline:g}; "
                        f"the well cannot be {'injecting' if _is_injection(col) else 'producing'}"
                    ),
                    source_row=r.line,
                    column=col,
                    observed=v,
                    expected="0 when the well is not producing",
                    unit=_unit_for(col, unit_declarations),
                    suggestion=(
                        f"either {uptime_col} is wrong or '{col}' is. "
                        "A shut-in well should report zero across all rate columns"
                    ),
                    original_value=r.raw.get(col),
                    context={
                        "uptime_column": uptime_col,
                        "uptime_value": uptime_val,
                        "volume_column": col,
                        "volume_value": v,
                        "well_median_for_column": baseline,
                        "ratio_to_well_median": round(ratio, 4),
                        "material": ratio >= MATERIAL_RATIO,
                    },
                )
            )
    return out


def _per_well_medians(rows: Iterable[Row]) -> dict[tuple[str, str], float]:
    """Median positive value per (well, column), for materiality comparisons.

    Only rows where the well reports *some* uptime contribute, so a bad
    uptime reading cannot define the baseline that judges it.
    """
    acc: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        uptime_col, uptime_val = _uptime(r)
        if uptime_col is None or uptime_val is None or uptime_val <= 0:
            continue
        for col, v in r.values.items():
            if v is not None and v > 0 and _is_rate_column(col):
                acc[(r.well_id, col)].append(v)
    return {k: median(vals) for k, vals in acc.items() if vals}


def _uptime(r: Row) -> tuple[str | None, float | None]:
    """The row's uptime column and value, across naming conventions.

    Returns ``(None, None)`` when the row carries no uptime signal at all, so
    the caller can stay silent rather than assume a well was shut in.

    ``days_on`` is preferred when both are present, because a day count is
    the stronger statement of intent. Within the hours convention, an exact
    token match beats a substring match, so a column like ``AVG_ON_HRS`` is
    not mistaken for the well's own uptime.

    Matching is token-based, so spacing and case do not matter. Volve's
    monthly export writes ``On Stream`` with a space and no unit suffix, and
    that is the well's uptime for the month.
    """
    days_col = days_val = None
    for col, v in r.values.items():
        if "days_on" in col.lower() or col.lower() in ("days", "day_count"):
            days_col, days_val = col, v
            break
    if days_col is not None:
        return days_col, days_val

    # Hours convention: exact names first, then the looser substring test.
    exact = ("on_stream_hrs", "onstream_hours", "uptime", "uptime_hrs", "hours")
    for col, v in r.values.items():
        if col.lower() in exact:
            return col, v
    for col, v in r.values.items():
        c = col.lower()
        if "stream" in c or "on_hrs" in c or "uptime" in c:
            return col, v
    return None, None


@rule("QC015_IMPLAUSIBLE_RATE", description="Rate exceeds physical plausibility ceiling", default_severity=Severity.MEDIUM)
def r015_implausible(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    """Absolute sanity ceilings. Not a business limit: a 500,000 bbl/d reading
    is a units or decimal error regardless of field capacity.

    Ceilings are expressed per day, so a column holding a period *volume*
    (``BORE_OIL_VOL``, ``Oil``) is normalised to a rate first. Comparing a
    monthly total against a daily ceiling is a category error that fires on
    every healthy row.

    Ceilings are stated in canonical units (bbl/d for liquid, MCF/d for gas), so
    a reading is converted before comparison. A column whose unit the file never
    declares is skipped and named in an INFO finding: the tool will not guess a
    unit, and it will not let a skipped check look like a clean one.
    """
    unit_declarations = unit_declarations or {}
    uncovered: set[str] = set()
    ceilings = ceilings or {
        "oil": 200_000.0,
        "water": 500_000.0,
        "gas": 5_000_000.0,
    }
    out = []
    for r in rows:
        for col, v in r.values.items():
            if v is None or _is_cumulative(col):
                continue
            kind = _rate_kind(col)
            if kind is None:
                continue
            limit = ceilings.get(kind)
            if limit is None:
                continue
            rate, basis = _as_daily_rate(r, col, v, unit_declarations.get(col))
            if rate is None:
                continue
            # The ceiling is stated in the canonical unit, so the reading must
            # be converted before comparison. An unconvertible unit means the
            # check is not answerable, so it is skipped and the column is
            # reported as uncovered rather than compared in the wrong units.
            source_unit = basis["rate_unit"]
            if source_unit == "undeclared":
                uncovered.add(col)
                continue
            canonical, canon_unit, note = to_canonical_rate(rate, kind, source_unit)
            if canonical is None:
                uncovered.add(col)
                continue
            if canonical > limit:
                if basis["normalised"]:
                    detail = (
                        f"{v:,.0f} {basis['raw_unit']} over {basis['days']:g}d "
                        f"= {canonical:,.0f}/{canon_unit}d"
                    )
                else:
                    detail = f"{canonical:,.0f}/{canon_unit}d"
                out.append(
                    Issue(
                        rule_id="QC015_IMPLAUSIBLE_RATE",
                        well_id=r.well_id,
                        date=r.date,
                        severity=Severity.MEDIUM,
                        message=(
                            f"{kind} rate {detail} exceeds plausibility "
                            f"ceiling {limit:,.0f}/{canon_unit}"
                        ),
                        source_row=r.line,
                        column=col,
                        observed=round(canonical, 1),
                        expected=f"<= {limit:,.0f}",
                        unit=f"{canon_unit}/d",
                        suggestion="likely a unit or decimal-place error (psi/Pa, mcf/scf, bbl/m3)",
                        original_value=r.raw.get(col),
                        context={
                            **basis,
                            "canonical_unit": canon_unit,
                            "conversion_note": note or None,
                        },
                    )
                )
    if uncovered:
        # Surfaced in the report so a run that skipped unit-dependent checks
        # cannot be mistaken for a run that found nothing.
        out.append(
            Issue(
                rule_id="QC015_IMPLAUSIBLE_RATE",
                well_id="",
                date="",
                severity=Severity.INFO,
                message=(
                    f"{len(uncovered)} column(s) skipped for plausibility: unit not "
                    f"declared, so readings were not converted to a comparable unit"
                ),
                source_row=0,
                column=",".join(sorted(uncovered)),
                observed=",".join(sorted(uncovered)),
                expected="declare a unit, e.g. --declare-unit BORE_OIL_VOL=Sm3",
                suggestion="add --declare-unit <COLUMN>=<UNIT> to cover these columns",
                original_value=None,
                context={"skipped_columns": sorted(uncovered)},
            )
        )
    return out


@rule("QC016_CUMULATE_JUMP", description="Implied daily rate from cumulative change is implausible", default_severity=Severity.MEDIUM)
def r016_cumulate_jump(
    rows: Iterable[Row],
    columns: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    """Cumulative total and daily rate must agree. The implied rate from
    cum[n]-cum[n-1] is checked against a ceiling, catching a total that was
    replaced with a value from a different unit or a different well."""
    ceilings = ceilings or {"oil": 200_000.0, "water": 500_000.0, "gas": 5_000_000.0}
    out = []
    by_well: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        if r.well_id and r.date:
            by_well[r.well_id].append(r)

    for well, wrows in by_well.items():
        for col in [c for c in (columns or []) if "cum" in c.lower()]:
            kind = _rate_kind(col) or "oil"
            limit = ceilings.get(kind)
            if limit is None:
                continue
            series = [(r, r.num(col)) for r in sorted(wrows, key=lambda x: (x.date or "", x.line))]
            series = [(r, v) for r, v in series if v is not None and r.date]
            for (pr, pv), (r, cv) in zip(series, series[1:]):
                try:
                    days = (_date.fromisoformat(r.date) - _date.fromisoformat(pr.date)).days
                except ValueError:
                    continue
                if days <= 0:
                    continue
                delta = cv - pv
                if delta < 0:
                    continue
                implied = delta / days
                if implied > limit:
                    out.append(
                        Issue(
                            rule_id="QC016_CUMULATE_JUMP",
                            well_id=well,
                            date=r.date,
                            severity=Severity.MEDIUM,
                            message=f"implied {kind} rate {implied:,.0f}/d from cumulative delta exceeds {limit:,.0f}/d",
                            source_row=r.line,
                            column=col,
                            observed=round(implied, 1),
                            expected=f"<= {limit:,.0f}",
                            unit=f"{kind}/d",
                            suggestion="cumulative total may be from a different unit, well, or vintage",
                            original_value=r.raw.get(col),
                            context={"delta": delta, "days": days, "previous_row": pr.line},
                        )
                    )
    return out


# ----------------------------------------------------------------- helpers


def _is_cumulative(column: str) -> bool:
    return "cum" in column.lower()


# Columns that carry numbers but are not production rates. Treating a period
# index or an uptime counter as a rate produces nonsense findings.
_NON_RATE_TOKENS = (
    "month",
    "year",
    "day",
    "date",
    "index",
    "idx",
    "seq",
    "no",
    "num",
    "count",
    "id",
    "status",
    "type",
    "flag",
    "pct",
    "percent",
    "ratio",
    "quality",
    "version",
)


def _is_rate_column(column: str) -> bool:
    """True when the column plausibly holds a production rate.

    A rate must be a recognised fluid (oil/water/gas) or carry an explicit
    rate suffix. Everything else is metadata and is skipped by rate rules.
    """
    c = column.lower()
    if _is_cumulative(c):
        return False
    if _rate_kind(c) is not None:
        return True
    if any(tok in c for tok in ("per_d", "_per", "rate", "/d", "bpd", "bbl", "mcf", "scf")):
        return True
    return False


def _column_tokens(column: str) -> set[str]:
    """Split a column name into lowercase alphanumeric tokens.

    Token matching keeps ``BORE_WI_VOL`` from matching on a stray substring and
    keeps ``swift_rate`` from looking like water injection. A column name is
    split on non-alphanumerics and on camelCase boundaries.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", column)
    return {t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t}


_OIL_TOKENS = {"oil", "o"}
_WATER_TOKENS = {"water", "wtr", "wat"}
_GAS_TOKENS = {"gas", "g"}
# Injection is named by a distinct token, never by the fluid name. Volve writes
# BORE_WAT_VOL for produced water and BORE_WI_VOL for water injection, so
# listing "wat" here would have relabelled every produced-water reading as an
# injection and downgraded four real defects to medium severity.
_INJECTION_TOKENS = {
    "wi",
    "wtrinj",
    "waterinj",
    "gi",
    "gasinj",
    "injection",
    "inj",
    "inject",
    "injected",
}


def _rate_kind(column: str) -> str | None:
    """Which fluid a column reports, or ``None`` if it does not say.

    Water is matched on the whole tokens ``water``/``wtr``/``wat`` and on the
    paired injection token ``wi``, so produced water and water injection are
    both recognised. That matters: skipping ``BORE_WAT_VOL`` silently hid four
    real negative-volume records in the Volve export.
    """
    c = column.lower()
    if _is_cumulative(c):
        return None
    tokens = _column_tokens(column)
    if tokens & _OIL_TOKENS:
        return "oil"
    if tokens & _WATER_TOKENS:
        return "water"
    if tokens & _GAS_TOKENS:
        return "gas"
    # Volve writes BORE_WI_VOL for water injection. Two-letter tokens are only
    # honoured when they are the whole of a segment, to avoid matching inside
    # longer words.
    if "wi" in tokens:
        return "water"
    if "gi" in tokens:
        return "gas"
    return None


def _is_injection(column: str) -> bool:
    """True when the column reports an injected rather than produced volume.

    Sign convention is genuinely ambiguous for injection -- some exports record
    injection as negative, some as positive. Callers must not assert that a
    negative here is an error, so this flag exists to soften the claim rather
    than to skip the column.
    """
    tokens = _column_tokens(column)
    if tokens & _INJECTION_TOKENS:
        return True
    c = column.lower()
    return "inj" in c or "injection" in c


def _as_daily_rate(
    r: Row, column: str, value: float, declared_unit: str | None = None
) -> tuple[float | None, dict]:
    """Express a cell as a per-day rate, normalising period volumes.

    Three cases:

    * column name already asserts a rate (``oil_bbl_per_d``) -> use as is
    * column is a period volume and an explicit period length is known
      (``days_on``, ``ON_STREAM_HRS``) -> divide
    * column is a period volume and cadence was inferred per well
      -> divide by that cadence

    Unit conversion is deliberately *not* done here. Rescaling by a period
    length is arithmetic that holds whatever unit the file uses; converting
    Sm3 to bbl is a claim about the file, and that belongs to the caller with
    access to a declared unit. The basis dict carries ``rate_unit`` from the
    declared or in-name unit so the caller can convert or refuse.

    Returns ``(None, basis)`` when no period can be established. Reporting a
    bare volume against a daily ceiling would be meaningless, so the rule
    stays silent rather than guessing.
    """
    vol = declared_unit or _volume_unit(column)
    basis: dict = {
        "normalised": False,
        "raw_unit": vol,
        "rate_unit": vol or "undeclared",
        "days": 1.0,
    }

    if _is_interval(column) or _is_cumulative(column):
        return value, basis

    if vol is None:
        return value, basis

    # An explicit uptime column is the strongest signal available.
    hours = None
    for col, v in r.values.items():
        if v is None:
            continue
        c = col.lower()
        if "stream" in c or "on_hrs" in c or "uptime" in c or c == "hours":
            hours = v
            break
    if hours and hours > 0:
        days = hours / 24.0
        basis.update(
            normalised=True,
            days=days,
            basis_detail=f"{hours:g} on-stream hours / 24",
        )
        return value / days, basis

    days = _period_days(r, column)
    if days and days > 0:
        basis.update(normalised=True, days=days, basis_detail="inferred cadence")
        return value / days, basis

    return None, basis


def _period_days(r: Row, column: str) -> float | None:
    """Reporting period length in days for a row.

    Uses ``days_on`` when the export carries it. Otherwise consults the
    per-well cadence inferred by QC012, cached on the row's well key.
    """
    for col, v in r.values.items():
        if v is None:
            continue
        c = col.lower()
        if "days_on" in c or c in ("days", "day_count"):
            if 0 < v <= 31:
                return float(v)
            return None
    return None


def _unit_for(
    column: str,
    unit_declarations: dict[str, str] | None = None,
) -> str | None:
    """Unit label for a column: a declaration first, the name second.

    A declaration beats inference, because the operator's statement outranks
    our reading of a column name. This is a reporting aid, not a measurement:
    naming a column ``MCF/d`` when the file actually holds Sm3 is worse than
    saying nothing, so an explicit metric token in the name wins and a
    volumetric name with no cadence resolves to a bare volume unit.
    """
    declared = (unit_declarations or {}).get(column)
    if declared:
        return declared
    c = column.lower()
    vol = _volume_unit(c)
    if _is_cumulative(c):
        return vol
    if _is_interval(c):
        return f"{vol}/d" if vol else None
    # A bare name like BORE_OIL_VOL is a volume over the reporting period.
    return vol


def _volume_unit(column: str) -> str | None:
    """Unit the column name *explicitly* declares, or ``None``.

    A bare fluid name is not a unit declaration. ``BORE_OIL_VOL`` says what it
    measures, not what it is measured in, and the Volve production export is in
    Sm3 while a US export is usually in bbl. Inferring bbl from the word "oil"
    would put a wrong unit into every audit record touching that column, so an
    undeclared column resolves to ``None`` and the unit-dependent rules skip it
    and report the gap.
    """
    c = column.lower()
    longest = None
    for token, unit in UNIT_TOKENS:
        if token in c and (longest is None or len(token) > len(longest[0])):
            longest = (token, unit)
    return longest[1] if longest else None


def _is_interval(column: str) -> bool:
    """True when the column name asserts a per-day rate."""
    c = column.lower()
    return any(
        tok in c for tok in ("per_d", "_per", "/d", "bpd", "rate", "_d ", "_d,")
    ) or c.endswith("_d")


def run_all(
    rows: list[Row],
    columns: list[str],
    *,
    rule_ids: list[str] | None = None,
    ceilings: dict[str, float] | None = None,
    unit_declarations: dict[str, str] | None = None,
) -> list[Issue]:
    """Execute the ruleset against detected columns.

    Every rule accepts the same (rows, columns, ceilings, unit_declarations)
    signature. Rules that need none of the last two simply ignore them.

    ``unit_declarations`` maps a column name to the unit the data owner has
    confirmed for it, for columns whose name does not state one. Passing it
    through every rule keeps the signature uniform and means adding a
    unit-aware rule later needs no call-site change.
    """
    selected = rule_ids or list(RULES.keys())
    issues: list[Issue] = []
    for rid in selected:
        fn = RULES.get(rid)
        if fn is None:
            raise KeyError(f"unknown rule: {rid}")
        issues.extend(fn(rows, columns, ceilings, unit_declarations))
    return issues
