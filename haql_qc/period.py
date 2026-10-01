"""Loading for period-granularity production exports.

Some operators ship a monthly or quarterly file where the reporting period is
carried in two columns rather than a date. Volve's monthly release is the
reference case: ``Year`` and ``Month`` columns, no date column anywhere, and a
second header line holding the units for each measure.

Both of those are handled here rather than in the daily loader, because both
are the same underlying problem -- the file describes its own schema in the
rows instead of the header, and a loader that ignores that either crashes or
silently audits garbage.

Nothing here guesses. If the units row is absent the units are simply not
known, and the caller is told so. If a year or month is unparseable the row
carries no date and the normal date rules report it.
"""

from __future__ import annotations

import csv
from datetime import date as _date
from pathlib import Path
from typing import Any

from .loader import LoadError, Row, sha256_file

UNITS_ROW_MAX_CELL = 12
"""Longest cell that can still be a unit token.

Keeps a prose note under the header from being read as a units row. A genuine
units row holds short tokens -- ``Sm3``, ``hrs``, ``bbl``, ``%``.
"""


class PeriodError(Exception):
    """Raised only for problems that make the file unauditable."""


def _looks_like_units_row(values: list[str]) -> tuple[bool, dict[str, str]]:
    """Decide whether a row is a units row, and return what it declares.

    Returns ``(is_units_row, {column: unit})``. The unit map is empty unless
    the row qualifies, so a caller cannot accidentally consume units from a
    line that is really data.
    """
    present = [v for v in values if v is not None and str(v).strip()]
    if not present:
        return False, {}

    numeric = 0
    for v in present:
        cleaned = str(v).strip().replace(",", "")
        try:
            float(cleaned)
            numeric += 1
        except ValueError:
            pass

    # A units row is all labels. One numeric cell means it is data.
    if numeric:
        return False, {}

    # Every cell must be short. A long sentence is a note, not a unit.
    if max(len(str(v).strip()) for v in present) > 12:
        return False, {}

    return True, {}


def detect_units_row(path: Path, columns: list[str]) -> tuple[int, dict[str, str]]:
    """Find a units row and return its 1-indexed file line plus its units.

    Only the line immediately after the header is considered. A units row
    anywhere deeper is a comment or a continuation, and guessing at which one
    applies to which column is exactly the kind of inference this tool refuses
    to make.
    """
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        try:
            next(reader)
        except StopIteration:
            return 0, {}
        try:
            second = next(reader)
        except StopIteration:
            return 0, {}

        is_units, _ = _looks_like_units_row(second)
        if not is_units:
            return 0, {}

        units: dict[str, str] = {}
        for col, val in zip(columns, second):
            token = str(val or "").strip()
            if token and token.lower() not in ("null", "none", "-", "n/a"):
                units[col] = token
        return 2, units


def month_end_date(year: Any, month: Any) -> str | None:
    """Last calendar day of a year/month pair, as an ISO date.

    The last day is chosen rather than the first so that the date sits inside
    the period it describes. A monthly row dated its first day looks, to any
    date-gap rule, like a record from before the period it covers.
    """
    try:
        y = int(str(year).strip())
        m = int(str(month).strip())
    except (TypeError, ValueError):
        return None
    if not (1 <= m <= 12):
        return None
    if not (1000 <= y <= 9999):
        return None

    nxt = _date(y + 1, 1, 1) if m == 12 else _date(y, m + 1, 1)
    return _date.fromordinal(nxt.toordinal() - 1).isoformat()


def load_period_csv(
    path: Path,
    well_column: str,
    year_column: str,
    month_column: str,
    skip_units_row: bool = True,
) -> tuple[list[Row], list[str], dict[str, str]]:
    """Read a Year/Month production export.

    Returns ``(rows, columns, units)`` where ``units`` maps column name to the
    unit declared on the file's own units row. An empty ``units`` map means
    the file declared nothing, and the caller must ask the operator rather
    than assume.

    The units row, when present, is skipped rather than loaded as data. It is
    still reported as part of the input hash, so the audit covers the exact
    bytes on disk.
    """
    path = Path(path)
    if not path.exists():
        raise PeriodError(f"input not found: {path}")
    if not path.is_file():
        raise PeriodError(f"not a regular file: {path}")

    units_line, units = detect_units_row(path, []) if skip_units_row else (0, {})

    rows: list[Row] = []
    columns: list[str] = []

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise PeriodError(f"empty file, no header: {path}")
        columns = [c.strip() for c in reader.fieldnames]
        reader.fieldnames = columns

        missing = [c for c in (well_column, year_column, month_column) if c not in columns]
        if missing:
            raise PeriodError(
                f"missing required column(s) {missing}; found {columns}"
            )

        for line, raw in enumerate(reader, start=2):
            if units_line and line == units_line:
                if not units:
                    units = _units_from_row(columns, raw)
                continue

            cleaned_raw: dict[str, str] = {}
            for k, v in raw.items():
                if k is None:
                    continue
                cleaned_raw[k] = "" if v is None else str(v)

            well = cleaned_raw.get(well_column, "").strip() or None
            date = month_end_date(
                cleaned_raw.get(year_column), cleaned_raw.get(month_column)
            )

            values: dict[str, float | None] = {}
            from .loader import parse_number

            for k, v in cleaned_raw.items():
                if k in (well_column, year_column, month_column):
                    continue
                values[k] = parse_number(v)

            rows.append(
                Row(
                    line=line,
                    raw=cleaned_raw,
                    well_id=well,
                    date=date,
                    values=values,
                    clean={},
                )
            )

    return rows, columns, units


def _units_from_row(columns: list[str], raw: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for col, val in raw.items():
        if col is None:
            continue
        token = str(val or "").strip()
        if token and token.lower() not in ("null", "none", "-", "n/a"):
            out[col.strip()] = token
    return out