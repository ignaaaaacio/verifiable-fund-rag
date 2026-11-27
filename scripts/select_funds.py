"""Pick the 100-fund universe from coverage candidates, the Risk/Return zip, and N-PORT.

Usage:
    python3 scripts/select_funds.py \\
        --candidates reports/candidates.csv \\
        --rr-zip <rr zip> \\
        --nport-zip <nport zip> \\
        [--overrides eval/overrides.csv]

A series is kept only when it is not an ETF, has risk text, and has a complete
Class A or a complete no-load Investor class. Series are ranked by N-PORT net
assets and filled into type quotas, with at most 10 series per filer. Expert
overrides are applied after that rule.

Standard library only. Holdings tables inside the N-PORT zip are not read.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

STANDARD_PREFIXES = ("oef", "rr", "dei", "fnd", "us-gaap", "srt")
FILER_CAP = 10
MAX_OVERRIDE_ADDS = 15
MINIMUM_INVESTMENT_CAP = Decimal("100000")

QUOTAS: tuple[tuple[str, int], ...] = (
    ("target_date_balanced", 15),
    ("bond", 20),
    ("international", 15),
    ("index_equity", 20),
    ("active_equity", 25),
    ("sector", 5),
)

LOAD_TAGS = {
    "MaximumSalesChargeImposedOnPurchasesOverOfferingPrice",
    "MaximumDeferredSalesChargeOverOfferingPrice",
    "MaximumDeferredSalesChargeOverOther",
    "MaximumCumulativeSalesChargeOverOfferingPrice",
}
FEE_WAIVER_TAG = "FeeWaiverOrReimbursementOverAssets"
MINIMUM_TAG = re.compile(
    r"minimum.*initial.*invest|initial.*investment.*minimum|minimuminitialinvestment",
    re.I,
)
ETF_NAME = re.compile(r"\betf\b|exchange[\s-]?traded", re.I)

LOADED_TYPES = {"A", "investor", "F2", "F3", "C", "retirement", "529"}

FUND_TYPE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "target_date_balanced",
        re.compile(
            r"\btarget\b|retirement\s+20|lifecycle|\bbalanced\b|\ballocation\b|income\s+builder",
            re.I,
        ),
    ),
    (
        "bond",
        re.compile(
            r"\bbond\b|\bincome\b|\bfixed\b|\btreasury\b|\bmunicipal\b|tax[-\s]?exempt|\bcredit\b|\bduration\b",
            re.I,
        ),
    ),
    (
        "international",
        re.compile(
            r"\binternational\b|\bglobal\b|\bworld\b|\bemerging\b|\beurope\b|\bpacific\b|\bdeveloping\b",
            re.I,
        ),
    ),
    (
        "index_equity",
        re.compile(r"\bindex\b|\b500\b|total\s+stock|total\s+market", re.I),
    ),
    (
        "sector",
        re.compile(
            r"\bhealth\b|\btechnolog|\benergy\b|real\s+estate|\butilities\b|\bfinancial\b|\bscience\b",
            re.I,
        ),
    ),
    (
        "active_equity",
        re.compile(r"\bequity\b|\bgrowth\b|\bvalue\b|\bfund\b|\bstock\b|\bcap\b", re.I),
    ),
)

UNIVERSE_FIELDS = [
    "rank",
    "series_id",
    "series_name",
    "filer_name",
    "cik",
    "fund_type",
    "main_class_id",
    "main_class_type",
    "ticker",
    "net_assets",
    "report_date",
    "selected_by",
    "reason",
]
CLASS_FIELDS = ["series_id", "class_id", "class_name", "class_type", "ticker"]

DATE_FORMATS = ("%Y-%m-%d", "%Y%m%d", "%d-%b-%Y", "%d-%b-%y", "%m/%d/%Y")


@dataclass
class ShareClass:
    series_id: str
    class_id: str
    class_name: str
    ticker: str
    adsh: str
    complete: bool
    class_type: str
    no_load: bool = True


@dataclass
class Series:
    series_id: str
    series_name: str
    filer_name: str
    cik: str
    has_risk_text: bool
    classes: list[ShareClass] = field(default_factory=list)
    fund_type: str = "unmatched"
    main: ShareClass | None = None
    net_assets: Decimal | None = None
    report_date: str = ""
    fee_waiver: bool = False
    selected_by: str = "rule"
    reason: str = ""
    how: str = ""

    @property
    def filer_key(self) -> str:
        digits = "".join(ch for ch in self.cik if ch.isdigit())
        return digits.lstrip("0") or "0"


def is_standard_version(version: str) -> bool:
    return (version or "").strip().lower().startswith(STANDARD_PREFIXES)


def parse_number(value: str | None) -> Decimal | None:
    text = (value or "").strip().replace(",", "")
    if not text or text.lower() in {"null", "na", "n/a", "none"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_date(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    token = text.split()[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).date().isoformat()
        except ValueError:
            continue
    return token


def format_assets(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def normalize_class_name(name: str) -> str:
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def class_type(name: str) -> str:
    """Map a share-class name onto the universe's class types.

    529 is tested first so Class 529-A and Class 529-F-2 stay 529. Investor A
    and Investor C are load classes, not the no-load Investor class.
    """
    text = normalize_class_name(name)
    if not text:
        return "other"
    if re.search(r"\b529\b", text):
        return "529"
    if ETF_NAME.search(text):
        return "etf"
    if re.search(r"\bf\s*2\b", text):
        return "F2"
    if re.search(r"\bf\s*3\b", text):
        return "F3"
    if re.search(r"\binstitutional\b|\binstl\b", text):
        return "institutional"
    if re.search(r"\binvestor\s+a\d?\b", text) or re.search(r"(?:^|\bclass\s+)a\d?\b", text) or text in {"a", "a class", "a shares"}:
        return "A"
    if re.search(r"\binvestor\s+c\b", text) or re.search(r"(?:^|\bclass\s+)c\b", text) or text in {"c", "c class", "c shares"}:
        return "C"
    if (
        re.search(r"\bretirement\b|\b401\s?k\b", text)
        or re.search(r"\br\s*[1-6]\b", text)
        or re.search(r"\bclass\s+r\b", text)
        or re.search(r"\bclass\s+k\b", text)
        or text in {"r", "r class", "r shares"}
    ):
        return "retirement"
    if (
        re.search(r"\bclass\s+i\b", text)
        or re.search(r"\bi\s*[0-9]\b", text)
        or text in {"i", "i class", "i shares", "class i shares"}
    ):
        return "institutional"
    if re.search(r"\bclass\s+[yz]\b", text) or text in {"y", "z", "y class", "z class", "y shares", "z shares"}:
        return "institutional"
    if re.search(r"\binvestor\b|\bretail\b|\bno\s*load\b", text):
        return "investor"
    return "other"


def fund_type(series_name: str) -> str:
    for name, pattern in FUND_TYPE_RULES:
        if pattern.search(series_name or ""):
            return name
    return "unmatched"


def is_etf_series(series_name: str) -> bool:
    return ETF_NAME.search(series_name or "") is not None


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "t", "yes", "y"}


def _column_map(fieldnames: list[str] | None) -> dict[str, str]:
    return {re.sub(r"[^a-z0-9]", "", (name or "").lower()): name for name in fieldnames or []}


def _col(columns: dict[str, str], *candidates: str) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return columns[candidate]
    for candidate in candidates:
        for key, original in columns.items():
            if candidate in key:
                return original
    return None


def read_candidates(path: Path) -> list[Series]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = _column_map(reader.fieldnames)
        required = ("seriesid", "classid", "complete")
        missing = [name for name in required if _col(columns, name) is None]
        if missing:
            raise SystemExit(f"{path} is missing columns: {', '.join(missing)}")

        def cell(row: dict, name: str) -> str:
            column = _col(columns, name)
            return (row.get(column) or "").strip() if column else ""

        grouped: dict[str, Series] = {}
        for row in reader:
            series_id = cell(row, "seriesid").upper()
            if not series_id:
                continue
            series = grouped.get(series_id)
            if series is None:
                series = Series(
                    series_id=series_id,
                    series_name=cell(row, "seriesname"),
                    filer_name=cell(row, "filername") or cell(row, "filer_name"),
                    cik=cell(row, "cik"),
                    has_risk_text=_truthy(cell(row, "hasrisktext")),
                )
                grouped[series_id] = series
            elif _truthy(cell(row, "hasrisktext")):
                series.has_risk_text = True
            share = ShareClass(
                series_id=series_id,
                class_id=cell(row, "classid").upper(),
                class_name=cell(row, "classname"),
                ticker=cell(row, "ticker"),
                adsh=cell(row, "adsh"),
                complete=_truthy(cell(row, "complete")),
                class_type=class_type(cell(row, "classname")),
            )
            series.classes.append(share)
    series_list = list(grouped.values())
    for series in series_list:
        series.fund_type = fund_type(series.series_name)
    return series_list


def _member(zf: zipfile.ZipFile, predicate) -> str | None:
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        if predicate(name):
            return name
    return None


def _header(zf: zipfile.ZipFile, name: str) -> list[str]:
    with zf.open(name) as raw:
        line = raw.readline()
    text = line.decode("utf-8-sig", errors="replace").rstrip("\r\n")
    return next(csv.reader([text], delimiter="\t"))


def _rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        yield from csv.DictReader(text, delimiter="\t")


def _is_holding(name: str) -> bool:
    folded = Path(name).name.lower()
    return "holding" in folded


def _is_fund_info(name: str) -> bool:
    folded = Path(name).name.lower()
    return "fund_reported_info" in folded and "holding" not in folded


def read_net_assets(zip_path: Path) -> dict[str, tuple[Decimal | None, str]]:
    """Latest net assets per series id. Holdings tables are never opened."""
    with zipfile.ZipFile(zip_path) as zf:
        info_name = _member(zf, _is_fund_info)
        if info_name is None:
            raise SystemExit(f"No FUND_REPORTED_INFO table in {zip_path.name}")
        info_header = _header(zf, info_name)
        info_cols = _column_map(info_header)
        series_col = _col(info_cols, "seriesid")
        assets_col = _col(info_cols, "netassets")
        info_accession = _col(info_cols, "accessionnumber")
        if not series_col or not assets_col or not info_accession:
            raise SystemExit(f"{info_name} is missing series id, net assets, or accession columns")

        date_name = None
        date_cols: dict[str, str] = {}
        for name in zf.namelist():
            if name.endswith("/") or _is_holding(name) or not name.lower().endswith(".tsv"):
                continue
            if Path(name).name == Path(info_name).name:
                continue
            header = _header(zf, name)
            columns = _column_map(header)
            if _col(columns, "reportdate") and _col(columns, "accessionnumber"):
                date_name = name
                date_cols = columns
                if "submission" in Path(name).name.lower():
                    break
        if date_name is None:
            raise SystemExit(f"No report-date table found in {zip_path.name}")

        report_col = _col(date_cols, "reportdate")
        filing_col = _col(date_cols, "filingdate")
        date_accession = _col(date_cols, "accessionnumber")
        dates: dict[str, tuple[str, str]] = {}
        for row in _rows(zf, date_name):
            accession = (row.get(date_accession) or "").strip()
            if not accession:
                continue
            dates[accession] = (
                parse_date(row.get(report_col or "")),
                parse_date(row.get(filing_col or "")) if filing_col else "",
            )

        # Latest report wins. Key is (report date, filing date, accession).
        best: dict[str, tuple[tuple[str, str, str], Decimal | None, str]] = {}
        for row in _rows(zf, info_name):
            series_id = (row.get(series_col) or "").strip().upper()
            if not series_id:
                continue
            accession = (row.get(info_accession) or "").strip()
            report_date, filing_date = dates.get(accession, ("", ""))
            assets = parse_number(row.get(assets_col))
            key = (report_date, filing_date, accession)
            current = best.get(series_id)
            if current is None or key > current[0]:
                best[series_id] = (key, assets, report_date)
    return {series_id: (assets, report_date) for series_id, (_, assets, report_date) in best.items()}


def _is_minimum_tag(tag: str) -> bool:
    if "breakpoint" in tag.lower():
        return False
    return MINIMUM_TAG.search(tag or "") is not None


def read_rr_facts(zip_path: Path, wanted: set[tuple[str, str]]) -> dict:
    """Sales loads, fee waivers, and minimum initial investments for selected classes."""
    loaded: set[str] = set()
    waived: set[str] = set()
    minimums: dict[str, Decimal] = {}
    minimum_tags: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        name = _member(zf, lambda item: Path(item).name.lower() == "num.tsv")
        if name is None:
            raise SystemExit(f"num.tsv was not in {zip_path.name}")
        for row in _rows(zf, name):
            if not is_standard_version(row.get("version") or ""):
                continue
            tag = row.get("tag") or ""
            class_id = (row.get("class") or "").strip().upper()
            adsh = (row.get("adsh") or "").strip()
            relevant = (adsh, class_id) in wanted
            if _is_minimum_tag(tag):
                minimum_tags.add(tag)
                if relevant:
                    amount = parse_number(row.get("value"))
                    if amount is not None:
                        current = minimums.get(class_id)
                        if current is None or amount < current:
                            minimums[class_id] = amount
                continue
            if not relevant:
                continue
            amount = parse_number(row.get("value"))
            if amount is None:
                continue
            if tag in LOAD_TAGS and amount > 0:
                loaded.add(class_id)
            elif tag == FEE_WAIVER_TAG and amount != 0:
                waived.add(class_id)
    return {
        "loaded": loaded,
        "waived": waived,
        "minimums": minimums,
        "minimum_tags": sorted(minimum_tags),
    }


def _choose_main(classes: list[ShareClass]) -> ShareClass | None:
    def ordered(pool: list[ShareClass]) -> ShareClass:
        return sorted(pool, key=lambda share: (share.ticker == "", share.class_id))[0]

    class_a = [share for share in classes if share.complete and share.class_type == "A"]
    if class_a:
        return ordered(class_a)
    investor = [
        share
        for share in classes
        if share.complete and share.class_type == "investor" and share.no_load
    ]
    if investor:
        return ordered(investor)
    return None


def apply_hard_filters(series_list: list[Series], facts: dict) -> dict[str, list[Series]]:
    """Split series into the eligible list and the dropped lists, in pipeline order."""
    for series in series_list:
        for share in series.classes:
            share.no_load = share.class_id not in facts["loaded"]
    minimum_active = bool(facts["minimum_tags"])
    dropped = {"etf": [], "no_risk": [], "no_main": [], "minimum": []}
    eligible = []
    for series in series_list:
        if is_etf_series(series.series_name):
            dropped["etf"].append(series)
            continue
        if not series.has_risk_text:
            dropped["no_risk"].append(series)
            continue
        series.main = _choose_main(series.classes)
        if series.main is None:
            dropped["no_main"].append(series)
            continue
        if minimum_active:
            amount = facts["minimums"].get(series.main.class_id)
            if amount is not None and amount > MINIMUM_INVESTMENT_CAP:
                dropped["minimum"].append(series)
                continue
        series.fee_waiver = series.main.class_id in facts["waived"]
        eligible.append(series)
    return {"eligible": eligible, "dropped": dropped, "minimum_active": minimum_active}


def rank_series(series_list: list[Series]) -> list[Series]:
    """Net assets descending. Series with no N-PORT match sort last; series id breaks ties."""

    def key(series: Series):
        missing = series.net_assets is None
        assets = series.net_assets if series.net_assets is not None else Decimal(0)
        return (missing, -assets, series.series_id)

    return sorted(series_list, key=key)


def fill_quotas(
    ranked: list[Series],
    quotas: tuple[tuple[str, int], ...] = QUOTAS,
    filer_cap: int = FILER_CAP,
) -> list[Series]:
    """Walk the ranking. Unfilled quota slots then go to the next-largest series of any type."""
    remaining = {name: count for name, count in quotas}
    per_filer: Counter = Counter()
    selected: list[Series] = []
    waiting: list[Series] = []
    for series in ranked:
        if per_filer[series.filer_key] >= filer_cap:
            continue
        left = remaining.get(series.fund_type)
        if left is None or left <= 0:
            waiting.append(series)
            continue
        series.how = "quota"
        series.reason = "quota"
        series.selected_by = "rule"
        selected.append(series)
        remaining[series.fund_type] -= 1
        per_filer[series.filer_key] += 1
    slots = sum(remaining.values())
    for series in waiting:
        if slots <= 0:
            break
        if per_filer[series.filer_key] >= filer_cap:
            continue
        series.how = "leftover"
        series.reason = "leftover"
        series.selected_by = "rule"
        selected.append(series)
        per_filer[series.filer_key] += 1
        slots -= 1
    return selected


def _override_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = _column_map(reader.fieldnames)
        action_col = _col(columns, "action")
        series_col = _col(columns, "seriesid", "series_id")
        reason_col = _col(columns, "reason")
        if not action_col or not series_col:
            raise SystemExit(f"{path} must have action and series_id columns")
        rows = []
        for row in reader:
            rows.append(
                {
                    "action": (row.get(action_col) or "").strip().lower(),
                    "series_id": (row.get(series_col) or "").strip().upper(),
                    "reason": (row.get(reason_col) or "").strip() if reason_col else "",
                }
            )
        return rows


def apply_overrides(
    selected: list[Series],
    eligible: list[Series],
    overrides: list[dict[str, str]],
) -> list[Series]:
    adds = [row for row in overrides if row["action"] == "add"]
    removes = [row for row in overrides if row["action"] == "remove"]
    if len(adds) > MAX_OVERRIDE_ADDS:
        raise SystemExit(f"refusing overrides: {len(adds)} adds exceeds {MAX_OVERRIDE_ADDS}")
    by_id = {series.series_id: series for series in eligible}
    chosen = {series.series_id: series for series in selected}
    for row in removes:
        chosen.pop(row["series_id"], None)
    for row in adds:
        series = by_id.get(row["series_id"])
        if series is None:
            raise SystemExit(
                f"refusing add {row['series_id']}: fails hard filters"
            )
        series.selected_by = "override"
        series.reason = row["reason"]
        series.how = "override"
        chosen[series.series_id] = series
    return list(chosen.values())


def classes_to_load(series: Series) -> list[ShareClass]:
    loaded = []
    for share in series.classes:
        if not share.complete or share.class_type not in LOADED_TYPES:
            continue
        if share.class_type == "investor" and not share.no_load:
            continue
        loaded.append(share)
    return sorted(loaded, key=lambda share: share.class_id)


def attach_assets(series_list: list[Series], assets: dict[str, tuple[Decimal | None, str]]) -> None:
    for series in series_list:
        net, report_date = assets.get(series.series_id, (None, ""))
        series.net_assets = net
        series.report_date = report_date


def build_selection(
    candidates: Path,
    rr_zip: Path,
    nport_zip: Path,
    overrides: Path | None = None,
    quotas: tuple[tuple[str, int], ...] = QUOTAS,
    filer_cap: int = FILER_CAP,
) -> dict:
    series_list = read_candidates(candidates)
    wanted = {
        (share.adsh, share.class_id)
        for series in series_list
        for share in series.classes
        if share.adsh and share.class_id
    }
    facts = read_rr_facts(rr_zip, wanted)
    filtered = apply_hard_filters(series_list, facts)
    eligible = filtered["eligible"]
    attach_assets(eligible, read_net_assets(nport_zip))
    ranked = rank_series(eligible)
    selected = fill_quotas(ranked, quotas=quotas, filer_cap=filer_cap)
    override_rows: list[dict[str, str]] = []
    if overrides is not None:
        override_rows = _override_rows(overrides)
        selected = apply_overrides(selected, eligible, override_rows)
    final = rank_series(selected)
    loaded = {series.series_id: classes_to_load(series) for series in final}
    return {
        "series_in": series_list,
        "eligible": eligible,
        "dropped": filtered["dropped"],
        "minimum_active": filtered["minimum_active"],
        "minimum_tags": facts["minimum_tags"],
        "ranked": ranked,
        "selected": final,
        "loaded": loaded,
        "quotas": quotas,
        "overrides": override_rows,
        "inputs": {
            "candidates": candidates,
            "rr_zip": rr_zip,
            "nport_zip": nport_zip,
            "overrides": overrides,
        },
    }


def _quota_table(result: dict) -> list[tuple[str, int, int, int]]:
    rows = []
    for name, quota in result["quotas"]:
        taken = [series for series in result["selected"] if series.fund_type == name]
        by_quota = sum(1 for series in taken if series.how == "quota")
        by_left = sum(1 for series in taken if series.how == "leftover")
        rows.append((name, quota, by_quota, by_left))
    return rows


def render_report(result: dict) -> str:
    dropped = result["dropped"]
    selected = result["selected"]
    inputs = result["inputs"]
    lines = [
        "# Fund universe selection",
        "",
        "## Inputs",
        "",
        f"- Candidates: `{inputs['candidates'].name}`",
        f"- Risk/Return zip: `{inputs['rr_zip'].name}`",
        f"- N-PORT zip: `{inputs['nport_zip'].name}`",
        f"- Overrides: `{inputs['overrides'].name}`" if inputs["overrides"] else "- Overrides: none",
        "",
        "## Filter steps",
        "",
        f"- Series in candidates: {len(result['series_in'])}",
        f"- Dropped, ETF: {len(dropped['etf'])}",
        f"- Dropped, no risk text: {len(dropped['no_risk'])}",
        f"- Dropped, no complete Class A or no-load Investor class: {len(dropped['no_main'])}",
    ]
    if result["minimum_active"]:
        tags = ", ".join(f"`{tag}`" for tag in result["minimum_tags"])
        lines.append(
            f"- Dropped, minimum initial investment over $100,000 ({tags}): {len(dropped['minimum'])}"
        )
    else:
        lines.append(
            "- Minimum initial investment: not applied. "
            "No minimum-initial-investment tag was in the Risk/Return zip "
            "(expense-breakpoint minimums are not that tag)."
        )
    lines.append(f"- Eligible after hard filters: {len(result['eligible'])}")
    lines.append(f"- Selected: {len(selected)}")
    if len(selected) < 100:
        lines.append(
            f"- Fewer than 100 because {len(result['eligible'])} series passed the hard filters "
            "and the quota, filer cap, and overrides left this many."
        )
    lines += ["", "## Quotas", "", "| Type | Quota | Filled by quota | Filled by leftover | Selected |", "| --- | --- | --- | --- | --- |"]
    for name, quota, by_quota, by_left in _quota_table(result):
        lines.append(f"| {name} | {quota} | {by_quota} | {by_left} | {by_quota + by_left} |")
    if any(series.fund_type == "unmatched" for series in selected):
        count = sum(1 for series in selected if series.fund_type == "unmatched")
        lines.append(f"| unmatched | 0 | 0 | {count} | {count} |")

    loaded_counts: Counter = Counter()
    multi = 0
    waiver = 0
    filers: dict[str, list[Series]] = defaultdict(list)
    for series in selected:
        shares = result["loaded"][series.series_id]
        if len(shares) >= 2:
            multi += 1
        if series.fee_waiver:
            waiver += 1
        for share in shares:
            loaded_counts[share.class_type] += 1
        filers[series.filer_key].append(series)

    def mark(ok: bool) -> str:
        return "met" if ok else "not met"

    lines += [
        "",
        "## Mix checks",
        "",
        "These checks are reported and do not change the selection.",
        "",
        f"- Distinct filers: {len(filers)} (target at least 10, {mark(len(filers) >= 10)})",
        f"- Series with 2 or more loaded classes: {multi} (target at least 50, {mark(multi >= 50)})",
        f"- Series whose main class has a nonzero fee waiver: {waiver} "
        f"(target at least 10, {mark(waiver >= 10)})",
        "",
        "| Loaded class type | Count |",
        "| --- | --- |",
    ]
    for name in ("A", "investor", "F2", "F3", "C", "retirement", "529"):
        lines.append(f"| {name} | {loaded_counts[name]} |")

    lines += ["", "## Filers", "", "| Filer | CIK | Series |", "| --- | --- | --- |"]
    filer_rows = sorted(filers.values(), key=lambda group: (-len(group), group[0].filer_name))
    for group in filer_rows:
        lines.append(f"| {group[0].filer_name} | {group[0].cik} | {len(group)} |")

    unmatched = sorted(
        {series.series_name for series in result["eligible"] if series.fund_type == "unmatched"}
    )
    lines += ["", "## Unmatched fund-type names", ""]
    if unmatched:
        lines.append("These eligible series matched none of the type keywords and can only fill leftover slots:")
        lines.append("")
        for name in unmatched:
            lines.append(f"- {name}")
    else:
        lines.append("Every eligible series matched a fund type.")
    lines.append("")
    return "\n".join(lines)


def write_outputs(result: dict, universe_path: Path, classes_path: Path, report_path: Path) -> None:
    universe_path.parent.mkdir(parents=True, exist_ok=True)
    classes_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with universe_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=UNIVERSE_FIELDS, lineterminator="\n")
        writer.writeheader()
        for index, series in enumerate(result["selected"], start=1):
            main = series.main
            writer.writerow(
                {
                    "rank": index,
                    "series_id": series.series_id,
                    "series_name": series.series_name,
                    "filer_name": series.filer_name,
                    "cik": series.cik,
                    "fund_type": series.fund_type,
                    "main_class_id": main.class_id if main else "",
                    "main_class_type": main.class_type if main else "",
                    "ticker": main.ticker if main else "",
                    "net_assets": format_assets(series.net_assets),
                    "report_date": series.report_date,
                    "selected_by": series.selected_by,
                    "reason": series.reason,
                }
            )
    with classes_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CLASS_FIELDS, lineterminator="\n")
        writer.writeheader()
        for series in result["selected"]:
            for share in result["loaded"][series.series_id]:
                writer.writerow(
                    {
                        "series_id": series.series_id,
                        "class_id": share.class_id,
                        "class_name": share.class_name,
                        "class_type": share.class_type,
                        "ticker": share.ticker,
                    }
                )
    report_path.write_text(render_report(result), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--rr-zip", required=True, type=Path)
    parser.add_argument("--nport-zip", required=True, type=Path)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--universe", type=Path, default=Path("eval/fund_universe.csv"))
    parser.add_argument("--classes", type=Path, default=Path("eval/share_classes.csv"))
    parser.add_argument("--report", type=Path, default=Path("reports/selection.md"))
    args = parser.parse_args(argv)
    if args.overrides is not None and not args.overrides.exists():
        raise SystemExit(f"overrides file not found: {args.overrides}")

    result = build_selection(args.candidates, args.rr_zip, args.nport_zip, args.overrides)
    write_outputs(result, args.universe, args.classes, args.report)
    print(f"Selected {len(result['selected'])} series")
    print(f"Wrote {args.universe}, {args.classes}, and {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
