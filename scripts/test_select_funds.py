"""Tests for the fund-universe selection, using synthetic candidates and zips."""

import csv
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import select_funds  # noqa: E402

CANDIDATE_HEADER = (
    "seriesid,seriesname,classid,classname,ticker,filer_name,cik,adsh,filed,"
    "has_expense_ratio,has_example,has_returns,has_ticker,has_risk_text,complete\n"
)


def _class_row(series, name, class_id, class_name, ticker, cik, filer, complete="1"):
    return (
        f"{series},{name},{class_id},{class_name},{ticker},{filer},{cik},RR1,20261001,"
        f"1,1,1,1,1,{complete}\n"
    )


def write_candidates(path: Path, rows: str) -> None:
    path.write_text(CANDIDATE_HEADER + rows, encoding="utf-8")


def write_rr(path: Path, num_body: str) -> None:
    num = "adsh\ttag\tversion\tclass\tvalue\n" + num_body
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("num.tsv", num)


def write_nport(path: Path, info_rows: str, submission_rows: str) -> None:
    info = "ACCESSION_NUMBER\tSERIES_ID\tNET_ASSETS\n" + info_rows
    submission = "ACCESSION_NUMBER\tREPORT_DATE\tFILING_DATE\n" + submission_rows
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("FUND_REPORTED_INFO.tsv", info)
        zf.writestr("SUBMISSION.tsv", submission)
        zf.writestr("FUND_REPORTED_HOLDING.tsv", "HOLDING_ID\tVALUE\nshould-not-be-read\t1\n")


def base_rows() -> str:
    return (
        _class_row("S_ETF", "Total Market ETF", "C_ETF", "ETF Class", "ETFX", "1", "ETF Trust")
        + _class_row("S_CONLY", "Only C Fund", "C_ONLY", "Class C", "ONLYX", "2", "Only Funds")
        + _class_row("S_BIG", "Big Growth Fund", "C_BIG_A", "Class A", "BIGAX", "111", "Alpha")
        + _class_row("S_BIG", "Big Growth Fund", "C_BIG_C", "Class C", "BIGCX", "111", "Alpha")
        + _class_row("S_BIG", "Big Growth Fund", "C_BIG_I", "Institutional Class", "BIGIX", "111", "Alpha")
        + _class_row("S_BIG", "Big Growth Fund", "C_BIG_N", "Investor Class", "BIGNX", "111", "Alpha")
        + _class_row("S_SMALL", "Small Growth Fund", "C_SMALL", "Class A", "SMLAX", "111", "Alpha")
        + _class_row("S_BOND", "Municipal Income Fund", "C_BOND", "Investor Class", "BNDIX", "222", "Beta")
        + _class_row("S_NONE", "Value Fund", "C_NONE", "Class A", "VALUX", "333", "Gamma")
        + _class_row("S_HIGH", "High Minimum Growth Fund", "C_HIGH", "Class A", "HIMNX", "444", "Delta")
        + _class_row("S_LOW", "Low Minimum Growth Fund", "C_LOW", "Class A", "LOMNX", "555", "Epsilon")
    )


def base_files(tmp_path: Path):
    candidates = tmp_path / "candidates.csv"
    rr = tmp_path / "rr.zip"
    nport = tmp_path / "nport.zip"
    write_candidates(candidates, base_rows())
    write_rr(
        rr,
        "RR1\tFeeWaiverOrReimbursementOverAssets\toef/2026\tC_BIG_A\t0.0010\n"
        "RR1\tMaximumSalesChargeImposedOnPurchasesOverOfferingPrice\toef/2026\tC_BIG_N\t0.0575\n"
        "RR1\tMinimumInitialInvestment\toef/2026\tC_HIGH\t250000\n"
        "RR1\tMinimumInitialInvestment\toef/2026\tC_LOW\t3000\n"
        "RR1\tMinimumInitialInvestment\t0000000001-26-000099\tC_SMALL\t999999999\n",
    )
    write_nport(
        nport,
        "ACC-OLD\tS_BIG\t100\n"
        "ACC-NEW\tS_BIG\t500\n"
        "ACC-BOND\tS_BOND\t400\n"
        "ACC-LOW\tS_LOW\t300\n"
        "ACC-SMALL\tS_SMALL\t50\n"
        "ACC-ETF\tS_ETF\t9999\n",
        "ACC-OLD\t2026-03-31\t2026-05-01\n"
        "ACC-NEW\t2026-06-30\t2026-08-01\n"
        "ACC-BOND\t2026-06-30\t2026-08-01\n"
        "ACC-LOW\t2026-06-30\t2026-08-01\n"
        "ACC-SMALL\t2026-06-30\t2026-08-01\n"
        "ACC-ETF\t2026-06-30\t2026-08-01\n",
    )
    return candidates, rr, nport


