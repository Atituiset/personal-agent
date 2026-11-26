#!/usr/bin/env python3
"""Documentation checks: keep AGENTS.md a working map and docs/ from rotting.

    python scripts/doc_check.py                  # exit 1 on errors; warnings are printed but do not fail
    python scripts/doc_check.py --max-age-days 180 --strict   # --strict also fails on warnings

Checks
  links        every relative Markdown link in tracked *.md files points to an existing file
  paths        every `backticked/repo/path` in tracked *.md files exists
               (skipped for proposals and active exec plans, which describe code that does not exist yet)
  agents-map   AGENTS.md links every required doc, every path it mentions exists, and it stays short
  front-matter managed docs carry owner / last_reviewed / code_paths; code_paths exist;
               exec plans carry a status that matches their directory
  freshness    last_reviewed is a real date, not in the future, and not older than --max-age-days (error);
               a code path changed in git after last_reviewed (warning: re-read the doc and bump the date)

Only the standard library is used. Every message ends with what to do about it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_IN_AGENTS = [
    "ARCHITECTURE.md", "RELIABILITY.md", "SECURITY.md", "QUALITY_SCORE.md", "PLANS.md",
    "docs/design-docs/index.md", "docs/design-docs/core-beliefs.md",
    "docs/exec-plans/tech-debt-tracker.md", "evals/README.md",
]
AGENTS_MAX_LINES = 120

# Docs whose front matter is mandatory.
MANAGED_GLOBS = ["ARCHITECTURE.md", "RELIABILITY.md", "SECURITY.md", "QUALITY_SCORE.md", "PLANS.md",
                 "docs/design-docs/*.md", "docs/references/*.md", "docs/exec-plans/*.md",
                 "docs/exec-plans/active/*.md", "docs/exec-plans/completed/*.md"]
TEMPLATES = {"docs/exec-plans/TEMPLATE.md"}
# Files allowed to mention paths that do not exist (yet).
PATH_CHECK_EXEMPT_GLOBS = ["docs/design.md", "docs/exec-plans/active/*.md", "docs/exec-plans/TEMPLATE.md"]

PATH_ROOTS = ("personal_agent/", "evals/", "scripts/", "tests/", "docs/", "migrations/", ".github/")
ROOT_FILES = {"AGENTS.md", "ARCHITECTURE.md", "RELIABILITY.md", "SECURITY.md", "QUALITY_SCORE.md", "PLANS.md",
              "README.md", "Makefile", "pyproject.toml", "docker-compose.yml", ".env.example", "LICENSE"}

LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CODE_RE = re.compile(r"`([^`\n]+)`")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, rule: str, msg: str, fix: str):
        self.errors.append(f"{where}: [{rule}] {msg}\n    fix: {fix}")

    def warn(self, where: str, rule: str, msg: str, fix: str):
        self.warnings.append(f"{where}: [{rule}] {msg}\n    fix: {fix}")


def tracked_markdown(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "*.md"], cwd=root,
                             capture_output=True, text=True, check=True).stdout.split()
        return sorted(root / p for p in out if (root / p).exists())
    except (OSError, subprocess.CalledProcessError):
        skip = {".venv", "venv", ".git", "node_modules", "var"}
        return sorted(p for p in root.rglob("*.md") if not skip & set(p.relative_to(root).parts))


def matches(rel: str, globs) -> bool:
    return any(Path(rel).match(g) and rel.count("/") == g.count("/") for g in globs)


def parse_front_matter(text: str) -> dict | None:
    """Tiny YAML subset: `key: value` and `key: [a, b]` between leading --- lines."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    meta = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key.strip()] = value.strip("\"'")
    return meta


def body_lines(text: str):
    """(line number, line) outside fenced code blocks; front matter skipped."""
    lines = text.splitlines()
    start = 0
    if text.startswith("---\n"):
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    fenced = False
    for i in range(start, len(lines)):
        if FENCE_RE.match(lines[i]):
            fenced = not fenced
            continue
        if not fenced:
            yield i + 1, lines[i]


def looks_like_path(token: str) -> str | None:
    token = token.strip().split("::")[0].rstrip(":,.")
    if any(c in token for c in " *<>{}|$=") or token.startswith(("http", "-")):
        return None
    if token in ROOT_FILES:
        return token
    if token.startswith(PATH_ROOTS) and len(token) > len(token.split("/")[0]) + 1:
        return token
    return None


def check_links_and_paths(root: Path, path: Path, text: str, rep: Report) -> set[str]:
    rel = path.relative_to(root).as_posix()
    check_paths = not matches(rel, PATH_CHECK_EXEMPT_GLOBS)
    mentioned = set()
    for lineno, line in body_lines(text):
        for target in LINK_RE.findall(line):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path = target.split("#")[0]
            if not target_path:
                continue
            resolved = (path.parent / target_path).resolve()
            try:
                mentioned.add(resolved.relative_to(root.resolve()).as_posix().rstrip("/"))
            except ValueError:
                pass
            if not resolved.exists():
                rep.error(f"{rel}:{lineno}", "links", f"broken link to {target}",
                          "point the link at an existing file (paths are relative to this file) or remove it")
        if not check_paths:
            continue
        for token in CODE_RE.findall(line):
            p = looks_like_path(token)
            if p is None:
                continue
            mentioned.add(p.rstrip("/"))
            if not (root / p).exists():
                rep.error(f"{rel}:{lineno}", "paths", f"`{p}` does not exist",
                          "update the path to where the code lives now, or drop the reference if it was removed")
    return mentioned


