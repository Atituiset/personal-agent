#!/usr/bin/env python3
"""Scan for drift from the golden principles (docs/design-docs/golden-principles.md).

    python scripts/golden_scan.py                       # print all findings, exit 0
    python scripts/golden_scan.py --fail-on-new         # exit 1 if a finding is not in the baseline (CI)
    python scripts/golden_scan.py --markdown out.md --json out.json   # reports (weekly workflow)

Known, accepted drift lives in scripts/golden_baseline.json; every entry names a tech-debt id from
docs/exec-plans/tech-debt-tracker.md. Findings are matched to the baseline by (rule, path, code line),
so they survive unrelated edits that move lines around. Baseline entries that no longer match are
reported as "fixed" so the baseline can shrink.

Only the standard library is used. Each finding says how to fix it.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "golden_baseline.json"
SCAN_DIRS = ["personal_agent", "evals"]

ENV_ALLOWED = ("personal_agent/config.py", "personal_agent/obs.py", "personal_agent/infra/",
               "personal_agent/queue/faults.py")
PRINT_ALLOWED = ("personal_agent/cli.py",)
HASH_ALLOWED = ("personal_agent/core/storage.py",)
BROAD = {"Exception", "BaseException"}

FIX = {
    "GP1-validate-payload": "validate the payload at the top of the handler (or in a _validate helper it calls) and "
                            "raise core.errors.Fatal on bad input, so bad jobs go to the DLQ instead of retrying",
    "GP1-typed-body": "declare the request body as a pydantic BaseModel parameter instead of dict/Request",
    "GP2-config-via-providers": "read the setting in personal_agent/config.py (or the adapter that owns it) and pass "
                                "the value in; service/runtime code must not read os.environ directly",
    "GP3-no-print": "use personal_agent.obs.log_event(logger, 'event_name', **fields); only cli.py prints (its output)",
    "GP3-structured-log": "replace the formatted message with log_event(logger, 'event_name', key=value, ...)",
    "GP4-no-private-import": "the helper is shared, so make it public (drop the underscore) in the lowest layer "
                             "that needs it, or copy the two lines if it is not really shared",
    "GP4-shared-hashing": "use core.storage.digest (bytes) or core.context.fingerprint (JSON values) instead of hashlib",
    "GP5-no-silent-except": "re-raise, convert to a JobError, or log_event the failure; if dropping it is deliberate, "
                            "say why on the except line with `# noqa: BLE001 <reason>`",
    "GP6-timeout": "pass an explicit timeout= so a hung dependency cannot stall a worker forever",
}


@dataclass
class Finding:
    rule: str
    path: str
    line: int
    code: str
    message: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.rule, self.path, self.code)

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}\n    fix: {FIX[self.rule]}"


def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def _raises_fatal(fn: ast.AST, funcs: dict[str, ast.AST], depth: int = 0) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Raise) and node.exc is not None:
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if _name(target).split(".")[-1] == "Fatal":
                return True
        if depth < 2 and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in funcs \
                and funcs[node.func.id] is not fn and _raises_fatal(funcs[node.func.id], funcs, depth + 1):
            return True
    return False


def scan_file(path: Path, root: Path = ROOT) -> list[Finding]:
    rel = path.relative_to(root).as_posix()
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src, filename=rel)
    runtime = rel.startswith("personal_agent/")
    out: list[Finding] = []

    def add(rule, node, msg):
        code = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
        out.append(Finding(rule, rel, node.lineno, code, msg))

    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    for node in ast.walk(tree):
        # GP1: task handlers validate their payload
        if runtime and isinstance(node, ast.Call) and _name(node.func) == "TaskSpec":
            for kw in node.keywords:
                if kw.arg == "handler" and isinstance(kw.value, ast.Name) and kw.value.id in funcs:
                    if not _raises_fatal(funcs[kw.value.id], funcs):
                        add("GP1-validate-payload", node, f"task handler {kw.value.id} never raises Fatal on bad payload")
        # GP1: HTTP bodies are typed
        if runtime and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            routes = [d for d in node.decorator_list if isinstance(d, ast.Call)
                      and _name(d.func).split(".")[-1] in ("post", "put", "patch")]
            if routes:
                for arg in node.args.args:
                    ann = _name(arg.annotation) if arg.annotation is not None else ""
                    if ann in ("dict", "Request", "Any"):
                        add("GP1-typed-body", node, f"route {node.name} takes `{arg.arg}: {ann}`")
        # GP2: environment reads only in providers / adapters
        if runtime and not rel.startswith(ENV_ALLOWED):
            if isinstance(node, ast.Attribute) and _name(node) == "os.environ":
                add("GP2-config-via-providers", node, "reads os.environ outside providers/adapters")
            elif isinstance(node, ast.Call) and _name(node.func) == "os.getenv":
                add("GP2-config-via-providers", node, "calls os.getenv outside providers/adapters")
        # GP3: no print, no free-text log messages
        if runtime and isinstance(node, ast.Call):
            fname = _name(node.func)
            if fname == "print" and not rel.startswith(PRINT_ALLOWED):
                add("GP3-no-print", node, "print() in runtime code")
            if fname.split(".")[-1] in ("debug", "info", "warning", "error", "exception", "critical") \
                    and fname.split(".")[0] in ("log", "logger", "logging") and node.args:
                first = node.args[0]
                if isinstance(first, ast.JoinedStr) or (isinstance(first, ast.BinOp) and isinstance(first.op, (ast.Mod, ast.Add))) \
                        or (isinstance(first, ast.Call) and _name(first.func).endswith("format")):
                    add("GP3-structured-log", node, f"{fname}() with a formatted free-text message")
        # GP4: shared helpers are public; one hashing helper
        if isinstance(node, ast.ImportFrom):
            private = [a.name for a in node.names if a.name.startswith("_") and not a.name.startswith("__")]
            if private:
                add("GP4-no-private-import", node, f"imports private name(s) {private} from {'.' * node.level}{node.module or ''}")
        if runtime and isinstance(node, (ast.Import, ast.ImportFrom)) and not rel.startswith(HASH_ALLOWED):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if "hashlib" in mods:
                add("GP4-shared-hashing", node, "imports hashlib directly")
        # GP5: broad excepts must not swallow silently
        if isinstance(node, ast.ExceptHandler):
            broad = node.type is None or _name(node.type) in BROAD or (
                isinstance(node.type, ast.Tuple) and any(_name(e) in BROAD for e in node.type.elts))
            if broad:
                acts = any(isinstance(n, (ast.Raise, ast.Call)) for b in node.body for n in ast.walk(b))
                waived = "noqa: BLE001" in lines[node.lineno - 1]
                if not acts and not waived:
                    add("GP5-no-silent-except", node, "broad except swallows the error without logging, converting or re-raising")
        # GP6: explicit timeouts on blocking external calls
        if isinstance(node, ast.Call):
            fname = _name(node.func)
            needs = fname in ("subprocess.run", "subprocess.check_output", "subprocess.check_call", "httpx.Client",
                              "httpx.AsyncClient") or (fname.startswith("httpx.") and fname.split(".")[-1]
                                                       in ("get", "post", "put", "patch", "delete", "request"))
            if needs and not any(kw.arg == "timeout" for kw in node.keywords):
                add("GP6-timeout", node, f"{fname}() without timeout=")
    return out


def scan(root: Path = ROOT) -> list[Finding]:
    findings = []
    for d in SCAN_DIRS:
        for path in sorted((root / d).rglob("*.py")):
            findings += scan_file(path, root)
    return sorted(findings, key=lambda f: (f.path, f.line, f.rule))


def load_baseline(path: Path = BASELINE) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["accepted"] if path.exists() else []


def classify(findings: list[Finding], baseline: list[dict]):
    keys = {(b["rule"], b["path"], b["code"]): b for b in baseline}
    new = [f for f in findings if f.key not in keys]
    known = [f for f in findings if f.key in keys]
    found = {f.key for f in findings}
    fixed = [b for k, b in keys.items() if k not in found]
    return new, known, fixed


def to_markdown(new, known, fixed, baseline) -> str:
    td = {(b["rule"], b["path"], b["code"]): b.get("tracker") for b in baseline}
    lines = ["# Golden principles drift report", "",
             "Rules: `docs/design-docs/golden-principles.md` · generated by `scripts/golden_scan.py` · "
             "known drift is listed in `scripts/golden_baseline.json` and `docs/exec-plans/tech-debt-tracker.md`. "
             "Nothing here is changed or merged automatically.", "",
             f"- new drift (not in baseline): **{len(new)}**",
             f"- known drift (baselined, tracked in tech-debt-tracker): {len(known)}",
             f"- baseline entries that no longer occur (remove them): {len(fixed)}", ""]
    if new:
        lines += ["## New drift", "", "| rule | where | finding | fix |", "| --- | --- | --- | --- |"]
        lines += [f"| {f.rule} | `{f.path}:{f.line}` | {f.message} | {FIX[f.rule]} |" for f in new]
        lines.append("")
    if known:
        lines += ["## Known drift", "", "| rule | where | tracker |", "| --- | --- | --- |"]
        lines += [f"| {f.rule} | `{f.path}:{f.line}` | {td.get(f.key)} |" for f in known]
        lines.append("")
    if fixed:
        lines += ["## Fixed (shrink the baseline)", ""]
        lines += [f"- {b['rule']} `{b['path']}`: `{b['code']}`" for b in fixed]
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fail-on-new", action="store_true")
    ap.add_argument("--baseline", default=str(BASELINE))
    ap.add_argument("--markdown")
    ap.add_argument("--json")
    ap.add_argument("--write-baseline", metavar="TRACKER_ID",
                    help="accept all current findings into the baseline under this tech-debt id (review the diff!)")
    a = ap.parse_args(argv)
    findings = scan()
    baseline = load_baseline(Path(a.baseline))
    if a.write_baseline:
        keep = {(b["rule"], b["path"], b["code"]): b for b in baseline}
        for f in findings:
            keep.setdefault(f.key, {"rule": f.rule, "path": f.path, "code": f.code, "tracker": a.write_baseline})
        Path(a.baseline).write_text(json.dumps({"accepted": sorted(keep.values(), key=lambda b: (b["path"], b["rule"]))},
                                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        baseline = load_baseline(Path(a.baseline))
    new, known, fixed = classify(findings, baseline)
    tracker = ROOT / "docs" / "exec-plans" / "tech-debt-tracker.md"
    tracker_text = tracker.read_text(encoding="utf-8") if tracker.exists() else ""
    untracked = sorted({b.get("tracker") or "?" for b in baseline if f"| {b.get('tracker')} |" not in tracker_text})
    for td in untracked:
        print(f"ERROR baseline references {td}, which is not in docs/exec-plans/tech-debt-tracker.md\n"
              f"    fix: add a '| {td} | ...' row with the reason and exit criteria, or drop those baseline entries")
    for f in new:
        print("NEW  ", f.render())
    for f in known:
        print(f"known {f.path}:{f.line}: [{f.rule}] {f.message}")
    for b in fixed:
        print(f"fixed {b['path']}: [{b['rule']}] no longer occurs; remove it from {Path(a.baseline).name}")
    if a.markdown:
        Path(a.markdown).write_text(to_markdown(new, known, fixed, baseline), encoding="utf-8")
    if a.json:
        Path(a.json).write_text(json.dumps({"new": [asdict(f) for f in new], "known": [asdict(f) for f in known],
                                            "fixed": fixed}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\ngolden scan: {len(new)} new, {len(known)} known, {len(fixed)} fixed baseline entries")
    return 1 if (a.fail_on_new and (new or untracked)) else 0


if __name__ == "__main__":
    sys.exit(main())
