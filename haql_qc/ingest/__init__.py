"""Ingest: any supported production export in, canonical CSV out.

Public surface:

    from haql_qc.ingest import ingest_file, load_any, sniff_file

``ingest_file`` returns the canonical CSV text plus a manifest describing every
decision made to produce it. ``load_any`` goes further and returns loader Rows
ready for the ruleset, so an XLSX or JSON export runs through the same eleven
rules with no change to the rules themselves.

The layer's governing rule: never guess silently. A role inferred at low
confidence is reported as an open question rather than applied, and a unit the
file did not declare stays undeclared in the output.
"""

from __future__ import annotations

from pathlib import Path

from .api import IngestResult, ingest_file, load_any, manifest_json, sniff_file
from .schema_map import (
    HIGH,
    LOW,
    MEDIUM,
    NONE,
    Cadence,
    RoleAssignment,
    SchemaMap,
    map_schema,
)
from .sniff import READABLE, Detection, detect
from .tables import IngestError, Table

__all__ = [
    "IngestResult",
    "ingest_file",
    "load_any",
    "manifest_json",
    "sniff_file",
    "SchemaMap",
    "RoleAssignment",
    "Cadence",
    "map_schema",
    "Detection",
    "detect",
    "Table",
    "IngestError",
    "READABLE",
    "HIGH",
    "MEDIUM",
    "LOW",
    "NONE",
]


def convert(
    src: Path | str,
    dst: Path | str,
    **kwargs,
) -> IngestResult:
    """Write the canonical CSV for ``src`` to ``dst`` and return the manifest.

    A convenience wrapper over :func:`ingest_file` for the common case. The
    source file is opened read-only and is never modified.
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = ingest_file(src, **kwargs)
    dst.write_text(result.csv_text, encoding="utf-8")
    return result