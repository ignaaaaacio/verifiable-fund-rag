"""Pick the 100-fund universe from coverage candidates, the Risk/Return zips, and N-PORT.

Usage:
    python3 scripts/select_funds.py \\
        --candidates reports/candidates.csv \\
        --rr-zip <rr zip> [--rr-zip <rr zip> ...] \\
        --nport-zip <nport zip> \\
        [--overrides eval/overrides.csv]

A series is kept only when it is not an ETF, has risk text, has a complete
Class A or a complete no-load Investor class (Admiral counts as Investor),
and has N-PORT net assets of at least $1 billion. Series are ranked by net
assets and filled into type quotas, with at most 8 series per fund family.
Alternatives are excluded. Expert overrides are applied after that rule.

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
FAMILY_CAP = 8
MAX_OVERRIDE_ADDS = 15
MINIMUM_INVESTMENT_CAP = Decimal("100000")
ASSET_FLOOR = Decimal("1000000000")
EXCLUDED_TYPES = {"alternative"}

QUOTAS: tuple[tuple[str, int], ...] = (
    ("target_date_balanced", 15),
    ("sector", 5),
    ("alternative", 0),
    ("bond", 20),
    ("index_equity", 20),
    ("international", 15),
    ("active_equity", 25),
)

LOAD_TAGS = {
    "MaximumSalesChargeImposedOnPurchasesOverOfferingPrice",
    "MaximumDeferredSalesChargeOverOfferingPrice",
    "MaximumDeferredSalesChargeOverOther",
    "MaximumCumulativeSalesChargeOverOfferingPrice",
}
FEE_WAIVER_TAG = "FeeWaiverOrReimbursementOverAssets"
FRONT_END_TAG = "MaximumSalesChargeImposedOnPurchasesOverOfferingPrice"
BREAKPOINT_TAG = "ExpenseBreakpointMinimumInvestmentRequiredAmount"
MINIMUM_TAG = re.compile(
    r"minimum.*initial.*invest|initial.*investment.*minimum|minimuminitialinvestment",
    re.I,
)
ETF_NAME = re.compile(r"\betf\b|exchange[\s-]?traded", re.I)

# Extra classes loaded besides the single main class. F-1 is reported and not loaded.
# R-1 through R-5 and 529 classes other than 529-A are not loaded.
LOAD_EXTRA = {"F2", "F3", "C", "R6", "529A"}

NOT_BOND = re.compile(
    r"equity\s+income|dividend\s+income|growth\s+(?:&|and)\s+income",
    re.I,
)
FUND_TYPE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "target_date_balanced",
        re.compile(
            r"\btarget\b|retirement\s+20|lifecycle|\bbalanced\b|\ballocation\b|income\s+builder|"
            r"smart\s*retirement|conservative\s+growth|"
            r"JPMorgan Investor (?:Balanced|Growth|Conservative)|"
            r"tactical\s+allocation|global\s+allocation",
            re.I,
        ),
    ),
    (
        "sector",
        re.compile(
            r"\bhealth|\btechnolog|\benergy\b|real\s+estate|\butilities\b|\bfinancial\b|"
            r"\bscience\b|\bgold\b|precious\s+metals|life\s+sciences|\binfrastructure\b",
            re.I,
        ),
    ),
    (
        "alternative",
        re.compile(
            r"\bhedge\b|(?<!dollar-)(?<!currency-)\bhedged\b|market\s+neutral|long\s*/\s*short|long[\s-]short|"
            r"tactical\s+plus|sector\s+plus|managed\s+futures",
            re.I,
        ),
    ),
    (
        "bond",
        re.compile(
            r"\bbond\b|\bincome\b|\bfixed\b|\btreasury\b|\bmunicipal\b|tax[-\s]?exempt|tax[-\s]?free|"
            r"\bcredit\b|\bduration\b|\bgovernment\b|securities\s+fund|\bmortgage\b|high\s+yield|"
            r"short[-\s]?term|limited\s+term|\bcorporate\b|\bultra\b",
            re.I,
        ),
    ),
    (
        "index_equity",
        re.compile(r"\bindex\b|\b500\b|total\s+stock|total\s+market", re.I),
    ),
    (
        "international",
        re.compile(
            r"\binternational\b|\bglobal\b|\bworld\b|\bemerging\b|\beurope\b|\bpacific\b|\bdeveloping\b|"
            r"\boverseas\b|\bforeign\b",
            re.I,
        ),
    ),
    (
        "active_equity",
        re.compile(r"\bequity\b|\bgrowth\b|\bvalue\b|\bfund\b|\bstock\b|\bcap\b", re.I),
    ),
)

MULTI_SPONSOR = re.compile(
    r"mutual fund series trust|northern lights|manager directed portfolios",
    re.I,
)
SPONSOR_SECOND_WORD = {"river", "capital", "point", "bridge", "harbor", "street"}

UNIVERSE_FIELDS = [
    "rank",
    "series_id",
    "series_name",
    "filer_name",
    "cik",
    "family",
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
    family: str = ""
    family_source: str = ""
    fee_waiver: bool = False
    selected_by: str = "rule"
    reason: str = ""
    how: str = ""
    type_before: str = ""
    type_reason: str = ""
    restricted_reason: str = ""

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


def format_charge(value: Decimal | None) -> str:
    """Front-end loads are fractions of the offering price. Breakpoints are dollar amounts."""
    if value is None:
        return "(none)"
    if abs(value) <= 1:
        percent = value * Decimal(100)
        text = format(percent, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text}%"
    return format_assets(value)


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


def class_type(name: str, series_name: str = "") -> str:
    """Map a share-class name onto the universe's class types.

    529 is tested first so Class 529-A and Class 529-F-2 stay 529. Investor A
    and Investor C are load classes, not the no-load Investor class. A class
    whose name is the series name is the unlabeled no-load retail class.
    """
    text = normalize_class_name(name)
    if not text:
        return "other"
    if re.search(r"\b529\b", text):
        if re.search(r"\b529\s+a\b", text):
            return "529A"
        return "529"
    if ETF_NAME.search(text):
        return "etf"
    if re.search(r"\bf\s*1\b", text):
        return "F1"
    if re.search(r"\bf\s*2\b", text):
        return "F2"
    if re.search(r"\bf\s*3\b", text):
        return "F3"
    if re.search(r"\badmiral\b", text):
        return "investor"
    if re.search(r"\binstitutional\b|\binstl\b", text):
        return "institutional"
    if re.search(r"\binvestor\s+a\d?\b", text) or re.search(r"(?:^|\bclass\s+)a\d?\b", text) or text in {"a", "a class", "a shares"}:
        return "A"
    if re.search(r"\binvestor\s+c\b", text) or re.search(r"(?:^|\bclass\s+)c\b", text) or text in {"c", "c class", "c shares"}:
        return "C"
    if re.search(r"\br\s*6\b(?!\s*e\b)", text):
        return "R6"
    if (
        re.search(r"\bretirement\b|\b401\s?k\b", text)
        or re.search(r"\br\s*[1-5]\b", text)
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
    if series_name and text == normalize_class_name(series_name):
        return "investor"
    return "other"


def fund_type(series_name: str) -> str:
    """Keyword types. Equity Income is not a bond; Gold is a sector, not international."""
    text = series_name or ""
    for name, pattern in FUND_TYPE_RULES:
        if name == "bond" and NOT_BOND.search(text):
            continue
        if pattern.search(text):
            return name
    return "unmatched"


def series_sponsor(series_name: str) -> str:
    """First word of a multi-sponsor series, or two words when the second is part of the name."""
    left = re.split(r"[/]", series_name or "", maxsplit=1)[0]
    words = re.findall(r"[A-Za-z0-9]+", left)
    if not words:
        return (series_name or "").strip() or "Unknown"
    if len(words) >= 2 and words[1].lower() in SPONSOR_SECOND_WORD:
        return f"{words[0]} {words[1]}"
    return words[0]


def load_family_rules(path: Path | None) -> list[tuple[re.Pattern[str], str]]:
    if path is None or not path.exists():
        return []
    rules = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = _column_map(reader.fieldnames)
        pattern_col = _col(columns, "pattern")
        family_col = _col(columns, "family")
        if not pattern_col or not family_col:
            raise SystemExit(f"{path} must have pattern and family columns")
        for row in reader:
            pattern = (row.get(pattern_col) or "").strip()
            family = (row.get(family_col) or "").strip()
            if not pattern or not family:
                continue
            rules.append((re.compile(pattern, re.I), family))
    return rules


def assign_family(series_name: str, filer_name: str, rules: list[tuple[re.Pattern[str], str]]) -> tuple[str, str]:
    """Return (family, source) where source is alias, sponsor, or filer."""
    blob = f"{series_name}\n{filer_name}"
    for pattern, family in rules:
        if pattern.search(blob):
            return family, "alias"
    if MULTI_SPONSOR.search(filer_name or ""):
        return series_sponsor(series_name), "sponsor"
    return filer_name or series_name, "filer"


def load_restricted_rules(path: Path | None) -> list[tuple[re.Pattern[str], str]]:
    """Patterns for series the public cannot buy. Columns: pattern, reason."""
    if path is None or not path.exists():
        return []
    rules = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = _column_map(reader.fieldnames)
        pattern_col = _col(columns, "pattern")
        reason_col = _col(columns, "reason")
        if not pattern_col:
            raise SystemExit(f"{path} must have a pattern column")
        for row in reader:
            pattern = (row.get(pattern_col) or "").strip()
            if not pattern:
                continue
            reason = (row.get(reason_col) or "").strip() if reason_col else ""
            rules.append((re.compile(pattern, re.I), reason))
    return rules


def restricted_reason(series_name: str, rules: list[tuple[re.Pattern[str], str]]) -> str:
    for pattern, reason in rules:
        if pattern.search(series_name or ""):
            return reason or "not available to retail investors"
    return ""


def load_class_type_overrides(path: Path | None) -> dict[str, str]:
    """Map a class id or ticker to a class type. Columns: class_id or ticker, class_type, reason."""
    if path is None or not path.exists():
        return {}
    overrides: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = _column_map(reader.fieldnames)
        ident_col = _col(columns, "classid", "class_id", "ticker")
        type_col = _col(columns, "classtype", "class_type")
        if not ident_col or not type_col:
            raise SystemExit(f"{path} must have class_id or ticker, and class_type")
        ticker_col = _col(columns, "ticker") if ident_col != _col(columns, "ticker") else None
        for row in reader:
            class_type_name = (row.get(type_col) or "").strip()
            if not class_type_name:
                continue
            for column in (ident_col, ticker_col):
                if not column:
                    continue
                ident = (row.get(column) or "").strip().upper()
                if ident:
                    overrides[ident] = class_type_name
    return overrides


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


def read_candidates(path: Path, class_overrides: dict[str, str] | None = None) -> list[Series]:
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
                class_type=class_type(cell(row, "classname"), cell(row, "seriesname")),
            )
            overrides = class_overrides or {}
            forced = overrides.get(share.class_id) or overrides.get(share.ticker.upper())
            if forced:
                share.class_type = forced
            series.classes.append(share)
    series_list = list(grouped.values())
    for series in series_list:
        series.fund_type = fund_type(series.series_name)
        series.type_before = series.fund_type
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


def _read_rr_zip(zip_path: Path, wanted: set[tuple[str, str]]) -> dict:
    """Sales loads, fee waivers, and minimum initial investments in one zip."""
    loaded: set[str] = set()
    waived: set[str] = set()
    minimums: dict[str, Decimal] = {}
    minimum_tags: set[str] = set()
    front_end: dict[str, Decimal] = {}
    breakpoints: dict[str, set[Decimal]] = {}
    series_breakpoints: dict[tuple[str, str], set[Decimal]] = {}
    wanted_adsh = {adsh for adsh, _ in wanted}
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
            if tag == BREAKPOINT_TAG:
                amount = parse_number(row.get("value"))
                if amount is None:
                    continue
                series_id = (row.get("series") or "").strip().upper()
                if not series_id:
                    found = re.search(r"LegalEntity=(S[0-9]+)", row.get("otherdims") or "", re.I)
                    series_id = found.group(1).upper() if found else ""
                if class_id and relevant:
                    breakpoints.setdefault(class_id, set()).add(amount)
                elif not class_id and adsh in wanted_adsh and series_id:
                    series_breakpoints.setdefault((adsh, series_id), set()).add(amount)
                continue
            if not relevant:
                continue
            amount = parse_number(row.get("value"))
            if amount is None:
                continue
            if tag == FRONT_END_TAG:
                current = front_end.get(class_id)
                if current is None or amount > current:
                    front_end[class_id] = amount
            if tag in LOAD_TAGS and amount > 0:
                loaded.add(class_id)
            elif tag == FEE_WAIVER_TAG and amount != 0:
                waived.add(class_id)
    return {
        "loaded": loaded,
        "waived": waived,
        "minimums": minimums,
        "minimum_tags": sorted(minimum_tags),
        "front_end": front_end,
        "breakpoints": breakpoints,
        "series_breakpoints": series_breakpoints,
    }


def read_rr_facts(zip_paths: Path | list[Path], wanted: set[tuple[str, str]]) -> dict:
    """Union of sales-load and fee facts across every Risk/Return zip."""
    paths = [zip_paths] if isinstance(zip_paths, Path) else list(zip_paths)
    loaded: set[str] = set()
    waived: set[str] = set()
    minimums: dict[str, Decimal] = {}
    minimum_tags: set[str] = set()
    front_end: dict[str, Decimal] = {}
    breakpoints: dict[str, set[Decimal]] = {}
    series_breakpoints: dict[tuple[str, str], set[Decimal]] = {}
    for path in paths:
        part = _read_rr_zip(path, wanted)
        loaded |= part["loaded"]
        waived |= part["waived"]
        minimum_tags.update(part["minimum_tags"])
        for class_id, amount in part["minimums"].items():
            current = minimums.get(class_id)
            if current is None or amount < current:
                minimums[class_id] = amount
        for class_id, amount in part["front_end"].items():
            current = front_end.get(class_id)
            if current is None or amount > current:
                front_end[class_id] = amount
        for class_id, amounts in part["breakpoints"].items():
            breakpoints.setdefault(class_id, set()).update(amounts)
        for series_id, amounts in part["series_breakpoints"].items():
            series_breakpoints.setdefault(series_id, set()).update(amounts)
    return {
        "loaded": loaded,
        "waived": waived,
        "minimums": minimums,
        "minimum_tags": sorted(minimum_tags),
        "front_end": front_end,
        "breakpoints": breakpoints,
        "series_breakpoints": series_breakpoints,
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
        return sorted(
            investor,
            key=lambda share: ("admiral" not in share.class_name.lower(), share.ticker == "", share.class_id),
        )[0]
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
    family_cap: int = FAMILY_CAP,
    filer_cap: int | None = None,
) -> list[Series]:
    """Walk the ranking. Unfilled quota slots go to the next-largest series of any included type.

    Alternatives (quota 0) are never selected. filer_cap is the old name of family_cap.
    """
    cap = family_cap if filer_cap is None else filer_cap
    remaining = {name: count for name, count in quotas}
    per_family: Counter = Counter()
    selected: list[Series] = []
    waiting: list[Series] = []
    for series in ranked:
        if series.fund_type in EXCLUDED_TYPES:
            continue
        family = series.family or series.filer_name or series.series_id
        if per_family[family] >= cap:
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
        per_family[family] += 1
    slots = sum(count for name, count in remaining.items() if name not in EXCLUDED_TYPES)
    for series in waiting:
        if slots <= 0:
            break
        family = series.family or series.filer_name or series.series_id
        if per_family[family] >= cap:
            continue
        series.how = "leftover"
        series.reason = "leftover"
        series.selected_by = "rule"
        selected.append(series)
        per_family[family] += 1
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
    """Main class, plus complete F-2, F-3, C, R-6, and 529-A. Not F-1, R-1–R-5, or other 529s."""
    loaded = []
    main_id = series.main.class_id if series.main else ""
    for share in series.classes:
        if not share.complete:
            continue
        if share.class_id == main_id or share.class_type in LOAD_EXTRA:
            loaded.append(share)
    return sorted(loaded, key=lambda share: share.class_id)


def attach_assets(series_list: list[Series], assets: dict[str, tuple[Decimal | None, str]]) -> None:
    for series in series_list:
        net, report_date = assets.get(series.series_id, (None, ""))
        series.net_assets = net
        series.report_date = report_date


def _read_type_overrides(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        columns = _column_map(reader.fieldnames)
        series_col = _col(columns, "seriesid", "series_id")
        type_col = _col(columns, "fundtype", "fund_type")
        reason_col = _col(columns, "reason")
        if not series_col or not type_col:
            raise SystemExit(f"{path} must have series_id and fund_type columns")
        rows = []
        for row in reader:
            series_id = (row.get(series_col) or "").strip().upper()
            fund_type_name = (row.get(type_col) or "").strip()
            if not series_id or not fund_type_name:
                continue
            rows.append(
                {
                    "series_id": series_id,
                    "fund_type": fund_type_name,
                    "reason": (row.get(reason_col) or "").strip() if reason_col else "",
                }
            )
        return rows


def apply_type_overrides(series_list: list[Series], rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Apply manual types before quotas. Returns the corrections that matched a series."""
    by_id = {series.series_id: series for series in series_list}
    applied = []
    for row in rows:
        series = by_id.get(row["series_id"])
        if series is None:
            applied.append({**row, "series_name": "", "previous": "", "applied": False})
            continue
        previous = series.fund_type
        series.type_before = previous
        series.fund_type = row["fund_type"]
        series.type_reason = row["reason"]
        applied.append(
            {
                **row,
                "series_name": series.series_name,
                "previous": previous,
                "applied": previous != row["fund_type"],
            }
        )
    return applied


