"""Classify project lines into documentation, code, and other buckets.

Uses `git ls-files -co --exclude-standard` to include tracked files and
untracked-but-not-ignored files in the current repository.

Examples:
  python scripts/line_classification.py
  python scripts/line_classification.py --top-ext 12 --top-files 8
  python scripts/line_classification.py --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DOC_EXTENSIONS = {
    ".md",
    ".rst",
    ".txt",
    ".adoc",
    ".org",
    ".rtf",
}

# Config-like and template files are intentionally treated as code-adjacent.
CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".kts",
    ".m",
    ".mm",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".psm1",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".vue",
    ".svelte",
    ".ipynb",
    ".r",
    ".jl",
    ".lua",
    ".pl",
    ".pm",
    ".dart",
    ".ex",
    ".exs",
    ".erl",
    ".hrl",
    ".nim",
    ".zig",
    ".tf",
    ".tfvars",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".xml",
    ".json",
}

DOC_BASENAMES = {
    "readme",
    "changelog",
    "contributing",
    "license",
    "authors",
    "code_of_conduct",
    "security",
    "roadmap",
    "notes",
    "todo",
}


def _list_repo_files(project_root: Path) -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=project_root,
        text=True,
    )
    return [project_root / rel for rel in output.splitlines() if rel.strip()]


def _count_lines(path: Path) -> int | None:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return sum(1 for _ in handle)
    except Exception:
        return None


def _classify(path: Path) -> str:
    basename = path.name.lower()
    stem = path.stem.lower()
    extension = path.suffix.lower()

    if extension in DOC_EXTENSIONS or stem in DOC_BASENAMES or basename in DOC_BASENAMES:
        return "documentation"
    if extension in CODE_EXTENSIONS:
        return "code"
    return "other"


def _percentage(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return (100.0 * part) / whole


def build_report(project_root: Path) -> dict:
    by_bucket: dict[str, int] = defaultdict(int)
    by_extension: dict[str, int] = defaultdict(int)
    code_files: list[tuple[int, str]] = []
    doc_files: list[tuple[int, str]] = []
    skipped_files: list[str] = []

    for path in _list_repo_files(project_root):
        line_count = _count_lines(path)
        rel_path = path.relative_to(project_root).as_posix()
        if line_count is None:
            skipped_files.append(rel_path)
            continue

        bucket = _classify(path)
        extension = path.suffix.lower() or "<no_ext>"
        by_bucket[bucket] += line_count
        by_extension[extension] += line_count

        if bucket == "code":
            code_files.append((line_count, rel_path))
        elif bucket == "documentation":
            doc_files.append((line_count, rel_path))

    total = by_bucket["documentation"] + by_bucket["code"] + by_bucket["other"]
    return {
        "totals": {
            "total": total,
            "documentation": by_bucket["documentation"],
            "code": by_bucket["code"],
            "other": by_bucket["other"],
            "documentation_pct": _percentage(by_bucket["documentation"], total),
            "code_pct": _percentage(by_bucket["code"], total),
            "other_pct": _percentage(by_bucket["other"], total),
        },
        "by_extension": sorted(by_extension.items(), key=lambda item: item[1], reverse=True),
        "top_code_files": sorted(code_files, reverse=True),
        "top_documentation_files": sorted(doc_files, reverse=True),
        "skipped_files": skipped_files,
    }


def _print_human_report(report: dict, top_ext: int, top_files: int) -> None:
    totals = report["totals"]
    print(f"Total lines: {totals['total']}")
    print(
        "Buckets: "
        f"documentation={totals['documentation']} ({totals['documentation_pct']:.1f}%), "
        f"code={totals['code']} ({totals['code_pct']:.1f}%), "
        f"other={totals['other']} ({totals['other_pct']:.1f}%)"
    )
    print()
    print(f"Top {top_ext} extensions:")
    for extension, count in report["by_extension"][:top_ext]:
        print(f"  {extension}: {count}")

    print()
    print(f"Top {top_files} code files:")
    for count, path in report["top_code_files"][:top_files]:
        print(f"  {count:>6}  {path}")

    print()
    print(f"Top {top_files} documentation files:")
    for count, path in report["top_documentation_files"][:top_files]:
        print(f"  {count:>6}  {path}")

    if report["skipped_files"]:
        print()
        print(f"Skipped unreadable files: {len(report['skipped_files'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate line-count classification for this repository.")
    parser.add_argument(
        "--top-ext",
        type=int,
        default=10,
        help="number of extensions to print in human output",
    )
    parser.add_argument(
        "--top-files",
        type=int,
        default=8,
        help="number of top code/doc files to print in human output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print full report JSON instead of human-readable output",
    )
    args = parser.parse_args()

    report = build_report(PROJECT_ROOT)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    _print_human_report(report, top_ext=max(1, args.top_ext), top_files=max(1, args.top_files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