def test_class_types():
    assert select_funds.class_type("Class A") == "A"
    assert select_funds.class_type("Class A Shares") == "A"
    assert select_funds.class_type("Investor A") == "A"
    assert select_funds.class_type("Class C") == "C"
    assert select_funds.class_type("Investor C") == "C"
    assert select_funds.class_type("Class F-1") == "F1"
    assert select_funds.class_type("Class F-2") == "F2"
    assert select_funds.class_type("Class F-3") == "F3"
    assert select_funds.class_type("Class 529-A") == "529A"
    assert select_funds.class_type("Class 529-C") == "529"
    assert select_funds.class_type("Class 529-F-2") == "529"
    assert select_funds.class_type("Investor Class") == "investor"
    assert select_funds.class_type("Admiral Shares") == "investor"
    assert (
        select_funds.class_type(
            "T. Rowe Price International Stock Fund",
            "T. Rowe Price International Stock Fund",
        )
        == "investor"
    )
    assert select_funds.class_type("Class I", "Dodge & Cox Stock Fund") == "institutional"
    assert select_funds.class_type("Class R6") == "R6"
    assert select_funds.class_type("Class R-6") == "R6"
    assert select_funds.class_type("Class R-5") == "retirement"
    assert select_funds.class_type("Class R-5E") == "retirement"
    assert select_funds.class_type("Institutional Class") == "institutional"
    assert select_funds.class_type("Advisor Class") == "other"
    assert select_funds.class_type("ETF Class") == "etf"


def test_filters_ranking_and_loaded_classes(tmp_path):
    candidates, rr, nport = base_files(tmp_path)
    result = select_funds.build_selection(
        candidates, rr, nport, asset_floor=0, families_path=None, type_overrides_path=None
    )
    ids = [series.series_id for series in result["selected"]]
    assert "S_ETF" not in ids
    assert "S_CONLY" not in ids
    assert "S_HIGH" not in ids
    assert "S_NONE" not in ids
    assert ids == ["S_BIG", "S_BOND", "S_LOW", "S_SMALL"]
    big = result["selected"][0]
    assert big.net_assets == 500
    assert big.report_date == "2026-06-30"
    assert big.main.class_id == "C_BIG_A"
    loaded = [share.class_id for share in result["loaded"]["S_BIG"]]
    assert loaded == ["C_BIG_A", "C_BIG_C"]
    assert result["minimum_active"] is True


def test_filer_cap_and_bucket_overflow(tmp_path):
    candidates = tmp_path / "candidates.csv"
    rr = tmp_path / "rr.zip"
    nport = tmp_path / "nport.zip"
    rows = (
        _class_row("S1", "Health Fund", "C1", "Class A", "H1", "999", "Same")
        + _class_row("S2", "Energy Fund", "C2", "Class A", "H2", "999", "Same")
        + _class_row("S3", "Utilities Fund", "C3", "Class A", "H3", "888", "Other")
    )
    write_candidates(candidates, rows)
    write_rr(rr, "")
    write_nport(
        nport,
        "A1\tS1\t90\nA2\tS2\t80\nA3\tS3\t70\n",
        "A1\t2026-06-30\t2026-08-01\nA2\t2026-06-30\t2026-08-01\nA3\t2026-06-30\t2026-08-01\n",
    )
    capped = select_funds.build_selection(
        candidates,
        rr,
        nport,
        quotas=(("sector", 5),),
        filer_cap=1,
        asset_floor=0,
        families_path=None,
        type_overrides_path=None,
    )
    assert [series.series_id for series in capped["selected"]] == ["S1", "S3"]

    overflow = select_funds.build_selection(
        candidates,
        rr,
        nport,
        quotas=(("sector", 1), ("bond", 2)),
        filer_cap=10,
        asset_floor=0,
        families_path=None,
        type_overrides_path=None,
    )
    by_id = {series.series_id: series for series in overflow["selected"]}
    assert set(by_id) == {"S1", "S2", "S3"}
    assert by_id["S1"].how == "quota"
    assert by_id["S2"].how == "leftover"
    assert by_id["S3"].how == "leftover"


