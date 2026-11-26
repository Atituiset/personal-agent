#!/usr/bin/env python3
"""Layer linter for the personal_agent package.

    python scripts/check_architecture.py            # exit 1 on any violation
    python scripts/check_architecture.py --explain  # also print the layer table

Rules (see ARCHITECTURE.md):
  1. Imports between personal_agent modules may only point *down* the layer stack.
     adapters and store are siblings and must not import each other.
  2. Third-party packages may only be imported by the layers listed in THIRD_PARTY.
  3. personal_agent must never import evals/, tests/ or scripts/.
Exceptions live in EXCEPTIONS and must reference an entry in docs/exec-plans/tech-debt-tracker.md.

Every error message says what to do instead, so an agent can fix it without reading this file.
Only the standard library is used.
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "personal_agent"

# Ordered bottom -> top. A module belongs to the first (longest) matching prefix.
LAYERS: dict[str, list[str]] = {
    "core": ["personal_agent.core"],
    "providers": ["personal_agent.config"],
    "adapters": ["personal_agent.infra"],
    "store": ["personal_agent.queue.broker", "personal_agent.queue.faults"],
    "service": ["personal_agent.tasks"],
    "runtime": ["personal_agent.queue.worker"],
    "entry": ["personal_agent.api", "personal_agent.cli", "personal_agent.__main__", "personal_agent"],
}

# layer -> layers it may import (besides itself)
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "providers": {"core"},
    "adapters": {"core", "providers"},
    "store": {"core", "providers"},
    "service": {"core", "providers", "adapters"},
    "runtime": {"core", "providers", "adapters", "store", "service"},
    "entry": {"core", "providers", "adapters", "store", "service", "runtime"},
}

# third-party top-level package -> layers allowed to import it
THIRD_PARTY: dict[str, set[str]] = {
    "httpx": {"adapters"},
    "agents": {"adapters"},
    "openai": {"adapters"},
    "psycopg": {"store", "entry"},
    "psycopg_pool": {"store"},
    "fastapi": {"entry"},
    "pydantic": {"entry"},
    "uvicorn": {"entry"},
}

FORBIDDEN_TOP = {"evals", "tests", "scripts", "conftest"}

# (importing module, imported top-level name or module) -> tech-debt id
EXCEPTIONS: dict[tuple[str, str], str] = {
    ("personal_agent.tasks.agent", "agents"): "TD-1",
}

STDLIB = set(sys.stdlib_module_names) | {"__future__"}


@dataclass
class Violation:
    path: str
    line: int
    rule: str
    message: str
    fix: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}\n    fix: {self.fix}"


def layer_of(module: str) -> str | None:
    best, best_len = None, -1
    for layer, prefixes in LAYERS.items():
        for p in prefixes:
            if (module == p or module.startswith(p + ".")) and len(p) > best_len:
                best, best_len = layer, len(p)
    return best


def module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def resolve(module: str, is_pkg: bool, node: ast.ImportFrom) -> list[str]:
    """Absolute module names an ImportFrom refers to (base + base.name for each alias)."""
    if node.level:
        base_parts = module.split(".") if is_pkg else module.split(".")[:-1]
        base_parts = base_parts[: len(base_parts) - (node.level - 1)]
        base = ".".join(base_parts + ([node.module] if node.module else []))
    else:
        base = node.module or ""
    return [base] + [f"{base}.{a.name}" for a in node.names]


def imports(path: Path):
    module = module_name(path)
    is_pkg = path.name == "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name, [a.name]
        elif isinstance(node, ast.ImportFrom):
            names = resolve(module, is_pkg, node)
            yield node.lineno, names[0], names


def check_file(path: Path) -> list[Violation]:
    module = module_name(path)
    src_layer = layer_of(module)
    rel = str(path.relative_to(ROOT))
    out: list[Violation] = []
    if src_layer is None:
        return [Violation(rel, 1, "unmapped-module", f"{module} is not assigned to a layer",
                          "add its module prefix to LAYERS in scripts/check_architecture.py and to the table in ARCHITECTURE.md")]
    for line, base, candidates in imports(path):
        top = base.split(".")[0]
        if top == PACKAGE:
            # most specific personal_agent module among the candidates that maps to a layer
            target = max((c for c in candidates if c.startswith(PACKAGE)), key=lambda c: len(c))
            tgt_layer = layer_of(target)
            # `from personal_agent.tasks import TASKS` -> candidate personal_agent.tasks.TASKS maps to service: fine
            if tgt_layer is None or tgt_layer == src_layer:
                continue
            if tgt_layer not in ALLOWED[src_layer]:
                if (module, base) in EXCEPTIONS:
                    continue
                allowed = ", ".join(sorted(ALLOWED[src_layer])) or "nothing outside its own layer"
                if {src_layer, tgt_layer} == {"adapters", "store"}:
                    fix = ("adapters and store are siblings: pass what you need in through core "
                           "(a port in personal_agent/core/executor.py or JobContext) instead of importing across")
                elif src_layer == "service" and tgt_layer == "store":
                    fix = ("task handlers must not touch the broker directly; use JobContext "
                           "(ctx.step / ctx.effect / ctx.complete in personal_agent/core/context.py) so fencing and "
                           "effect de-duplication still apply")
                else:
                    fix = (f"move the shared code down into a layer {src_layer} may use ({allowed}), "
                           f"or invert the dependency (define a Protocol in personal_agent/core and inject it from {tgt_layer})")
                out.append(Violation(rel, line, "layer-direction",
                                     f"{src_layer} module {module} imports {tgt_layer} module {target}; "
                                     f"{src_layer} may only import: {allowed}", fix))
        elif top in FORBIDDEN_TOP:
            out.append(Violation(rel, line, "no-test-imports", f"{module} imports {base}",
                                 "runtime code must not depend on evals/, tests/ or scripts/; move the shared helper into "
                                 "personal_agent (lowest layer that needs it) and import it from both places"))
        elif top in STDLIB:
            continue
        else:
            layers = THIRD_PARTY.get(top)
            if layers is None:
                out.append(Violation(rel, line, "unknown-dependency", f"{module} imports third-party package {top!r}, "
                                     "which has no layer assignment",
                                     f"decide which layer owns {top!r}, add it to THIRD_PARTY in scripts/check_architecture.py "
                                     "and to the table in ARCHITECTURE.md (and to pyproject.toml dependencies)"))
            elif src_layer not in layers and (module, top) not in EXCEPTIONS:
                owners = ", ".join(sorted(layers))
                out.append(Violation(rel, line, "third-party-layer",
                                     f"{src_layer} module {module} imports {top!r}, which only {owners} may import",
                                     f"wrap the {top!r} call in a {owners} module and call that wrapper instead; "
                                     "if this is a deliberate exception, add it to EXCEPTIONS with a tech-debt id "
                                     "and record it in docs/exec-plans/tech-debt-tracker.md"))
    return out


def check_exceptions() -> list[Violation]:
    tracker = (ROOT / "docs/exec-plans/tech-debt-tracker.md")
    text = tracker.read_text(encoding="utf-8") if tracker.exists() else ""
    out = []
    for (mod, name), td in EXCEPTIONS.items():
        if f"| {td} |" not in text:
            out.append(Violation("scripts/check_architecture.py", 1, "exception-untracked",
                                 f"exception ({mod} -> {name}) references {td}, which is not in the tech-debt tracker",
                                 f"add a row '| {td} | ...' to docs/exec-plans/tech-debt-tracker.md with the reason "
                                 "and exit criteria, or remove the exception"))
    return out


def run(root: Path = ROOT) -> list[Violation]:
    violations = []
    for path in sorted((root / PACKAGE).rglob("*.py")):
        violations += check_file(path)
    return violations + check_exceptions()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--explain", action="store_true", help="print the layer table")
    args = ap.parse_args(argv)
    if args.explain:
        for layer, prefixes in LAYERS.items():
            print(f"{layer:<10} {', '.join(prefixes)}  -> may import: {', '.join(sorted(ALLOWED[layer])) or '-'}")
    violations = run()
    for v in violations:
        print(v)
    files = len(list((ROOT / PACKAGE).rglob("*.py")))
    if violations:
        print(f"\narchitecture check FAILED: {len(violations)} violation(s) in {files} files (see ARCHITECTURE.md)")
        return 1
    print(f"architecture check passed: {files} files, {len(EXCEPTIONS)} tracked exception(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
