"""Tests for the coverage report, using a small synthetic Risk/Return zip."""

import csv
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import coverage  # noqa: E402

SUB = (
    "adsh\tcik\tname\tfiled\taccepted\n"
    "OLD-26-000001\t0000000001\tAlpha Funds\t20260101\t2026-01-01 00:00:00.0\n"
    "NEW-26-000001\t0000000001\tAlpha Funds\t20260601\t2026-06-01 00:00:00.0\n"
    "BETA-26-000001\t0000000002\tBeta Funds\t20260615\t2026-06-15 00:00:00.0\n"
)
SCN = (
    "adsh\tseriesid\tseriesname\tclassid\tclassname\tticker\n"
    "NEW-26-000001\tS000000001\tAlpha Growth Fund\tC000000001\tClass A\tALPHX\n"
    "NEW-26-000001\tS000000001\tAlpha Growth Fund\tC000000002\tClass C\tALPCX\n"
    "BETA-26-000001\tS000000002\tBeta Bond Fund\tC000000003\tInvestor Class\tBETAX\n"
)


def _return_rows(adsh: str, series: str, class_id: str) -> str:
    lines = []
    for qtrs in ("4", "20", "40"):
        lines.append(
            f"{adsh}\tAvgAnnlRtrPct\toef/2026\t{qtrs}\t{series}\t{class_id}\t\t0.05\n"
        )
    lines.append(
        f"{adsh}\tAvgAnnlRtrPct\toef/2026\t4\t{series}\t{class_id}\t"
        "PerformanceMeasure=SP500Index;\t0.10\n"
    )
    lines.append(
        f"{adsh}\tAvgAnnlRtrPct\toef/2026\t4\t{series}\t{class_id}\t"
        "PerformanceMeasure=AfterTaxesOnDistributions;\t0.04\n"
    )
    return "".join(lines)


def _examples(adsh: str, series: str, class_id: str, years: tuple[str, ...]) -> str:
    return "".join(
        f"{adsh}\tExpenseExampleYear{year}\toef/2026\t0\t{series}\t{class_id}\t\t50\n"
        for year in years
    )


NUM_HEADER = "adsh\ttag\tversion\tqtrs\tseries\tclass\totherdims\tvalue\n"
NUM = (
    NUM_HEADER
    # Older filing for C000000001 lacks the expense example. It must not win.
    + "OLD-26-000001\tExpensesOverAssets\toef/2026\t0\tS000000001\tC000000001\t\t0.01\n"
    + "NEW-26-000001\tExpensesOverAssets\toef/2026\t0\tS000000001\tC000000001\t\t0.007\n"
    + _examples("NEW-26-000001", "S000000001", "C000000001", ("01", "03", "05", "10"))
    + _return_rows("NEW-26-000001", "S000000001", "C000000001")
    # C000000002 is missing the 10-year example. A custom-version row must not fill it in.
    + "NEW-26-000001\tNetExpensesOverAssets\toef/2026\t0\tS000000001\tC000000002\t\t0.015\n"
    + _examples("NEW-26-000001", "S000000001", "C000000002", ("01", "03", "05"))
    + "NEW-26-000001\tExpenseExampleYear10\t0000000001-26-000001\t0\tS000000001\tC000000002\t\t99\n"
    + _return_rows("NEW-26-000001", "S000000001", "C000000002")
    + "BETA-26-000001\tExpensesOverAssets\toef/2026\t0\tS000000002\tC000000003\t\t0.005\n"
    + _examples("BETA-26-000001", "S000000002", "C000000003", ("01", "03", "05", "10"))
    + _return_rows("BETA-26-000001", "S000000002", "C000000003")
    # Custom-version class is ignored entirely.
    + "BETA-26-000001\tExpensesOverAssets\t0000000002-26-000099\t0\tS000000009\tC000000099\t\t0.01\n"
)
TXT = (
    "adsh\ttag\tversion\tseries\tclass\tvalue\n"
    "NEW-26-000001\tRiskTextBlock\toef/2026\tS000000001\tC000000001\tMarket risk\n"
    "BETA-26-000001\tRiskTextBlock\toef/2026\tS000000002\tC000000003\tCredit risk\n"
    "BETA-26-000001\tRiskTextBlock\t0000000002-26-000099\tS000000009\tC000000099\tIgnored\n"
)


def make_zip(tmp_path: Path) -> Path:
    path = tmp_path / "rr.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("sub.tsv", SUB)
        zf.writestr("scn.tsv", SCN)
        zf.writestr("num.tsv", NUM)
        zf.writestr("txt.tsv", TXT)
    return path


def test_custom_versions_latest_filing_and_flags(tmp_path):
    result = coverage.analyze(make_zip(tmp_path))
    by_class = {rec["classid"]: rec for rec in result["records"]}
    assert "C000000099" not in by_class
    assert set(by_class) == {"C000000001", "C000000002", "C000000003"}

    first = by_class["C000000001"]
    assert first["adsh"] == "NEW-26-000001"
    assert first["filed"] == "20260601"
    assert first["complete"] is True
    assert first["flags"]["has_returns"] is True

    second = by_class["C000000002"]
    assert second["flags"]["has_example"] is False
    assert second["flags"]["has_expense_ratio"] is True
    assert second["complete"] is False

    third = by_class["C000000003"]
    assert third["complete"] is True
    assert third["seriesname"] == "Beta Bond Fund"
    assert third["ticker"] == "BETAX"


def test_main_writes_candidates_header(tmp_path):
    out = tmp_path / "out"
    assert coverage.main(["--zip", str(make_zip(tmp_path)), "--out-dir", str(out)]) == 0
    text = (out / "coverage.md").read_text()
    assert "AvgAnnlRtrPct" in text
    assert "Series with at least one complete class: 2" in text
    with (out / "candidates.csv").open(newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == coverage.CANDIDATE_FIELDS
        rows = list(reader)
    assert len(rows) == 3
    complete = {row["classid"]: row["complete"] for row in rows}
    assert complete == {"C000000001": "1", "C000000002": "0", "C000000003": "1"}
