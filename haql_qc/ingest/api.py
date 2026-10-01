"""Top-level ingest entry point.

``ingest_file`` is the whole public surface: bytes in, canonical CSV out, plus a
report describing every decision made along the way. The contract is that the
report is complete enough that a reviewer who did not run the tool can tell
whether to trust it.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._version import TOOL, __version__
from ..loader import sha256_file
from . import sniff
from .schema_map import SchemaMap, map_schema
from .sniff import Detection
from .tables import IngestError, Table, read_bytes_for_detection

MAX_HEADER_SCAN = 12


@dataclass
class IngestResult:
    """Everything the caller needs: the CSV, and the reasoning behind it."""

    csv_text: str
    detection: Detection
    schema: SchemaMap
    source_path: Path
    source_sha256: str
    table: Table
    role_overrides: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def well_column(self) -> str | None:
        return self.schema.column_for("well")

    @property
    def date_column(self) -> str | None:
        return self.schema.column_for("date")

    @property
    def year_column(self) -> str | None:
        return self.schema.column_for("period_year")

    @property
    def month_column(self) -> str | None:
        return self.schema.column_for("period_month")

    @property
    def units(self) -> dict[str, str]:
        """Units from the file, merged with any explicit operator override."""
        declared = dict(self.schema.units)
        declared.update(self.schema.operator_units)
        return declared

    def manifest(self) -> dict[str, Any]:
        """The auditable record of this ingest."""
        return {
            "tool": TOOL,
            "tool_version": __version__,
            "source": {
                "path": str(self.source_path),
                "sha256": self.source_sha256,
                "detected_format": self.detection.format,
                "detection_reason": self.detection.reason,
                "member": self.detection.member,
                "sheet": self.table.sheet,
                "header_row": self.table.header_line,
                "available_sheets": self.table.available_sheets,
                "rows_extracted": len(self.table.rows),
                "truncated": self.table.truncated,
            },
            "schema": self.schema.to_dict(),
            "role_overrides": dict(self.role_overrides),
            "warnings": list(self.warnings),
            "notes": list(self.table.notes) + list(self.detection.notes),
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


def sniff_file(path: Path) -> Detection:
    """Identify a file without reading its whole contents.

    Separated from :func:`ingest_file` so a caller can ask "what is this?" and
    get an answer, including for a format that will be refused, without
    triggering the refusal as an exception.
    """
    return sniff.detect(Path(path))


def ingest_file(
    path: Path | str,
    *,
    well_column: str | None = None,
    date_column: str | None = None,
    year_column: str | None = None,
    month_column: str | None = None,
    unit_declarations: dict[str, str] | None = None,
    sheet: str | None = None,
    member: str | None = None,
    max_rows: int | None = None,
) -> IngestResult:
    """Convert any supported production export into canonical CSV.

    Parameters are overrides. Passing ``well_column`` does not stop the other
    columns from being mapped; it tells the mapper which column *you* mean, so
    a file where the tool guessed wrong can be corrected without re-running the
    analysis by hand.

    The result is never a silent coercion. A column the file did not declare a
    unit for stays undeclared in the output, and a role the tool could not
    resolve is listed in ``schema.unresolved`` rather than defaulted.
    """
    path = Path(path)
    det = sniff.detect(path, member=member)
    if not det.supported:
        # A file that is missing, unreadable or empty carries the actionable
        # reason in det.reason; only a genuinely unrecognised format falls back
        # to the format-level message.
        raise IngestError(
            det.reason
            if det.format == sniff.UNKNOWN and det.reason
            else det.unsupported_reason or f"unsupported format: {det.format}"
        )

    if sheet and det.format == sniff.XLSX:
        det.detail["sheet"] = sheet

    table = read_bytes_for_detection(path, det)

    file_units = table.units_row()
    data = table.without_units_row() if file_units else table

    if max_rows is not None and len(data.rows) > max_rows:
        data.rows = data.rows[:max_rows]
        data.notes.append(f"truncated to {max_rows} rows on request")

    schema = map_schema(
        data.columns,
        data.as_dict(),
        file_units=file_units,
        well_column=well_column,
        date_column=date_column,
        year_column=year_column,
        month_column=month_column,
    )

    if unit_declarations:
        from ..units import normalise_unit_token

        resolved: dict[str, str] = {}
        for col, token in unit_declarations.items():
            unit = normalise_unit_token(token)
            if unit is None:
                raise IngestError(
                    f"unknown unit {token!r} for column {col!r}; refusing to "
                    f"write an unrecognised unit into the audit record"
                )
            resolved[col] = unit
            schema.operator_units[col] = unit
            schema.unit_sources[col] = "operator"

    warnings: list[str] = []
    warnings.extend(schema.unresolved)
    if data.truncated:
        warnings.append("row limit reached; file truncated")

    csv_text = _to_canonical_csv(data)

    return IngestResult(
        csv_text=csv_text,
        detection=det,
        schema=schema,
        source_path=path,
        source_sha256=sha256_file(path),
        table=data,
        warnings=warnings,
    )


def _to_canonical_csv(table: Table) -> str:
    """Write the table as RFC 4180 CSV with LF newlines.

    Determinism is the point. The same input must produce byte-identical output
    on every run and every platform, because the output's SHA-256 goes into the
    audit record and a report that cannot be reproduced cannot be defended.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(table.columns)
    for row in table.rows:
        writer.writerow([row[i] if i < len(row) else "" for i in range(len(table.columns))])
    return buf.getvalue()


