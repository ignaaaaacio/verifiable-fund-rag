"""Tests for the data-spike script, using a small synthetic zip."""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import sec_spike  # noqa: E402

NUM = (
    "adsh\ttag\tversion\tseries\tclass\tvalue\n"
    "0001-24-000001\tExpensesOverAssets\trr/2023\tS000000001\tC000000001\t0.0005\n"
    "0001-24-000001\tExpenseExampleYear01\trr/2023\tS000000001\tC000000001\t5\n"
    "0001-24-000001\tAverageAnnualReturnYear05\trr/2023\tS000000001\tC000000001\t0.081\n"
    "0002-25-000002\tExpensesOverAssets\toef/2025\tS000000002\tC000000002\t0.0100\n"
)
TXT = (
    "adsh\ttag\tversion\tvalue\n"
    "0001-24-000001\tRiskNarrativeTextBlock\trr/2023\tMarket risk is ...\n"
)
SUB = "adsh\tcik\tname\n0001-24-000001\t0000000001\tExample Funds\n"


def make_zip(tmp_path: Path) -> Path:
    path = tmp_path / "sample_rr.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("sub.tsv", SUB)
        zf.writestr("num.tsv", NUM)
        zf.writestr("txt.tsv", TXT)
        zf.writestr("readme.htm", "<html></html>")
    return path


def test_analyze_counts_groups_and_versions(tmp_path):
    result = sec_spike.analyze(make_zip(tmp_path))
    assert result["versions"]["rr"] == 4
    assert result["versions"]["oef"] == 1
    fees = result["groups"]["fees_and_expense_ratios"]
    assert fees["series"] == 2 and fees["classes"] == 2
    assert result["groups"]["expense_example"]["distinct_tags"] == 1
    assert result["groups"]["returns"]["distinct_tags"] == 1
    assert result["groups"]["risk"]["distinct_tags"] == 1


def test_main_writes_report(tmp_path):
    out = tmp_path / "report.md"
    assert sec_spike.main(["--zip", str(make_zip(tmp_path)), "--out", str(out)]) == 0
    text = out.read_text()
    assert "Return tags present: yes" in text
    assert "num.tsv" in text and "(not a table)" in text


def test_url_requires_user_agent(tmp_path):
    try:
        sec_spike.main(["--url", "https://example.com/x.zip", "--out", str(tmp_path / "r.md")])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected argparse to exit")
