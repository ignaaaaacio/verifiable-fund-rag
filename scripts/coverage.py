"""M1 coverage: which share classes have the facts FundRAG needs.

Usage:
    python3 scripts/coverage.py --zip <path> [--out-dir reports]

Reads num.tsv, txt.tsv, scn.tsv, and sub.tsv from one SEC Risk/Return zip.
Filer-custom taxonomy rows (version not starting with oef, rr, dei, fnd,
us-gaap, or srt) are ignored. When a class is in more than one filing, only
the latest (sub.tsv filed, then accepted) is kept.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

STANDARD_PREFIXES = ("oef", "rr", "dei", "fnd", "us-gaap", "srt")

EXPENSE_RATIO_TAGS = {"ExpensesOverAssets", "NetExpensesOverAssets"}
EXAMPLE_TAGS = {
    "ExpenseExampleYear01",
    "ExpenseExampleYear03",
    "ExpenseExampleYear05",
    "ExpenseExampleYear10",
}
RISK_TAG = "RiskTextBlock"

CANDIDATE_FIELDS = [
    "seriesid",
    "seriesname",
    "classid",
    "classname",
    "ticker",
    "filer_name",
    "cik",
    "adsh",
    "filed",
    "has_expense_ratio",
    "has_example",
    "has_returns",
    "has_ticker",
    "has_risk_text",
    "complete",
]


def is_standard_version(version: str) -> bool:
    """True for SEC taxonomy versions, false for filer-custom versions."""
    text = (version or "").strip().lower()
    return text.startswith(STANDARD_PREFIXES)


def is_average_annual_tag(tag: str) -> bool:
    """The 1/5/10-year average annual return fact, under either taxonomy name."""
    folded = (tag or "").lower()
    return "avgannlrtr" in folded or "averageannualreturn" in folded


def _performance_measure(otherdims: str) -> str | None:
    for part in (otherdims or "").split(";"):
        piece = part.strip()
        if piece.lower().startswith("performancemeasure="):
            return piece.split("=", 1)[1].strip()
    return None


def is_own_before_tax(otherdims: str) -> bool:
    """Fund return with no benchmark dimension and no after-tax marker.

    In the OEF filings, benchmark rows and after-tax rows share AvgAnnlRtrPct
    and are distinguished by PerformanceMeasure. A blank measure, or the
    explicit value ReturnBeforeTaxes, is the before-tax fund return.
    """
    measure = _performance_measure(otherdims)
    if measure is None:
        return True
    compact = "".join(ch for ch in measure.lower() if ch.isalnum())
    if "aftertax" in compact:
        return False
    return compact == "returnbeforetaxes"


def return_period(tag: str, qtrs: str, otherdims: str) -> str | None:
    """'1y', '5y', or '10y' for a before-tax fund average-annual row, else None."""
    if not is_average_annual_tag(tag) or not is_own_before_tax(otherdims):
        return None
    folded = tag.lower()
    if "year10" in folded or "10year" in folded:
        return "10y"
    if "year05" in folded or "year5" in folded or "5year" in folded:
        return "5y"
    if "year01" in folded or "year1" in folded or "1year" in folded:
        return "1y"
    quarter = (qtrs or "").strip().split(".", 1)[0]
    return {"4": "1y", "20": "5y", "40": "10y"}.get(quarter)


def _open_table(zf: zipfile.ZipFile, name: str) -> csv.DictReader:
    raw = zf.open(name)
    text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
    return csv.DictReader(text, delimiter="\t")


def _member(zf: zipfile.ZipFile, stem: str) -> str:
    stem = stem.lower()
    matches = [
        name
        for name in zf.namelist()
        if Path(name).name.lower() == f"{stem}.tsv"
    ]
    if not matches:
        raise SystemExit(f"{stem}.tsv was not in the zip")
    return matches[0]


def _filing_key(sub: dict, adsh: str) -> tuple:
    row = sub.get(adsh) or {}
    return (row.get("filed") or "", row.get("accepted") or "", adsh)


def analyze(zip_path: Path) -> dict:
    """Build the return-tag inventory and one record per latest share class."""
    with zipfile.ZipFile(zip_path) as zf:
        filings: dict[str, dict] = {}
        for row in _open_table(zf, _member(zf, "sub")):
            filings[row.get("adsh") or ""] = row

        classes_scn: dict[str, dict] = {}
        for row in _open_table(zf, _member(zf, "scn")):
            class_id = (row.get("classid") or "").strip()
            if class_id:
                classes_scn[class_id] = row

        return_tags: dict[str, Counter] = defaultdict(Counter)
        return_classes: dict[str, set] = defaultdict(set)
        return_rows: Counter = Counter()
        periods: dict[tuple[str, str], set] = defaultdict(set)
        ratios: dict[tuple[str, str], bool] = defaultdict(bool)
        examples: dict[tuple[str, str], set] = defaultdict(set)
        class_adshs: dict[str, set] = defaultdict(set)
        class_series: dict[tuple[str, str], str] = {}

        for row in _open_table(zf, _member(zf, "num")):
            if not is_standard_version(row.get("version") or ""):
                continue
            tag = row.get("tag") or ""
            class_id = (row.get("class") or "").strip()
            adsh = row.get("adsh") or ""
            if "return" in tag.lower() or is_average_annual_tag(tag):
                return_rows[tag] += 1
                if class_id:
                    return_classes[tag].add(class_id)
                dims = (row.get("otherdims") or "").strip()
                if dims:
                    return_tags[tag][dims] += 1
            if not class_id:
                continue
            class_adshs[class_id].add(adsh)
            class_series[(adsh, class_id)] = (row.get("series") or "").strip()
            if tag in EXPENSE_RATIO_TAGS:
                ratios[(adsh, class_id)] = True
            if tag in EXAMPLE_TAGS:
                examples[(adsh, class_id)].add(tag)
            period = return_period(tag, row.get("qtrs") or "", row.get("otherdims") or "")
            if period:
                periods[(adsh, class_id)].add(period)

        risk_series: set[str] = set()
        for row in _open_table(zf, _member(zf, "txt")):
            if not is_standard_version(row.get("version") or ""):
                continue
            if (row.get("tag") or "") != RISK_TAG:
                continue
            series_id = (row.get("series") or "").strip()
            if series_id:
                risk_series.add(series_id)

    records = []
    for class_id, adshs in class_adshs.items():
        adsh = max(adshs, key=lambda item: _filing_key(filings, item))
        filing = filings.get(adsh) or {}
        scn = classes_scn.get(class_id) or {}
        series_id = (scn.get("seriesid") or class_series.get((adsh, class_id)) or "").strip()
        ticker = (scn.get("ticker") or "").strip()
        flags = {
            "has_expense_ratio": ratios[(adsh, class_id)],
            "has_example": EXAMPLE_TAGS <= examples[(adsh, class_id)],
            "has_returns": {"1y", "5y", "10y"} <= periods[(adsh, class_id)],
            "has_ticker": bool(ticker),
            "has_risk_text": series_id in risk_series,
        }
        records.append(
            {
                "seriesid": series_id,
                "seriesname": (scn.get("seriesname") or "").strip(),
                "classid": class_id,
                "classname": (scn.get("classname") or "").strip(),
                "ticker": ticker,
                "filer_name": (filing.get("name") or "").strip(),
                "cik": (filing.get("cik") or "").strip(),
                "adsh": adsh,
                "filed": (filing.get("filed") or "").strip(),
                "flags": flags,
                "complete": all(flags.values()),
            }
        )
    records.sort(key=lambda rec: (rec["seriesid"], rec["classid"]))

    tag_table = []
    for tag, count in return_rows.most_common():
        tag_table.append(
            {
                "tag": tag,
                "rows": count,
                "classes": len(return_classes[tag]),
                "otherdims": [dims for dims, _ in return_tags[tag].most_common(10)],
            }
        )
    return {"records": records, "return_tags": tag_table}


def _flag(value: bool) -> str:
    return "1" if value else "0"


def write_candidates(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CANDIDATE_FIELDS, lineterminator="\n")
        writer.writeheader()
        for rec in records:
            row = {key: rec[key] for key in CANDIDATE_FIELDS if key in rec}
            for name, value in rec["flags"].items():
                row[name] = _flag(value)
            row["complete"] = _flag(rec["complete"])
            writer.writerow(row)


def render_report(result: dict, source: str) -> str:
    records = result["records"]
    complete_classes = [rec for rec in records if rec["complete"]]
    complete_series = {rec["seriesid"] for rec in complete_classes if rec["seriesid"]}
    lines = [
        "# Fund coverage",
        "",
        f"Source: `{source}`",
        "",
        "## Return tags",
        "",
        "1-, 5-, and 10-year average annual returns are `AvgAnnlRtrPct` "
        "(or `AverageAnnualReturn*`). Periods are `qtrs` 4, 20, and 40 when the "
        "tag name does not already say the year. Benchmark rows and after-tax rows "
        "use `PerformanceMeasure` (`ReturnBeforeTaxes` is the fund's own before-tax "
        "figure; a blank measure is the same thing). Tags whose name contains "
        "`Return` are listed as well.",
        "",
        "| Tag | Rows | Classes | Example otherdims |",
        "| --- | --- | --- | --- |",
    ]
    for tag in result["return_tags"]:
        examples = "; ".join(f"`{dims}`" for dims in tag["otherdims"]) or "(none)"
        lines.append(f"| `{tag['tag']}` | {tag['rows']} | {tag['classes']} | {examples} |")

    lines += [
        "",
        "## Flags",
        "",
        f"- Share classes: {len(records)}",
        f"- Classes with every flag: {len(complete_classes)}",
        f"- Series with at least one complete class: {len(complete_series)}",
        "",
        "| Flag | Classes |",
        "| --- | --- |",
    ]
    if records:
        for name in (
            "has_expense_ratio",
            "has_example",
            "has_returns",
            "has_ticker",
            "has_risk_text",
        ):
            count = sum(1 for rec in records if rec["flags"][name])
            lines.append(f"| {name} | {count} |")
    lines.append("")
    lines.append("## Top filers by complete series")
    lines.append("")
    by_filer: dict[tuple[str, str], set] = defaultdict(set)
    for rec in complete_classes:
        if rec["seriesid"]:
            by_filer[(rec["filer_name"], rec["cik"])].add(rec["seriesid"])
    ranked = sorted(by_filer.items(), key=lambda item: (-len(item[1]), item[0][0]))
    if not ranked:
        lines.append("No series has a complete share class.")
    else:
        lines.append("| Filer | CIK | Complete series |")
        lines.append("| --- | --- | --- |")
        for (name, cik), series_ids in ranked[:20]:
            lines.append(f"| {name} | {cik} | {len(series_ids)} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", required=True, type=Path, help="Risk/Return data-set zip")
    parser.add_argument("--out-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    result = analyze(args.zip)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    report = args.out_dir / "coverage.md"
    candidates = args.out_dir / "candidates.csv"
    report.write_text(render_report(result, str(args.zip)), encoding="utf-8")
    write_candidates(result["records"], candidates)
    complete = sum(1 for rec in result["records"] if rec["complete"])
    series = len({rec["seriesid"] for rec in result["records"] if rec["complete"]})
    print(f"Wrote {candidates} and {report}")
    print(f"Complete classes: {complete}. Series with a complete class: {series}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
