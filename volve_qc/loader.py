"""CSV loading that never mutates raw values.

Parsed values and raw strings are kept side by side. A correction can only ever
write to the ``clean`` view; ``raw`` stays exactly as it arrived on disk.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import AuditReport, Severity
from ._version import RULESET_VERSION, TOOL, __version__

TOOL_VERSION = __version__

MAX_CELL_BYTES = 1_048_576
MAX_ROWS = 5_000_000


class LoadError(Exception):
    pass


@dataclass
class Row:
    """One input record. ``line`` is the 1-indexed file line, header included."""

    line: int
    raw: dict[str, str]
    well_id: str | None
    date: str | None
    values: dict[str, float | None] = field(default_factory=dict)
    clean: dict[str, Any] = field(default_factory=dict)
    invalid_numeric: set[str] = field(default_factory=set)
    """Columns holding non-numeric text *in a column that is otherwise numeric*.

    A text column such as ``FLOW_KIND`` = ``production`` is not a finding.
    An oil column containing ``N/A`` is. Which case applies is decided per
    column by :data:`ColumnProfile`, not guessed from the name.
    """

    def num(self, column: str) -> float | None:
        return self.values.get(column)

    def text(self, column: str) -> str | None:
        return self.raw.get(column)


NUMERIC_PARSE_THRESHOLD = 0.8
"""Share of non-empty values in a column that must parse as float for the
column to count as numeric.

A column where a single cell reads ``N/A`` among hundreds of numbers is
corrupt. A column where no value ever parses is a label column, and
complaining about it 15,000 times helps nobody.
"""

MIN_PROFILE_SAMPLES = 5
"""Below this many non-empty values a column's type is undecidable.

The rule then assumes *numeric*, so a small file cannot hide a real defect
just because there was not enough data to prove the column is a label.
"""


@dataclass
class ColumnProfile:
    """Per-column type inference over the whole file."""

    nonempty: int = 0
    parsed: int = 0
    nulls: int = 0
    is_numeric: bool = False


def profile_columns(rows: list[Row], columns: list[str]) -> dict[str, ColumnProfile]:
    """Decide per column whether it is numeric, from its own contents.

    Name heuristics are not enough: ``ON_STREAM_HRS`` and
    ``AVG_DOWNHOLE_PRESSURE`` are numeric but carry no fluid name, while
    ``FLOW_KIND`` and ``NPD_FIELD_NAME`` are pure text. Only the data settles
    which is which.
    """
    prof: dict[str, ColumnProfile] = {c: ColumnProfile() for c in columns}
    for r in rows:
        for col, v in r.raw.items():
            p = prof.get(col)
            if p is None:
                continue
            if is_null(v):
                # Absent values carry no information about the column's type.
                # Counting them would let a sparse numeric column fall below
                # the threshold and be mistaken for a label column.
                p.nulls += 1
                continue
            p.nonempty += 1
            if r.values.get(col) is not None:
                p.parsed += 1
    for p in prof.values():
        if p.nonempty == 0:
            p.is_numeric = False
        elif p.nonempty < MIN_PROFILE_SAMPLES:
            p.is_numeric = True
        else:
            p.is_numeric = p.parsed / p.nonempty >= NUMERIC_PARSE_THRESHOLD
    return prof


def null_rate(rows: list[Row], column: str) -> tuple[int, int]:
    """(declared-null count, non-empty count) for one column."""
    nulls = 0
    nonempty = 0
    for r in rows:
        v = r.raw.get(column)
        if v is None or not str(v).strip():
            continue
        nonempty += 1
        if is_null(v):
            nulls += 1
    return nulls, nonempty


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise_date(value: Any) -> str | None:
    """Best-effort date normalisation. Returns None if unparseable.

    Accepts real date/datetime objects as well as strings: spreadsheet exports
    hand back typed cells, and a datetime silently normalising to None would
    turn a good row into a QC002 finding.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    v = str(value).strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%m/%d/%Y", "%d-%b-%Y", "%b %d %Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v).date().isoformat()
    except ValueError:
        return None


NULL_TOKENS = frozenset(
    {
        "",
        "-",
        "n/a",
        "na",
        "n.a.",
        "null",
        "none",
        "nil",
        "nan",
        "#n/a",
        "#null!",
        "#value!",
        "unknown",
    }
)
"""Explicit null markers.

These are a value, not corruption. A numeric column holding a few of them is
still a numeric column, and coercing them to None is what a spreadsheet would
do anyway. Anything outside this set is a genuine parse failure.
"""