def test_override_add_and_remove(tmp_path):
    candidates = tmp_path / "candidates.csv"
    rr = tmp_path / "rr.zip"
    nport = tmp_path / "nport.zip"
    rows = (
        _class_row("S1", "Growth Fund", "C1", "Class A", "G1", "1", "Alpha")
        + _class_row("S2", "Value Fund", "C2", "Class A", "G2", "2", "Beta")
    )
    write_candidates(candidates, rows)
    write_rr(rr, "")
    write_nport(
        nport,
        "A1\tS1\t100\nA2\tS2\t10\n",
        "A1\t2026-06-30\t2026-08-01\nA2\t2026-06-30\t2026-08-01\n",
    )
    overrides = tmp_path / "overrides.csv"
    overrides.write_text(
        "action,series_id,reason\nremove,S1,too concentrated\nadd,S2,advisor prefers the smaller fund\n",
        encoding="utf-8",
    )
    result = select_funds.build_selection(
        candidates,
        rr,
        nport,
        overrides,
        quotas=(("active_equity", 1),),
        filer_cap=10,
        asset_floor=0,
        families_path=None,
        type_overrides_path=None,
    )
    assert len(result["selected"]) == 1
    chosen = result["selected"][0]
    assert chosen.series_id == "S2"
    assert chosen.selected_by == "override"
    assert chosen.reason == "advisor prefers the smaller fund"

    bad = tmp_path / "bad.csv"
    bad.write_text("action,series_id,reason\nadd,S_ETF,not eligible\n", encoding="utf-8")
    try:
        select_funds.build_selection(
            candidates, rr, nport, bad, asset_floor=0, families_path=None, type_overrides_path=None
        )
    except SystemExit as exc:
        assert "fails hard filters" in str(exc)
    else:
        raise AssertionError("expected the ETF add to be refused")

    too_many = tmp_path / "many.csv"
    lines = ["action,series_id,reason"] + [f"add,S{i},n" for i in range(16)]
    too_many.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        select_funds.build_selection(
            candidates, rr, nport, too_many, asset_floor=0, families_path=None, type_overrides_path=None
        )
    except SystemExit as exc:
        assert "exceeds 15" in str(exc)
    else:
        raise AssertionError("expected more than 15 adds to be refused")


