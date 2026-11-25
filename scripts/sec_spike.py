"""M0 data spike: inspect one SEC Mutual Fund Prospectus Risk/Return Summary data set.

Run it on your own computer (the SEC site requires a contact in the User-Agent):

    python scripts/sec_spike.py --url <zip URL from the SEC data-set page> \
        --user-agent "Your Name your@email.com"

or, if you already downloaded the zip:

    python scripts/sec_spike.py --zip path/to/2026q3_rr1.zip

It writes reports/spike_report.md with:
  * every file in the zip, its columns, and its row count;
  * which taxonomy versions appear (RR vs OEF);
  * how many tags look like fees, expense examples, returns, and risks;
  * how many funds (series) and share classes have each kind of data.

Only the Python standard library is used, so no install is needed.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# Keyword groups used to spot the tags we care about. These are deliberately broad;
# the report lists the actual tag names so the RR/OEF mapping can be written from them.
TAG_GROUPS: dict[str, re.Pattern[str]] = {
    "fees_and_expense_ratios": re.compile(
        r"(expense|fee|waiver|reimburs|load|12b1|distribution)", re.I
    ),
    "expense_example": re.compile(r"expenseexample", re.I),
    "returns": re.compile(r"(return|performance)", re.I),
    "risk": re.compile(r"risk", re.I),
}


def download(url: str, user_agent: str, dest: Path) -> Path:
    """Download the data-set zip with the SEC-required User-Agent."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
        out.write(response.read())
    return dest


def read_table(zf: zipfile.ZipFile, name: str) -> tuple[list[str], list[dict[str, str]]]:
    """Read one tab-delimited member of the zip into (columns, rows)."""
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        reader = csv.DictReader(text, delimiter="\t")
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def find_column(columns: list[str], *candidates: str) -> str | None:
    """Return the first column whose lowercase name contains one of the candidates."""
    lowered = {c.lower(): c for c in columns}
    for cand in candidates:
        for low, original in lowered.items():
            if cand in low:
                return original
    return None


def analyze(zip_path: Path) -> dict:
    """Inventory the zip and collect tag statistics."""
    result: dict = {"files": [], "versions": Counter(), "groups": {}}
    group_tags: dict[str, Counter] = {g: Counter() for g in TAG_GROUPS}
    group_series: dict[str, set] = defaultdict(set)
    group_classes: dict[str, set] = defaultdict(set)

    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith("/"):
                continue
            if not name.lower().endswith((".tsv", ".txt")):
                result["files"].append({"name": name, "columns": [], "rows": None})
                continue
            columns, rows = read_table(zf, name)
            result["files"].append({"name": name, "columns": columns, "rows": len(rows)})

            version_col = find_column(columns, "version")
            tag_col = next((c for c in columns if c.lower() == "tag"), None)
            series_col = find_column(columns, "series")
            class_col = find_column(columns, "class")

            for row in rows:
                if version_col and row.get(version_col):
                    result["versions"][row[version_col].split("/")[0].lower()] += 1
                if not tag_col:
                    continue
                tag = row.get(tag_col) or ""
                for group, pattern in TAG_GROUPS.items():
                    if pattern.search(tag):
                        group_tags[group][tag] += 1
                        if series_col and row.get(series_col):
                            group_series[group].add(row[series_col])
                        if class_col and row.get(class_col):
                            group_classes[group].add(row[class_col])

    for group in TAG_GROUPS:
        result["groups"][group] = {
            "distinct_tags": len(group_tags[group]),
            "top_tags": group_tags[group].most_common(25),
            "series": len(group_series[group]),
            "classes": len(group_classes[group]),
        }
    return result


def render(result: dict, source: str) -> str:
    """Turn the analysis into a Markdown report."""
    lines = [f"# SEC Risk/Return data spike\n", f"Source: `{source}`\n", "## Files\n"]
    lines.append("| File | Rows | Columns |")
    lines.append("| --- | --- | --- |")
    for f in result["files"]:
        cols = ", ".join(f["columns"]) if f["columns"] else "(not a table)"
        rows = "" if f["rows"] is None else f"{f['rows']:,}"
        lines.append(f"| {f['name']} | {rows} | {cols} |")

    lines.append("\n## Taxonomy versions seen\n")
    if result["versions"]:
        for version, count in result["versions"].most_common():
            lines.append(f"- {version}: {count:,} rows")
    else:
        lines.append("- No version column found; check the file list above.")

    lines.append("\n## Tag groups\n")
    for group, info in result["groups"].items():
        lines.append(f"### {group}\n")
        lines.append(
            f"- Distinct tags: {info['distinct_tags']} · funds (series): {info['series']} "
            f"· share classes: {info['classes']}"
        )
        for tag, count in info["top_tags"]:
            lines.append(f"  - `{tag}`: {count:,}")
        lines.append("")

    returns = result["groups"]["returns"]["distinct_tags"]
    lines.append("## Checks for M0/M1\n")
    lines.append(f"- Return tags present: {'yes' if returns else 'NO, review scope'}")
    lines.append(
        f"- Funds with fee data: {result['groups']['fees_and_expense_ratios']['series']} "
        "(target: at least 75 with complete data)"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--zip", type=Path, help="path to a downloaded data-set zip")
    src.add_argument("--url", help="URL of a data-set zip on sec.gov")
    parser.add_argument("--user-agent", help='required with --url, e.g. "Jane Doe jane@example.com"')
    parser.add_argument("--out", type=Path, default=Path("reports/spike_report.md"))
    args = parser.parse_args(argv)

    if args.url:
        if not args.user_agent:
            parser.error("--user-agent is required with --url (the SEC asks for a name and email)")
        zip_path = download(args.url, args.user_agent, Path("data") / Path(args.url).name)
        source = args.url
    else:
        zip_path = args.zip
        source = str(args.zip)

    result = analyze(zip_path)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(result, source), encoding="utf-8")
    print(f"Report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