def load_any(
    path: Path | str,
    *,
    well_column: str | None = None,
    date_column: str | None = None,
    year_column: str | None = None,
    month_column: str | None = None,
    unit_declarations: dict[str, str] | None = None,
    sheet: str | None = None,
    member: str | None = None,
    limits: bool = True,
) -> tuple[Any, str, Any]:
    """Read any supported file straight into loader Rows.

    Returns ``(rows, columns, report)`` exactly as :func:`load_production_csv`
    does, so every existing rule works unchanged on an XLSX or JSON export.

    The audit report records the source file, not the temporary CSV. Hashing the
    intermediate would let the two drift apart, and a report that points at a
    path the reviewer cannot open is not evidence of anything.
    """
    from ..loader import MAX_ROWS, Row, parse_number
    from ..schema import AuditReport

    result = ingest_file(
        path,
        well_column=well_column,
        date_column=date_column,
        year_column=year_column,
        month_column=month_column,
        unit_declarations=unit_declarations,
        sheet=sheet,
        member=member,
    )

    table = result.table
    well_col = result.well_column
    date_col = result.date_column
    yr_col = result.year_column
    mo_col = result.month_column

    if well_col is None:
        raise IngestError(
            "could not identify the well identifier column; pass well_column"
        )

    is_period = yr_col is not None and mo_col is not None
    if not is_period and date_col is None:
        raise IngestError(
            "could not identify a date column; pass date_column, or year_column "
            "and month_column for a period file"
        )

    report = AuditReport(
        tool=TOOL,
        tool_version=__version__,
        input_path=str(Path(path).resolve()),
        input_sha256=result.source_sha256,
        run_started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    report.config["ingest"] = result.manifest()
    report.config["granularity"] = "period" if is_period else "daily"
    report.config["well_column"] = well_col
    report.config["date_column"] = date_col
    report.config["year_column"] = yr_col
    report.config["month_column"] = mo_col

    rows: list[Row] = []
    limit = MAX_ROWS if limits else None
    for i, raw_row in enumerate(table.rows):
        if limit is not None and len(rows) >= limit:
            break
        # Source line: header row, then units row if it was consumed. Tracked so
        # a finding points at the line a human sees in their spreadsheet.
        line = i + table.header_line + 2 + (1 if result.schema.units else 0)
        raw = {
            c: (raw_row[j] if j < len(raw_row) else "")
            for j, c in enumerate(table.columns)
        }

        well = (raw.get(well_col) or "").strip() or None

        if is_period:
            from ..period import month_end_date

            date = month_end_date(raw.get(yr_col), raw.get(mo_col))
        else:
            from ..loader import normalise_date

            date = normalise_date(raw.get(date_col, ""))

        row = Row(line=line, raw=raw, well_id=well, date=date)
        skip = {well_col, date_col, yr_col, mo_col}
        for k, v in raw.items():
            if k in skip:
                continue
            parsed = parse_number(v)
            row.values[k] = parsed
            if parsed is None and v and v.strip().lower() not in {"n/a", "na", "null", "-", "none"}:
                row.invalid_numeric.add(k)
        row.clean = dict(raw)
        rows.append(row)

    from ..loader import profile_columns

    prof = profile_columns(rows, table.columns)
    for r in rows:
        r.invalid_numeric = {c for c in r.invalid_numeric if prof[c].is_numeric}

    report.rows_read = len(rows)
    report.wells_seen = len({r.well_id for r in rows if r.well_id})
    report.config["numeric_columns"] = sorted(
        c for c in table.columns if prof[c].is_numeric and c not in (well_col, date_col, yr_col, mo_col)
    )
    report.config["text_columns"] = sorted(
        c for c in table.columns if not prof[c].is_numeric and c not in (well_col, date_col, yr_col, mo_col)
    )
    report.config["units_declared_by_file"] = dict(result.schema.units)

    return rows, list(table.columns), report


def manifest_json(result: IngestResult, indent: int | None = 2) -> str:
    """Render the ingest manifest as JSON, for piping into another tool."""
    return json.dumps(result.manifest(), indent=indent, sort_keys=False, default=str)