def test_main_writes_headers(tmp_path):
    candidates, rr, nport = base_files(tmp_path)
    universe = tmp_path / "fund_universe.csv"
    classes = tmp_path / "share_classes.csv"
    report = tmp_path / "selection.md"
    assert (
        select_funds.main(
            [
                "--candidates",
                str(candidates),
                "--rr-zip",
                str(rr),
                "--nport-zip",
                str(nport),
                "--asset-floor",
                "0",
                "--families",
                str(tmp_path / "missing-families.csv"),
                "--type-overrides",
                str(tmp_path / "missing-types.csv"),
                "--universe",
                str(universe),
                "--classes",
                str(classes),
                "--report",
                str(report),
            ]
        )
        == 0
    )
    with universe.open(newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == select_funds.UNIVERSE_FIELDS
        rows = list(reader)
    assert rows[0]["series_id"] == "S_BIG"
    assert rows[0]["selected_by"] == "rule"
    assert "S_ETF" not in {row["series_id"] for row in rows}
    with classes.open(newline="") as handle:
        class_reader = csv.DictReader(handle)
        assert class_reader.fieldnames == select_funds.CLASS_FIELDS
    text = report.read_text()
    assert "rr.zip" in text and "nport.zip" in text
    assert "Dropped, ETF: 1" in text


def test_fund_type_order():
    assert select_funds.fund_type("JPMorgan Equity Income Fund") == "active_equity"
    assert select_funds.fund_type("Columbia Dividend Income Fund") == "active_equity"
    assert select_funds.fund_type("T. Rowe Price Overseas Stock Fund") == "international"
    assert select_funds.fund_type("Growth & Income Fund") == "target_date_balanced"
    assert select_funds.fund_type("U.S. Government Securities Fund") == "bond"
    assert select_funds.fund_type("PGIM High Yield Fund") == "bond"
    assert select_funds.fund_type("American Funds Mortgage Fund") == "bond"
    assert select_funds.fund_type("Global Gold Fund") == "sector"
    assert select_funds.fund_type("Long/Short Equity Fund") == "alternative"


def test_family_alias_and_cap(tmp_path):
    families = tmp_path / "families.csv"
    families.write_text(
        "pattern,family\n"
        "JPMorgan|J\\.P\\. Morgan|JP Morgan|Undiscovered Managers,JPMorgan\n",
        encoding="utf-8",
    )
    assert select_funds.assign_family(
        "Undiscovered Managers Behavioral Value Fund",
        "Undiscovered Managers Funds",
        select_funds.load_family_rules(families),
    ) == ("JPMorgan", "alias")
    assert select_funds.assign_family(
        "Hood River Small-Cap Growth Fund",
        "Mutual Fund Series Trust",
        [],
    ) == ("Hood River", "sponsor")
    assert select_funds.assign_family("Eventide Gilead Fund", "Northern Lights Fund Trust", []) == (
        "Eventide",
        "sponsor",
    )
    assert select_funds.assign_family("Plain Fund", "Plain Trust", []) == ("Plain Trust", "filer")

    rows = "".join(
        _class_row(f"S{i}", f"Growth Fund {i}", f"C{i}", "Class A", f"T{i}", str(100 + i), filer)
        for i, filer in enumerate(
            [
                "JPMorgan Trust I",
                "JPMorgan Trust II",
                "J.P. Morgan Mutual Fund Investment Trust",
                "JP Morgan Fleming Funds",
                "Undiscovered Managers Funds",
                "JPMorgan Trust I",
                "JPMorgan Trust II",
                "JPMorgan Trust I",
                "JPMorgan Trust II",
                "Other Trust",
            ]
        )
    )
    candidates = tmp_path / "candidates.csv"
    rr = tmp_path / "rr.zip"
    nport = tmp_path / "nport.zip"
    write_candidates(candidates, rows)
    write_rr(rr, "")
    info = "".join(f"A{i}\tS{i}\t{2_000_000_000 - i}\n" for i in range(10))
    submission = "".join(f"A{i}\t2026-06-30\t2026-08-01\n" for i in range(10))
    write_nport(nport, info, submission)
    result = select_funds.build_selection(
        candidates,
        rr,
        nport,
        quotas=(("active_equity", 25),),
        families_path=families,
        type_overrides_path=None,
    )
    selected = result["selected"]
    jpm = [series for series in selected if series.family == "JPMorgan"]
    assert len(jpm) == 8
    assert all(series.family_source == "alias" for series in jpm)
    assert any(series.family == "Other Trust" for series in selected)
    assert max(
        sum(1 for series in selected if series.family == family)
        for family in {series.family for series in selected}
    ) <= 8


def test_asset_floor_drops_small_and_unmatched(tmp_path):
    rows = (
        _class_row("S_BIG", "Growth Fund", "C1", "Class A", "BIG", "1", "Alpha")
        + _class_row("S_TINY", "Value Fund", "C2", "Class A", "TINY", "2", "Beta")
        + _class_row("S_NONE", "Income Fund", "C3", "Class A", "NONE", "3", "Gamma")
    )
    candidates = tmp_path / "candidates.csv"
    rr = tmp_path / "rr.zip"
    nport = tmp_path / "nport.zip"
    write_candidates(candidates, rows)
    write_rr(rr, "")
    write_nport(
        nport,
        "A1\tS_BIG\t2000000000\nA2\tS_TINY\t20000000\n",
        "A1\t2026-06-30\t2026-08-01\nA2\t2026-06-30\t2026-08-01\n",
    )
    result = select_funds.build_selection(
        candidates, rr, nport, families_path=None, type_overrides_path=None
    )
    assert [series.series_id for series in result["selected"]] == ["S_BIG"]
    dropped = {series.series_id for series in result["dropped"]["small"]}
    assert dropped == {"S_TINY", "S_NONE"}


def test_loaded_class_filter(tmp_path):
    names = [
        ("C_A", "Class A"),
        ("C_ADM", "Admiral Shares"),
        ("C_F1", "Class F-1"),
        ("C_F2", "Class F-2"),
        ("C_F3", "Class F-3"),
        ("C_C", "Class C"),
        ("C_R5", "Class R-5"),
        ("C_R6", "Class R-6"),
        ("C_529A", "Class 529-A"),
        ("C_529C", "Class 529-C"),
        ("C_I", "Institutional Class"),
    ]
    rows = "".join(
        _class_row("S1", "Growth Fund", class_id, class_name, class_id, "1", "Alpha")
        for class_id, class_name in names
    )
    candidates = tmp_path / "candidates.csv"
    rr = tmp_path / "rr.zip"
    nport = tmp_path / "nport.zip"
    write_candidates(candidates, rows)
    write_rr(rr, "")
    write_nport(nport, "A1\tS1\t2000000000\n", "A1\t2026-06-30\t2026-08-01\n")
    result = select_funds.build_selection(
        candidates,
        rr,
        nport,
        quotas=(("active_equity", 1),),
        families_path=None,
        type_overrides_path=None,
    )
    series = result["selected"][0]
    assert series.main.class_type == "A"
    assert {share.class_id: share.class_type for share in series.classes}["C_ADM"] == "investor"
    loaded = [share.class_id for share in result["loaded"]["S1"]]
    assert loaded == ["C_529A", "C_A", "C_C", "C_F2", "C_F3", "C_R6"]
