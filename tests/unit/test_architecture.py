"""Architecture rules (AC-ENG-04, CLAUDE.md rules 1-2, NFR-10)."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

from tests.conftest import ROOT

SRC = ROOT / "src" / "simcore"
CORE = SRC / "core"
FORBIDDEN_EXTERNAL = ("omni", "isaacsim", "pxr")
#: Domain vocabulary that must never appear in core/ (BRD 9: "Domain-specific rule").
DOMAIN_WORDS = re.compile(
    r"\b(warehouse|pallets?|jacks?|aisles?|bays?|docks?|forklifts?|pickers?|factory|"
    r"packaging|fill[- ]finish)\b",
    re.IGNORECASE,
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_AC_ENG_04_import_linter_contracts_pass() -> None:
    lint = Path(sys.executable).parent / "lint-imports"
    out = subprocess.run([str(lint)], cwd=ROOT, capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stdout + out.stderr


def test_AC_ENG_04_core_has_no_domain_or_omniverse_imports() -> None:
    for path in CORE.rglob("*.py"):
        for name in _imports(path):
            top = name.split(".")[0]
            assert top != "domains", f"{path} imports {name}"
            assert top not in FORBIDDEN_EXTERNAL, f"{path} imports {name}"


def test_only_omniverse_adapter_imports_isaac() -> None:
    adapter = SRC / "adapters" / "omniverse"
    for root in (SRC, ROOT / "domains"):
        for path in root.rglob("*.py"):
            if adapter in path.parents:
                continue
            for name in _imports(path):
                assert name.split(".")[0] not in FORBIDDEN_EXTERNAL, f"{path} imports {name}"


def test_core_contains_no_domain_vocabulary() -> None:
    hits = []
    for path in CORE.rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if DOMAIN_WORDS.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, "domain concepts leaked into core/:\n" + "\n".join(hits)


def test_no_print_in_library_code() -> None:
    for root in (SRC, ROOT / "domains"):
        for path in root.rglob("*.py"):
            if path.parent.name == "cli":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
                    raise AssertionError(f"print() in library code: {path}:{node.lineno}")