def check_front_matter(root: Path, path: Path, text: str, rep: Report, today: dt.date, max_age: int,
                       git_ok: bool) -> None:
    rel = path.relative_to(root).as_posix()
    meta = parse_front_matter(text)
    if meta is None:
        rep.error(f"{rel}:1", "front-matter", "missing front matter",
                  "start the file with ---/owner/last_reviewed/code_paths/--- (copy it from ARCHITECTURE.md)")
        return
    for key in ("owner", "last_reviewed", "code_paths"):
        if key not in meta:
            rep.error(f"{rel}:1", "front-matter", f"front matter has no '{key}'", f"add '{key}:' to the front matter")
    paths = meta.get("code_paths") or []
    if "code_paths" in meta and not isinstance(paths, list):
        rep.error(f"{rel}:1", "front-matter", "code_paths must be a [list]", "write code_paths: [path/a, path/b]")
        paths = []
    for p in paths:
        if not (root / p).exists():
            rep.error(f"{rel}:1", "front-matter", f"code_paths entry {p!r} does not exist",
                      "update it to the current location or remove it, then re-read the doc for stale content")
    if "/exec-plans/active/" in f"/{rel}" or "/exec-plans/completed/" in f"/{rel}":
        want = "active" if "/active/" in rel else "completed"
        if meta.get("status") != want:
            rep.error(f"{rel}:1", "front-matter", f"status is {meta.get('status')!r} but the plan is in {want}/",
                      f"set status: {want}, or git mv the plan to docs/exec-plans/{meta.get('status')}/")
    raw = meta.get("last_reviewed")
    if not raw:
        return
    try:
        reviewed = dt.date.fromisoformat(raw)
    except ValueError:
        rep.error(f"{rel}:1", "freshness", f"last_reviewed {raw!r} is not a YYYY-MM-DD date",
                  "write the date you last checked the doc against the code, e.g. last_reviewed: 2026-11-26")
        return
    if reviewed > today:
        rep.error(f"{rel}:1", "freshness", f"last_reviewed {raw} is in the future", "use today's date or earlier")
    elif (today - reviewed).days > max_age:
        rep.error(f"{rel}:1", "freshness", f"last reviewed {(today - reviewed).days} days ago (> {max_age})",
                  "re-read the doc against the code paths it lists, fix what is stale, then bump last_reviewed")
    if git_ok and paths:
        changed = subprocess.run(["git", "log", "-1", "--format=%cs", f"--since={reviewed + dt.timedelta(days=1)}",
                                  "--", *paths], cwd=root, capture_output=True, text=True).stdout.strip()
        if changed:
            rep.warn(f"{rel}:1", "freshness", f"code under code_paths changed on {changed}, after last_reviewed {raw}",
                     "re-read the doc against those changes and bump last_reviewed (or fix the doc)")


def check_agents_map(root: Path, rep: Report, mentioned: set[str]) -> None:
    agents = root / "AGENTS.md"
    if not agents.exists():
        rep.error("AGENTS.md:1", "agents-map", "AGENTS.md is missing", "restore AGENTS.md (it is the map for agents)")
        return
    n = len(agents.read_text(encoding="utf-8").splitlines())
    if n > AGENTS_MAX_LINES:
        rep.error("AGENTS.md:1", "agents-map", f"{n} lines (> {AGENTS_MAX_LINES})",
                  "AGENTS.md is a table of contents: move details into docs/ and link them")
    for doc in REQUIRED_IN_AGENTS:
        if not (root / doc).exists():
            rep.error("AGENTS.md:1", "agents-map", f"required doc {doc} does not exist", f"create {doc} or drop it from REQUIRED_IN_AGENTS")
        elif doc not in mentioned:
            rep.error("AGENTS.md:1", "agents-map", f"AGENTS.md does not link {doc}",
                      f"add a row that links [{Path(doc).name}]({doc}) to the 'where to read' table")


def run(root: Path = ROOT, max_age: int = 180, today: dt.date | None = None) -> Report:
    rep = Report()
    today = today or dt.date.today()
    git_ok = (root / ".git").exists()
    files = tracked_markdown(root)
    agents_mentions: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        mentioned = check_links_and_paths(root, path, text, rep)
        if rel == "AGENTS.md":
            agents_mentions = mentioned
        if matches(rel, MANAGED_GLOBS) and rel not in TEMPLATES:
            check_front_matter(root, path, text, rep, today, max_age, git_ok)
    check_agents_map(root, rep, agents_mentions)
    rep.files = len(files)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-age-days", type=int, default=180)
    ap.add_argument("--strict", action="store_true", help="fail on warnings too")
    args = ap.parse_args(argv)
    rep = run(max_age=args.max_age_days)
    for e in rep.errors:
        print("ERROR", e)
    for w in rep.warnings:
        print("WARN ", w)
    failed = bool(rep.errors) or (args.strict and bool(rep.warnings))
    print(f"\ndoc check {'FAILED' if failed else 'passed'}: {rep.files} markdown files, "
          f"{len(rep.errors)} error(s), {len(rep.warnings)} warning(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
