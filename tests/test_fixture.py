"""End-to-end test against a Volve-shaped fixture with known defects.

Run: python -m unittest tests.test_fixture -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fixtures.volve_like import EXPECTED_FINDINGS, HEADER, ROWS
from volve_qc.loader import load_production_csv
from volve_qc.rules import run_all


def build() -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "volve_like.csv"
    p.write_text(HEADER + "\n" + "\n".join(ROWS) + "\n", encoding="utf-8")
    return p


def audit():
    rows, cols, rep = load_production_csv(build(), well_column="well", date_column="date")
    rep.extend(run_all(rows, cols))
    return rows, cols, rep


class TestFixture(unittest.TestCase):
    def test_expected_finding_counts(self):
        _, _, rep = audit()
        actual = rep.by_rule()
        for rule_id, count in EXPECTED_FINDINGS.items():
            self.assertEqual(
                actual.get(rule_id, 0),
                count,
                f"{rule_id}: expected {count}, got {actual.get(rule_id, 0)}",
            )

    def test_no_unexpected_rules_fired(self):
        _, _, rep = audit()
        actual = set(rep.by_rule())
        expected = set(EXPECTED_FINDINGS)
        self.assertEqual(
            actual - expected,
            set(),
            f"unexpected rules fired: {sorted(actual - expected)}",
        )

    def test_monthly_well_not_flagged(self):
        """B-1 is monthly and clean. It must produce zero findings."""
        _, _, rep = audit()
        b1 = [i for i in rep.issues if i.well_id == "B-1"]
        self.assertEqual(b1, [], "monthly clean well should be silent")

    def test_gap_issue_context(self):
        _, _, rep = audit()
        gaps = [i for i in rep.issues if i.rule_id == "QC012_DATE_GAP"]
        self.assertEqual(len(gaps), 1)
        ctx = gaps[0].context
        self.assertEqual(ctx["expected_interval_days"], 1)
        self.assertEqual(ctx["observed_interval_days"], 4)
        self.assertEqual(ctx["gap_start"], "2024-01-08")
        self.assertEqual(ctx["gap_end"], "2024-01-10")

    def test_rollover_not_double_reported(self):
        """A cumulative drop must be QC011 only, never also QC016."""
        _, _, rep = audit()
        rollovers = [i for i in rep.issues if i.rule_id == "QC011_METER_ROLLOVER"]
        self.assertEqual(len(rollovers), 1)
        self.assertEqual(rollovers[0].column, "oil_cum_bbl")
        jumps = [
            i
            for i in rep.issues
            if i.rule_id == "QC016_CUMULATE_JUMP" and i.well_id == "A-1"
        ]
        self.assertEqual(jumps, [], "a decrease is QC011's job, not QC016's")

    def test_shutin_flags_every_rate_column(self):
        _, _, rep = audit()
        shut = [i for i in rep.issues if i.rule_id == "QC014_SHUTIN_PRODUCTION"]
        self.assertEqual(len(shut), 3)
        self.assertEqual({i.well_id for i in shut}, {"C-1"})
        self.assertEqual({i.column for i in shut}, {"oil_bbl_per_d", "water_bbl_per_d", "gas_mcf_per_d"})

    def test_every_issue_has_provenance(self):
        """The contract: rule_id, row, well, severity, expected, suggestion."""
        _, _, rep = audit()
        self.assertTrue(rep.issues)
        for i in rep.issues:
            self.assertTrue(i.rule_id.startswith("QC"))
            self.assertGreaterEqual(i.source_row, 2, "row 1 is the header")
            self.assertIsNotNone(i.well_id, f"{i.rule_id} on row {i.source_row} lost its well")
            self.assertIsNotNone(i.severity)
            self.assertIsNotNone(i.expected, f"{i.rule_id} has no stated constraint")
            self.assertIsNotNone(i.suggestion, f"{i.rule_id} has no suggestion")

    def test_source_rows_resolve_to_expected_wells(self):
        """Row provenance must actually point at the right record."""
        rows, _, rep = audit()
        by_line = {r.line: r for r in rows}
        for i in rep.issues:
            self.assertIn(i.source_row, by_line, f"{i.rule_id} cites a nonexistent row")
            self.assertEqual(
                by_line[i.source_row].well_id,
                i.well_id,
                f"{i.rule_id} row {i.source_row}: well mismatch",
            )

    def test_raw_preserved_throughout(self):
        rows, _, rep = audit()
        n1 = [r for r in rows if r.well_id == "C-2"][0]
        self.assertEqual(n1.raw["gas_mcf_per_d"], "-15.0")
        self.assertEqual(n1.values["gas_mcf_per_d"], -15.0)
        n2 = [r for r in rows if r.well_id == "C-3"][0]
        self.assertEqual(n2.raw["water_bbl_per_d"], "elevenish")
        self.assertIsNone(n2.values["water_bbl_per_d"])
        self.assertIn("water_bbl_per_d", n2.invalid_numeric)

    pass  # E-1 null handling already verified in test_qc.

    def test_report_serialises(self):
        _, _, rep = audit()
        d = json.loads(rep.to_json())
        self.assertEqual(d["input"]["rows_read"], len(ROWS))
        self.assertEqual(len(d["summary"]["by_rule"]), len(EXPECTED_FINDINGS))
        self.assertEqual(len(d["issues"]), len(rep.issues))
        lines = rep.to_jsonl().splitlines()
        self.assertEqual(len(lines), len(rep.issues) + 1)

    def test_sha256_pinned(self):
        _, _, rep = audit()
        self.assertEqual(len(rep.input_sha256), 64)
        self.assertEqual(rep.input_sha256, audit()[2].input_sha256, "hash must be stable")

    def test_cumulative_rate_check_fires_on_unit_error(self):
        """A cumulative total in the wrong unit shows up as an implied rate.

        The default oil ceiling is 200,000 bbl/d, which is deliberately loose, so
        this test tightens it to a plausible field rate. A cumulative column
        that jumps 5,000 bbl in one day is a unit error even though it is well
        under the absolute sanity limit.
        """
        d = Path(tempfile.mkdtemp())
        p = d / "cum.csv"
        p.write_text(
            HEADER + "\n"
            + "\n".join(
                [
                    "E-1,2024-01-01,100.0,1.0,50.0,1000.0,10.0,500.0,1",
                    "E-1,2024-01-02,100.0,1.0,50.0,1100.0,11.0,550.0,1",
                    "E-1,2024-01-03,100.0,1.0,50.0,1200.0,12.0,600.0,1",
                    # 5,000 bbl jump in one day against a 1,000 bbl/d ceiling
                    "E-1,2024-01-04,100.0,1.0,50.0,6200.0,13.0,650.0,1",
                    "E-1,2024-01-05,100.0,1.0,50.0,6300.0,14.0,700.0,1",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        rows, cols, rep = load_production_csv(p, well_column="well", date_column="date")
        rep.extend(
            run_all(
                rows,
                cols,
                ceilings={"oil": 1000.0, "water": 1000.0, "gas": 10_000.0},
            )
        )
        jumps = [i for i in rep.issues if i.rule_id == "QC016_CUMULATE_JUMP"]
        self.assertTrue(jumps, "5000 bbl/d implied rate should exceed a 1000 bbl/d ceiling")
        self.assertEqual(jumps[0].well_id, "E-1")
        self.assertEqual(jumps[0].context["delta"], 5000.0)
        self.assertEqual(jumps[0].context["days"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