def apply_asset_floor(eligible: list[Series], floor: Decimal) -> tuple[list[Series], list[Series]]:
    """Drop series under the floor and series with no N-PORT assets."""
    kept = []
    dropped = []
    for series in eligible:
        if series.net_assets is None or series.net_assets < floor:
            dropped.append(series)
        else:
            kept.append(series)
    return kept, dropped


def build_selection(
    candidates: Path,
    rr_zip: Path | list[Path],
    nport_zip: Path,
    overrides: Path | None = None,
    quotas: tuple[tuple[str, int], ...] = QUOTAS,
    family_cap: int = FAMILY_CAP,
    filer_cap: int | None = None,
    asset_floor: Decimal = ASSET_FLOOR,
    families_path: Path | None = Path("eval/families.csv"),
    type_overrides_path: Path | None = Path("eval/fund_type_overrides.csv"),
    class_overrides_path: Path | None = Path("eval/class_type_overrides.csv"),
    restricted_path: Path | None = Path("eval/restricted_funds.csv"),
) -> dict:
    series_list = read_candidates(candidates, load_class_type_overrides(class_overrides_path))
    rules = load_family_rules(families_path)
    for series in series_list:
        series.family, series.family_source = assign_family(series.series_name, series.filer_name, rules)
    type_rows = _read_type_overrides(type_overrides_path)
    type_corrections = apply_type_overrides(series_list, type_rows)
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
    eligible, too_small = apply_asset_floor(eligible, asset_floor)
    restricted_rules = load_restricted_rules(restricted_path)
    restricted: list[Series] = []
    public: list[Series] = []
    for series in eligible:
        reason = restricted_reason(series.series_name, restricted_rules)
        if reason:
            series.restricted_reason = reason
            restricted.append(series)
        else:
            public.append(series)
    would = fill_quotas(
        rank_series(eligible), quotas=quotas, family_cap=family_cap, filer_cap=filer_cap
    )
    would_ids = {series.series_id for series in would}
    ranked = rank_series(public)
    selected = fill_quotas(ranked, quotas=quotas, family_cap=family_cap, filer_cap=filer_cap)
    removed = [series for series in would if series.series_id not in {item.series_id for item in selected}]
    added = [series for series in selected if series.series_id not in would_ids]
    eligible = public
    override_rows: list[dict[str, str]] = []
    if overrides is not None:
        override_rows = _override_rows(overrides)
        selected = apply_overrides(selected, eligible, override_rows)
    final = rank_series(selected)
    loaded = {series.series_id: classes_to_load(series) for series in final}
    rr_names = [rr_zip.name] if isinstance(rr_zip, Path) else [path.name for path in rr_zip]
    return {
        "series_in": series_list,
        "eligible": eligible,
        "dropped": {**filtered["dropped"], "small": too_small, "restricted": restricted},
        "minimum_active": filtered["minimum_active"],
        "minimum_tags": facts["minimum_tags"],
        "ranked": ranked,
        "selected": final,
        "loaded": loaded,
        "quotas": quotas,
        "overrides": override_rows,
        "type_corrections": type_corrections,
        "removed": removed,
        "added": added,
        "front_end": facts["front_end"],
        "breakpoints": facts["breakpoints"],
        "series_breakpoints": facts["series_breakpoints"],
        "asset_floor": asset_floor,
        "family_cap": family_cap if filer_cap is None else filer_cap,
        "inputs": {
            "candidates": candidates,
            "rr_zip": rr_zip,
            "rr_names": rr_names,
            "nport_zip": nport_zip,
            "overrides": overrides,
            "families": families_path,
            "type_overrides": type_overrides_path,
            "class_overrides": class_overrides_path,
            "restricted": restricted_path,
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


def _retail_section(result: dict) -> list[str]:
    restricted = result["dropped"]["restricted"]
    removed_ids = {series.series_id for series in result["removed"]}
    lines = ["", "## Not available to retail investors", ""]
    if not restricted:
        lines.append("No series that passed the other filters matched a restricted-fund pattern.")
        lines.append("")
        return lines
    lines.append(
        f"{len(restricted)} series passed the other hard filters and the size floor, "
        "then were dropped because the public cannot buy them."
    )
    lines.append("")
    lines.append("| Series | Reason | Had a slot in the 100 |")
    lines.append("| --- | --- | --- |")
    ordered = sorted(restricted, key=lambda series: (-(series.net_assets or Decimal(0)), series.series_name))
    for series in ordered:
        slot = "yes" if series.series_id in removed_ids else "no"
        lines.append(f"| {series.series_name} | {series.restricted_reason} | {slot} |")
    lines += ["", "### Replaced by", ""]
    added = result["added"]
    if not added:
        lines.append("Dropping these series did not change which funds filled the 100.")
    else:
        lines.append("These funds took the slots the restricted series had held:")
        lines.append("")
        for series in sorted(added, key=lambda item: (-(item.net_assets or Decimal(0)), item.series_name)):
            ticker = series.main.ticker if series.main else ""
            detail = f"{series.family}, {series.fund_type}"
            if ticker:
                detail += f", {ticker}"
            lines.append(f"- {series.series_name} ({detail})")
    extra = [series for series in result["removed"] if not series.restricted_reason]
    if extra:
        lines.append("")
        lines.append("These other series also left the 100 when the quotas and family cap were filled again:")
        for series in extra:
            lines.append(f"- {series.series_name} ({series.family})")
    lines.append("")
    return lines


def _breakpoint_section(result: dict) -> list[str]:
    front = result.get("front_end") or {}
    points = result.get("breakpoints") or {}
    series_points = result.get("series_breakpoints") or {}
    listed = [
        (series, share)
        for series in result["selected"]
        for share in result["loaded"][series.series_id]
        if share.class_type in {"A", "529A"}
    ]
    lines = [
        "",
        "## Front-end sales charge and breakpoints",
        "",
        "Loaded Class A and 529-A only. The front-end figure is the maximum "
        "`MaximumSalesChargeImposedOnPurchasesOverOfferingPrice`, which is the sales charge before "
        "any breakpoint discount. `ExpenseBreakpointMinimumInvestmentRequiredAmount` is only the "
        "investment level where a discount starts ($25,000, $50,000, or $100,000 here). It does "
        "not say what the charge falls to at $100,000 or $400,000, so those amounts cannot be "
        "priced from this tag. A cited answer can show the maximum sales charge, say the "
        "investment qualifies for a discount, and compute the cost before that discount. Most "
        "filings leave the class blank and put only the series id in `otherdims`, so a series-level "
        "amount is shown on each loaded A and 529-A of that series and marked series. 0% means the "
        "front-end tag is present and the maximum is zero. (none) means that tag was not filed "
        "for the class or its series.",
        "",
    ]
    a_shares = [share for _, share in listed if share.class_type == "A"]
    charge_counts: Counter = Counter()
    point_counts: Counter = Counter()
    with_charge = 0
    with_point = 0
    for share in a_shares:
        if share.class_id in front:
            with_charge += 1
        charge_counts[format_charge(front.get(share.class_id))] += 1
        amounts = points.get(share.class_id) or series_points.get((share.adsh, share.series_id)) or set()
        if amounts:
            with_point += 1
            for amount in amounts:
                point_counts[format_assets(amount)] += 1
        else:
            point_counts["(none)"] += 1
    lines.append(
        f"Loaded Class A: {len(a_shares)}. {with_charge} have a front-end sales-charge tag. "
        f"{with_point} have at least one breakpoint amount."
    )
    lines += ["", "| Front-end charge | Class A count |", "| --- | --- |"]
    for label, count in sorted(charge_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {label} | {count} |")
    lines += ["", "| Breakpoint amount | Class A count |", "| --- | --- |"]
    for label, count in sorted(point_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {label} | {count} |")
    lines += [
        "",
        "| Series | Class | Ticker | Type | Max front-end | Breakpoint amounts |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if not listed:
        lines.append("| (none) |  |  |  |  |  |")
    for series, share in sorted(listed, key=lambda item: (item[0].series_name, item[1].class_id)):
        class_amounts = points.get(share.class_id) or set()
        series_amounts = series_points.get((share.adsh, series.series_id)) or set()
        if class_amounts:
            shown = ", ".join(format_assets(amount) for amount in sorted(class_amounts))
        elif series_amounts:
            shown = ", ".join(format_assets(amount) for amount in sorted(series_amounts)) + " (series)"
        else:
            shown = "(none)"
        lines.append(
            f"| {series.series_name} | {share.class_name} | {share.ticker} | {share.class_type} | "
            f"{format_charge(front.get(share.class_id))} | {shown} |"
        )
    lines.append("")
    return lines


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
        f"- Risk/Return zips: {', '.join(f'`{name}`' for name in inputs['rr_names'])}",
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
    lines.append(f"- Dropped, net assets under ${format_assets(result['asset_floor'])} or no N-PORT match: {len(dropped['small'])}")
    lines.append(f"- Dropped, not available to retail investors: {len(dropped['restricted'])}")
    still_restricted = [series for series in selected if series.restricted_reason]
    lines.append(
        "- No selected fund is closed to retail investors: "
        + ("met" if not still_restricted else "not met")
    )
    lines.append(f"- Eligible after hard filters, the size floor, and the retail filter: {len(result['eligible'])}")
    lines.append(f"- Selected: {len(selected)}")
    below = [series for series in selected if series.net_assets is None or series.net_assets < result["asset_floor"]]
    lines.append(
        f"- No selected fund under ${format_assets(result['asset_floor'])}: {'met' if not below else 'not met'}"
    )
    if len(selected) < 100:
        lines.append(
            f"- Fewer than 100 because {len(result['eligible'])} series passed the hard filters "
            "and the size floor, and the quotas, family cap, and overrides left this many."
        )
    lines += ["", "## Quotas", "", "| Type | Quota | Filled by quota | Filled by leftover | Selected |", "| --- | --- | --- | --- | --- |"]
    for name, quota, by_quota, by_left in _quota_table(result):
        lines.append(f"| {name} | {quota} | {by_quota} | {by_left} | {by_quota + by_left} |")
    if any(series.fund_type == "unmatched" for series in selected):
        count = sum(1 for series in selected if series.fund_type == "unmatched")
        lines.append(f"| unmatched | 0 | 0 | {count} | {count} |")

    eligible_by_type: Counter = Counter(series.fund_type for series in result["eligible"])
    index_quota = dict(result["quotas"]).get("index_equity", 0)
    index_selected = sum(1 for series in selected if series.fund_type == "index_equity")
    index_eligible = eligible_by_type["index_equity"]
    lines += ["", "## Index equity", ""]
    if index_selected >= index_quota:
        lines.append(f"Index equity filled its quota ({index_selected} selected, quota {index_quota}).")
    elif index_eligible < index_quota:
        lines.append(
            f"Index equity is not full: {index_selected} selected of quota {index_quota}. "
            f"Only {index_eligible} index series passed the hard filters and the size floor."
        )
    else:
        lines.append(
            f"Index equity is not full: {index_selected} selected of quota {index_quota}, "
            f"with {index_eligible} eligible. The family cap held the rest back."
        )

    loaded_counts: Counter = Counter()
    multi = 0
    waiver = 0
    families: dict[str, list[Series]] = defaultdict(list)
    for series in selected:
        shares = result["loaded"][series.series_id]
        if len(shares) >= 2:
            multi += 1
        if series.fee_waiver:
            waiver += 1
        for share in shares:
            loaded_counts[share.class_type] += 1
        families[series.family or series.filer_name].append(series)

    def mark(ok: bool) -> str:
        return "met" if ok else "not met"

    lines += [
        "",
        "## Mix checks",
        "",
        "These checks are reported and do not change the selection.",
        "",
        f"- Distinct families: {len(families)} (target at least 10, {mark(len(families) >= 10)})",
        f"- No family above {result['family_cap']} series: "
        f"{mark(all(len(group) <= result['family_cap'] for group in families.values()))}",
        f"- Loaded share classes: {sum(loaded_counts.values())}",
        f"- Series with 2 or more loaded classes: {multi} (target at least 50, {mark(multi >= 50)})",
        f"- Series whose main class has a nonzero fee waiver: {waiver} "
        f"(target at least 10, {mark(waiver >= 10)})",
        "",
        "| Loaded class type | Count |",
        "| --- | --- |",
    ]
    for name in ("A", "investor", "F1", "F2", "F3", "C", "R6", "529A"):
        lines.append(f"| {name} | {loaded_counts[name]} |")
    lines.append("")
    f3_shares = [share for series in selected for share in series.classes if share.class_type == "F3"]
    f3_complete = sum(1 for share in f3_shares if share.complete)
    if loaded_counts["F3"] == 0:
        if not f3_shares:
            f3_why = "No selected series has a class named F-3."
        else:
            f3_why = (
                f"{len(f3_shares)} F-3 classes sit on selected series and {f3_complete} of them "
                "are complete, so none is loaded. They stay classified as F-3. Coverage shows the "
                "incomplete ones are missing a flag, almost always the 1/5/10-year return."
            )
    else:
        f3_why = f"{loaded_counts['F3']} complete F-3 classes were loaded."
    lines.append(
        "F-1 is its own class type and is not loaded. R-6 is the only retirement class loaded, "
        "and 529-A is the only 529 class loaded. " + f3_why
    )
    lines.extend(_retail_section(result))
    lines.extend(_breakpoint_section(result))

    lines += ["", "## Families", "", "| Family | Series |", "| --- | --- |"]
    family_rows = sorted(families.values(), key=lambda group: (-len(group), group[0].family))
    for group in family_rows:
        lines.append(f"| {group[0].family} | {len(group)} |")

    fallbacks = [
        series
        for series in result["eligible"]
        if series.family_source == "filer"
    ]
    lines += ["", "## Family fallbacks", ""]
    if not fallbacks:
        lines.append("No eligible series fell back to its filer name.")
    else:
        lines.append(
            f"{len(fallbacks)} eligible series did not match a family alias or a multi-sponsor trust. "
            "Their family is the filer name."
        )
        lines.append("")
        for series in sorted(fallbacks, key=lambda item: (item.family, item.series_name)):
            lines.append(f"- {series.series_name} ({series.filer_name}) → {series.family}")

    corrections = result.get("type_corrections") or []
    lines += ["", "## Fund-type corrections", ""]
    if not corrections:
        lines.append("No rows in the fund-type override file.")
    else:
        for row in corrections:
            if not row.get("series_name"):
                lines.append(f"- `{row['series_id']}` was not in the candidates ({row['reason']})")
                continue
            lines.append(
                f"- {row['series_name']} (`{row['series_id']}`): {row['previous']} → {row['fund_type']}"
                + (f" — {row['reason']}" if row["reason"] else "")
            )

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
                    "family": series.family,
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
    parser.add_argument("--rr-zip", required=True, action="append", type=Path, dest="rr_zips")
    parser.add_argument("--nport-zip", required=True, type=Path)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--families", type=Path, default=Path("eval/families.csv"))
    parser.add_argument("--type-overrides", type=Path, default=Path("eval/fund_type_overrides.csv"))
    parser.add_argument("--class-overrides", type=Path, default=Path("eval/class_type_overrides.csv"))
    parser.add_argument("--restricted", type=Path, default=Path("eval/restricted_funds.csv"))
    parser.add_argument("--asset-floor", type=Decimal, default=ASSET_FLOOR)
    parser.add_argument("--universe", type=Path, default=Path("eval/fund_universe.csv"))
    parser.add_argument("--classes", type=Path, default=Path("eval/share_classes.csv"))
    parser.add_argument("--report", type=Path, default=Path("reports/selection.md"))
    args = parser.parse_args(argv)
    if args.overrides is not None and not args.overrides.exists():
        raise SystemExit(f"overrides file not found: {args.overrides}")

    result = build_selection(
        args.candidates,
        args.rr_zips,
        args.nport_zip,
        args.overrides,
        families_path=args.families,
        type_overrides_path=args.type_overrides,
        class_overrides_path=args.class_overrides,
        restricted_path=args.restricted,
        asset_floor=args.asset_floor,
    )
    write_outputs(result, args.universe, args.classes, args.report)
    fallbacks = [series for series in result["eligible"] if series.family_source == "filer"]
    if fallbacks:
        print(f"Family fell back to the filer name for {len(fallbacks)} eligible series:")
        for series in sorted(fallbacks, key=lambda item: (item.family, item.series_name)):
            print(f"  {series.series_name} ({series.filer_name})")
    print(f"Selected {len(result['selected'])} series")
    print(f"Wrote {args.universe}, {args.classes}, and {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
