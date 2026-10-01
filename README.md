# volve-qc

Deterministic data-quality checks for oil and gas production data, with an
audit trail you can hand to someone who did not run the tool.

Built against the [Equinor Volve](https://www.equinor.com/energy/volve-data-sharing)
production dataset, but nothing here is Volve-specific: point it at any
production CSV with a well column and a date column.

## Design stance

Three rules, in priority order:

1. **Never corrupt source data.** The tool reads and reports. It does not
   rewrite your production history. Corrections, when they exist, are opt-in,
   written to a separate file, and recorded in the audit log.
2. **Never guess.** A unit, a column meaning, or a reporting cadence is not
   inferred when inference could put a false claim into the audit record. When
   the tool cannot establish something, it says so explicitly rather than
   reporting "clean".
3. **Never be untraceable.** Every finding carries a stable `rule_id`, a
   severity, a 1-indexed `source_row` you can open in an editor, the observed
   value, and the input's SHA-256.

This is not a wrapper around a model. There is no LLM call, no network access,
and no randomness. The same bytes always produce the same report.

## Install

```bash
pip install volve-qc
```

Or from a checkout:

```bash
pip install -e ".[dev]"
```

Python 3.9+. Zero runtime dependencies — standard library only.

## Quick start

```bash
volve-qc -i production.csv \
         --well-column WELL_BORE_CODE \
         --date-column DATEPRD \
         --declare-unit BORE_OIL_VOL=Sm3 \
         --declare-unit BORE_GAS_VOL=Sm3 \
         --declare-unit BORE_WAT_VOL=Sm3 \
         --declare-unit BORE_WI_VOL=Sm3 \
         -r audit.json
```

```text
rows=15634 wells=7 issues=42
  critical=0 high=8 medium=34 low=0 info=0
report -> audit.json
```

On the real Volve daily export, those 8 high findings are 4 date gaps and 4
negative water rates:

```json
{
  "rule_id": "QC010_NEGATIVE_RATE",
  "well_id": "F-12 H",
  "date": "2012-08-13",
  "severity": "high",
  "message": "negative water production: -457.84",
  "source_row": 7214,
  "column": "BORE_WAT_VOL",
  "observed": -457.84,
  "unit": "Sm3",
  "suggestion": "a production rate cannot be negative; check for a reversed sign convention or a meter fault"
}
```

## Units are declared, never inferred

A production number without a unit is not interpretable, and a wrong unit in an
audit record is worse than a missing check. Volve reports production in Sm³
with no unit in the column name, so the unit has to be stated:

```bash
--declare-unit BORE_OIL_VOL=Sm3
```

The tool will **not** infer `bbl` from the word "oil". Malformed or unknown
declarations exit `2` with the supported set printed.

Conversion targets are canonical per fluid:

| Fluid | Canonical | Notes |
|-------|-----------|-------|
| oil  | bbl      | `1 Sm³ = 6.289810770432105 bbl`, API MPMS 14.3.2 stock tank |
| water| bbl      | same |
| gas  | MCF      | `1 Sm³ = 0.0353146667215 MCF`, geometric ft³ conversion |

The gas Sm³ factor is geometric and assumes standard cubic metres at
15.6 °C. **Volve's actual standard conditions are unconfirmed.** The ceiling
for gas is 5,000,000 MCF/d, so this assumption cannot change a pass/fail
verdict today, but treat it as unverified for other datasets.

If a rate column has no declared unit, the tool skips the plausibility check
and emits an INFO finding naming the columns it could not cover. A skipped
check never looks like a clean one.

## Rules

| Rule | Default | Fires on |
|------|---------|---------|
| `QC001_MISSING_WELL` | critical | row has no well identifier |
| `QC002_MISSING_DATE` | critical | row has no parseable date |
| `QC003_UNPARSEABLE_NUMBER` | high | numeric column holds a non-numeric value |
| `QC004_DUPLICATE_WELL_DATE` | high | same well reported twice for one date |
| `QC010_NEGATIVE_RATE` | high | negative rate; medium for injection columns |
| `QC011_METER_ROLLOVER` | high | cumulative total decreased |
| `QC012_DATE_GAP` | medium | gap beyond the well's own reporting cadence |
| `QC013_DAYS_ON_RANGE` | medium | `days_on` outside 0–31 |
| `QC014_SHUTIN_PRODUCTION` | high | `days_on = 0` but rates are non-zero |
| `QC015_IMPLAUSIBLE_RATE` | medium | rate exceeds physical plausibility ceiling |
| `QC016_CUMULATE_JUMP` | medium | implied daily rate from cumulative change is implausible |

`volve-qc --list-rules` prints the same table at runtime.

Run a subset with repeatable `--rule QC012_DATE_GAP`.

### Default plausibility ceilings

| Fluid | Ceiling |
|-------|---------|
| oil  | 200,000 bbl/d |
| water | 500,000 bbl/d |
| gas  | 5,000,000 MCF/d |

These are absolute sanity bounds, not business limits. Override with
`--ceiling-oil`, `--ceiling-water`, `--ceiling-gas`. All three must be supplied
together.

**Ceilings are in canonical units, not source units.** A monthly total compared
against a daily ceiling is a category error, so daily rates are normalised by
`ON_STREAM_HRS / 24`, `days_on`, or the inferred reporting cadence before
comparison.

## Schema awareness

Volve's daily export uses `ON_STREAM_HRS` rather than a day count, so a daily
rate is `volume / (hours / 24)`. Without this, every rate is wrong by the uptime
factor and `QC015` would be meaningless.

Fluid detection is token-based, not substring-based:

| Column | Interpreted as |
|--------|----------------|
| `BORE_OIL_VOL` | oil production |
| `BORE_GAS_VOL` | gas production |
| `BORE_WAT_VOL` | **water production** |
| `BORE_WI_VOL` | water injection |
| `GI` | gas injection |

`WAT` and `WI` are different things, and a substring match conflates them. So
does `SWIFT_VOLUME`, which is a metadata column, not a water rate.

### Null handling

`NULL`, `N/A`, `NA`, `-`, `None`, `NaN`, and spreadsheet error markers are
treated as absent values, **not** as `QC003` corruption. Declared nulls are
excluded from numeric parse-rate profiling, so a column that is 75% numeric and
25% null is not misclassified as 25% corrupt.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | completed; no finding at or above `--fail-on` |
| 1 | completed; a finding met or exceeded `--fail-on` |
| 2 | bad invocation: unknown rule, malformed unit declaration, missing input |

`--fail-on` defaults to `never`, so the tool reports without failing a pipeline.
Set `--fail-on high` to gate CI on the serious findings.

## Output

`-r audit.json` writes a single JSON document. `--format jsonl` writes a header
record followed by one line per issue, for `jq` and log shipping. The report
always goes to a file; `-r -` does not mean stdout and would create a file
named `-`.

```bash
volve-qc -i production.csv --format jsonl -r audit.jsonl \
  && jq -r 'select(.severity=="high") | "\(.well_id) \(.date) \(.message)"' audit.jsonl
```

The report embeds `input.sha256`, `tool_version`, `ruleset_version`, the full
runtime config, and per-rule and per-severity counts.

`ruleset_version` is separate from the package version and is bumped whenever a
rule changes what it fires on or claims — including this release, where
`QC010` gained produced water and `QC015` gained unit conversion.

## Library use

```python
from volve_qc import load_production_csv, run_all

rows, columns, report = load_production_csv("production.csv", well_column="WELL_BORE_CODE")
report.extend(run_all(rows, columns, unit_declarations={"BORE_OIL_VOL": "Sm3"}))

print(report.summary())
for issue in report.issues:
    print(issue.rule_id, issue.source_row, issue.message)
```

## Validation

The reference numbers come from the real Volve production export, not from the
synthetic fixture:

- **Daily:** 15,634 rows, 7 wells, 42 findings (38 date gaps, 4 negative
  water rates), 0 plausibility breaches.
- **Monthly:** 526 rows, 7 wells, 0 findings.
- Peak rates recomputed independently outside the tool: oil 152,878 bbl/d,
  water 71,600 bbl/d, gas 45,090 MCF/d — all below ceiling, confirming the
  zero-breach result is correct rather than a silent skip.

## Data licensing

The Volve dataset is published by Equinor. Redistribution and commercial use
terms are not resolved here, and **no Volve data is bundled with this package**.
It downloads nothing. Point it at data you already have.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
python -m unittest discover -s tests -v
```

61 tests. All offline; no network calls, no fixtures from the internet.

## License

Apache-2.0