def is_null(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in NULL_TOKENS


def parse_number(value: Any) -> float | None:
    """Parse a numeric cell, tolerating thousands separators and unit suffixes."""
    if is_null(value):
        return None
    cleaned = str(value).replace(",", "").replace(" ", "")
    for suffix in ("bbl/d", "stb/d", "bbl", "sm3/d", "m3/d", "sm3", "mcf/d", "%"):
        if cleaned.lower().endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    try:
        return float(cleaned)
    except ValueError:
        return None


def load_production_csv(
    path: Path,
    report: AuditReport | None = None,
    well_column: str = "well_id",
    date_column: str = "date",
) -> tuple[list[Row], list[str], AuditReport]:
    """Read a production CSV into Rows plus the report skeleton.

    Non-fatal problems (oversized cell, unparseable number) are recorded as
    issues rather than raised, so one bad row never costs you the whole file.
    """
    path = Path(path)
    if not path.exists():
        raise LoadError(f"input not found: {path}")
    if not path.is_file():
        raise LoadError(f"not a regular file: {path}")

    report = report or AuditReport(
        tool=TOOL,
        tool_version=TOOL_VERSION,
        input_path=str(path.resolve()),
        input_sha256=sha256_file(path),
        run_started=_now(),
        ruleset_version=RULESET_VERSION,
    )

    rows: list[Row] = []
    wells: set[str] = set()
    columns: list[str] = []

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise LoadError(f"empty file, no header: {path}")
        columns = [c.strip() for c in reader.fieldnames]
        reader.fieldnames = columns

        if well_column not in columns:
            raise LoadError(f"missing required column '{well_column}'; found {columns}")
        if date_column not in columns:
            raise LoadError(f"missing required column '{date_column}'; found {columns}")

        for line, raw in enumerate(reader, start=2):
            if len(rows) >= MAX_ROWS:
                report.add(
                    _row_issue(
                        "QC000_ROW_LIMIT",
                        Severity.CRITICAL,
                        f"row limit {MAX_ROWS:,} reached, file truncated",
                        line,
                        expected=f"<= {MAX_ROWS:,} rows",
                    )
                )
                break

            cleaned_raw: dict[str, str] = {}
            for k, v in raw.items():
                if k is None:
                    continue
                if isinstance(v, str) and len(v.encode()) > MAX_CELL_BYTES:
                    report.add(
                        _row_issue(
                            "QC000_CELL_OVERSIZE",
                            Severity.HIGH,
                            f"cell in column '{k}' exceeds {MAX_CELL_BYTES:,} bytes",
                            line,
                            column=k,
                            observed=len(v.encode()),
                            expected=f"<= {MAX_CELL_BYTES:,} bytes",
                            suggestion="truncate or split the upstream export",
                        )
                    )
                    v = v[:MAX_CELL_BYTES]
                cleaned_raw[k] = v if isinstance(v, str) else ("" if v is None else str(v))

            well = (cleaned_raw.get(well_column) or "").strip() or None
            date = normalise_date(cleaned_raw.get(date_column, ""))
            if well:
                wells.add(well)

            row = Row(line=line, raw=cleaned_raw, well_id=well, date=date)
            for k, v in cleaned_raw.items():
                if k in (well_column, date_column):
                    continue
                parsed = parse_number(v)
                row.values[k] = parsed
                # A declared null is not a finding. Only genuine text in a
                # numeric column is, and the column test happens after the
                # file is fully read.
                if parsed is None and not is_null(v):
                    row.invalid_numeric.add(k)
            row.clean = dict(cleaned_raw)

            rows.append(row)

    prof = profile_columns(rows, columns)
    for r in rows:
        # Only flag text inside a column that is otherwise numeric. A label
        # column is not corrupt data, and QC003 on one buries the real hits.
        r.invalid_numeric = {c for c in r.invalid_numeric if prof[c].is_numeric}

    # Identifier and date columns are the business of QC001/QC002. Classifying
    # them as text here would be misleading: they are neither numeric nor
    # candidate data values.
    other = [c for c in columns if c not in (well_column, date_column)]
    report.rows_read = len(rows)
    report.wells_seen = len(wells)
    report.config["numeric_columns"] = sorted(c for c in other if prof[c].is_numeric)
    report.config["text_columns"] = sorted(c for c in other if not prof[c].is_numeric)
    return rows, columns, report


def _row_issue(
    rule_id: str,
    severity: Severity,
    message: str,
    line: int,
    *,
    well_id: str | None = None,
    date: str | None = None,
    column: str | None = None,
    observed: Any = None,
    expected: str | None = None,
    unit: str | None = None,
    auto_correctable: bool = False,
    suggestion: str | None = None,
) -> Any:
    from .schema import Issue

    return Issue(
        rule_id=rule_id,
        well_id=well_id,
        date=date,
        severity=severity,
        message=message,
        source_row=line,
        column=column,
        observed=observed,
        expected=expected,
        unit=unit,
        auto_correctable=auto_correctable,
        suggestion=suggestion,
        original_value=observed,
    )
