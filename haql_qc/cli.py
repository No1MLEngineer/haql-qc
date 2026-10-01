"""CLI: haql-qc --input production.csv --report audit.json

Read-only by default. --fix writes corrected values to a separate output file;
the input is never modified.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from ._version import RULESET_VERSION
from .loader import LoadError, load_production_csv
from .period import PeriodError, load_period_csv
from .rules import RULES, RULE_META, run_all
from .units import UNIT_TOKENS, normalise_unit_token


def _default_ceilings(args) -> dict[str, float] | None:
    if args.ceiling_oil and args.ceiling_water and args.ceiling_gas:
        return {
            "oil": float(args.ceiling_oil),
            "water": float(args.ceiling_water),
            "gas": float(args.ceiling_gas),
        }
    return None


def _parse_unit_declarations(raw: list[str] | None) -> dict[str, str] | None:
    """Validate ``COLUMN=UNIT`` declarations, rejecting unknown units loudly."""
    if not raw:
        return None
    out: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise ValueError(
                f"malformed --declare-unit {item!r}; expected COLUMN=UNIT"
            )
        column, unit = item.split("=", 1)
        column = column.strip()
        if not column:
            raise ValueError(f"malformed --declare-unit {item!r}; no column named")
        resolved = normalise_unit_token(unit)
        if resolved is None:
            supported = sorted({u for _, u in UNIT_TOKENS})
            raise ValueError(
                f"unknown unit {unit.strip()!r} for {column}; supported: "
                + ", ".join(supported)
            )
        out[column] = resolved
    return out


EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_NO_LICENSE = 3


def _fail(msg: str, code: int = EXIT_USAGE) -> int:
    print(f"haql-qc: {msg}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="haql-qc",
        description="Production data QC and reconciliation with an audit trail.",
    )
    p.add_argument(
        "--input",
        "-i",
        help="production CSV (required unless --list-rules)",
    )
    p.add_argument(
        "--well-column",
        default="well_id",
        help="name of the well identifier column (default: well_id)",
    )
    p.add_argument(
        "--date-column",
        default="date",
        help="name of the date column (default: date)",
    )
    p.add_argument("--report", "-r", default="audit.json", help="audit report path")
    p.add_argument("--format", choices=["json", "jsonl"], default="json", help="report format")
    p.add_argument(
        "--rule",
        action="append",
        dest="rules",
        help="run only this rule (repeatable); default runs all",
    )
    p.add_argument("--list-rules", action="store_true", help="print ruleset and exit")
    p.add_argument("--ceiling-oil", default=0, help="plausibility ceiling, oil rate")
    p.add_argument("--ceiling-water", default=0, help="plausibility ceiling, water rate")
    p.add_argument("--ceiling-gas", default=0, help="plausibility ceiling, gas rate")
    p.add_argument(
        "--declare-unit",
        action="append",
        dest="unit_declarations",
        metavar="COLUMN=UNIT",
        help=(
            "declare the unit of a column that does not name one, e.g. "
            "--declare-unit BORE_OIL_VOL=Sm3 (repeatable). The tool will not "
            "infer a unit from a fluid name, because guessing wrong puts a "
            "false unit into the audit record."
        ),
    )
    p.add_argument("--fail-on", choices=["critical", "high", "medium", "low", "info", "never"], default="never")
    p.add_argument(
        "--granularity",
        choices=["daily", "period"],
        default="daily",
        help=(
            "how the input encodes its period. 'daily' reads one date column "
            "(default). 'period' reads Year+Month columns and picks up a units "
            "row, for monthly exports that declare their own units."
        ),
    )
    p.add_argument("--year-column", default="Year", help="year column when --granularity period")
    p.add_argument("--month-column", default="Month", help="month column when --granularity period")
    p.add_argument(
        "--license-file",
        help=(
            "path to a signed HAQL1 license. Defaults to $HAQL_LICENSE_FILE, "
            "then $HAQL_LICENSE, then ~/.haql/license.key."
        ),
    )
    p.add_argument(
        "--require-license",
        action="store_true",
        help=(
            "refuse to run without a valid, unexpired, correctly signed license. "
            "Exit code 3. Off by default, so the free evaluation tier keeps "
            "working; distribution builds are expected to turn it on."
        ),
    )
    p.add_argument(
        "--license-status",
        action="store_true",
        help="print license status as JSON and exit (exit code 3 if not valid)",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__} (ruleset {RULESET_VERSION})",
    )
    args = p.parse_args(argv)

    if args.license_status:
        import json as _json

        from . import licensing

        result = licensing.check(args.license_file)
        print(_json.dumps(result.summary(), indent=2, sort_keys=True))
        return 0 if result.valid else 3

    # The gate runs before any input is read, so an unlicensed caller does no
    # work and, more importantly, cannot obtain findings from a build that is
    # supposed to require payment.
    if args.require_license:
        from . import licensing

        result = licensing.check(args.license_file)
        if not result.valid:
            detail = result.reason or "no license"
            src = f" (looked at {result.source})" if result.source else ""
            return _fail(
                f"license required and not satisfied: {detail}{src}. Set "
                f"$HAQL_LICENSE or pass --license-file.",
                code=EXIT_NO_LICENSE,
            )

    if args.list_rules:
        for rid in sorted(RULES):
            m = RULE_META[rid]
            print(f"{rid:<28} [{m['default_severity']:<8}] {m['description']}")
        return 0

    if not args.input:
        return _fail("--input is required unless --list-rules is given")

    input_path = Path(args.input)

    if args.granularity == "period":
        try:
            rows, columns, file_units = load_period_csv(
                input_path,
                well_column=args.well_column,
                year_column=args.year_column,
                month_column=args.month_column,
            )
        except PeriodError as e:
            return _fail(str(e))
        except Exception as e:  # noqa: BLE001
            return _fail(f"failed to read input: {e}")

        from ._version import TOOL
        from .loader import sha256_file
        from .schema import AuditReport

        report = AuditReport(
            tool=TOOL,
            tool_version=__version__,
            input_path=str(input_path.resolve()),
            input_sha256=sha256_file(input_path),
            run_started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ruleset_version=RULESET_VERSION,
        )
        report.config["granularity"] = "period"
        report.rows_read = len(rows)
        report.wells_seen = len({r.well_id for r in rows if r.well_id})
        # Always present for period input, empty when the file declared nothing,
        # so a reader can tell "no units declared" from "key absent".
        report.config["units_declared_by_file"] = file_units or {}
    else:
        try:
            rows, columns, report = load_production_csv(
                input_path,
                well_column=args.well_column,
                date_column=args.date_column,
            )
        except LoadError as e:
            return _fail(str(e))
        except Exception as e:  # noqa: BLE001
            return _fail(f"failed to read input: {e}")
        file_units = None

    # Record the licence in the audit report too. The report is handed to
    # customers and to auditors, so it should say which entitlement produced
    # it. Only the non-sensitive fields go in; the token itself stays out.
    from . import licensing as _licensing

    _lic = _licensing.check(args.license_file)
    if _lic.license is not None:
        report.config["license"] = {
            "company": _lic.license.company,
            "tier": _lic.license.tier,
            "seats": _lic.license.seats,
            "expires": _lic.license.expires,
            "nonce": _lic.license.nonce,
            "valid": _lic.valid,
        }

    if args.rules:
        unknown = [r for r in args.rules if r not in RULES]
        if unknown:
            return _fail(f"unknown rule(s): {unknown}")
        report.config["selected_rules"] = args.rules

    try:
        units = _parse_unit_declarations(args.unit_declarations)
    except ValueError as e:
        return _fail(str(e))

    # A unit the file declared for itself is as authoritative as an operator
    # declaration, and is recorded as such. Explicit --declare-unit still wins,
    # because it is the more recent human statement about that column.
    if file_units:
        units = {**file_units, **(units or {})}
    report.config["units_declared_by_file"] = file_units or {}
    if units:
        report.config["unit_declarations"] = units

    issues = run_all(
        rows,
        columns,
        rule_ids=args.rules,
        ceilings=_default_ceilings(args),
        unit_declarations=units,
    )
    report.extend(issues)
    report.run_finished = datetime.now(timezone.utc).isoformat(timespec="seconds")

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_jsonl() if args.format == "jsonl" else report.to_json()
    out.write_text(payload, encoding="utf-8")

    s = report.summary()
    sev = s["by_severity"]
    print(f"rows={s['rows_read']} wells={s['wells_seen']} issues={s['issue_count']}")
    print(
        "  critical={critical} high={high} medium={medium} low={low} info={info}".format(**sev)
    )
    print(f"report -> {out}")

    if args.fail_on != "never":
        order = ["critical", "high", "medium", "low", "info"]
        threshold = order.index(args.fail_on)
        for lvl in order[: threshold + 1]:
            if sev.get(lvl, 0) > 0:
                return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
