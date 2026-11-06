"""Lock the package dependency direction (the ``DEPS.list`` idea).

SomnusUI documents the direction ``providers -> compiler -> projection ->
serialization -> protocol``, with ``runtime`` on top of the core layers, but
nothing enforced it. This module parses every ``somnus/**/*.py`` with ``ast``
and refuses an import that crosses a layer boundary the design does not allow.

Two details matter and are covered here:

* a deferred import (``import`` inside a function body) is still a dependency,
  so it is checked exactly like a module-level one;
* imports under ``if TYPE_CHECKING:`` are annotations only and are not checked.

The allowlist is the dependency graph **as it exists at the time the check was
installed**, not an idealised one. Each entry that is broader than the ideal
matrix has a comment saying why. Removing an exception is a code change, not a
test change.

Known gap: a module name built at runtime (``importlib.import_module``) cannot
be seen statically and is not checked.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOMNUS = ROOT / "somnus"

#: Source package -> allowed ``somnus`` import prefixes.
#:
#: ``""`` is ``somnus/__init__.py``. ``cli`` is the entry point and may reach
#: everything. The rest are the documented layers. Real, current edges that are
#: wider than the ideal direction are called out inline.
ALLOWED_PREFIXES: dict[str, tuple[str, ...]] = {
    "": ("somnus",),  # package root: re-exports only
    "cli": ("somnus",),  # command-line entry point
    "config": ("somnus.config", "somnus.errors"),
    "errors": ("somnus.errors",),
    "ir": ("somnus.ir",),  # value objects: no compiler/providers/runtime
    "sui": ("somnus.ir", "somnus.sui", "somnus.errors"),
    "compiler": (
        "somnus.ir",
        "somnus.compiler",
        # deferred built-in pack registration in pack_schema._load_builtin();
        # the packs themselves import compiler.pack_schema, so this edge is the
        # deliberate registry hook, not a compiler dependency on pack content.
        "somnus.packs",
    ),
    "packs": ("somnus.ir", "somnus.compiler", "somnus.packs"),
    "projection": (
        "somnus.ir",
        "somnus.compiler",
        "somnus.projection",
        # deferred render_line() calls in engine.py inside budget/expand paths
        "somnus.sui",
    ),
    "providers": ("somnus.ir", "somnus.providers"),
    # Trace storage: a leaf package (stdlib only). It must stay independent of
    # the runtime so a sink can be reused by tests, CLI and MCP alike.
    "observability": ("somnus.observability",),
    "runtime": (
        "somnus.ir",
        "somnus.compiler",
        "somnus.projection",
        "somnus.sui",
        "somnus.errors",
        # runtime reads persistent settings through this deferred import
        "somnus.config",
        # runtime may use the provider protocol but never a concrete provider
        "somnus.providers.base",
        "somnus.session",
        "somnus.runtime",
        # trace events go through the sink protocol / implementations
        "somnus.observability",
    ),
    "protocol": (
        "somnus.ir",
        "somnus.sui",
        "somnus.runtime",
        "somnus.providers.base",
        "somnus.errors",
        "somnus.config",
        "somnus.protocol",
        # SOMNUS_TRACE is attached when the MCP server builds its session
        "somnus.observability",
    ),
    "session": (
        "somnus.ir",
        "somnus.providers",
        "somnus.runtime",
        "somnus.protocol",
        "somnus.errors",
        "somnus.config",
        "somnus.session",
    ),
    # Self-check commands are host code, invoked from the CLI only. They drive
    # a normal Session and read traces; nothing in the runtime imports them.
    "selfcheck": (
        "somnus.ir",
        "somnus.selfcheck",
        "somnus.runtime",
        "somnus.session",
        "somnus.observability",
        "somnus.errors",
        "somnus.config",
        # ``doctor`` imports the provider module only to answer "is the Windows
        # UIA stack importable?" - a diagnostic, never an execution path.
        "somnus.providers.windows",
    ),
}


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _walk(node: ast.AST):
    """Walk the tree, skipping ``if TYPE_CHECKING:`` bodies."""
    if isinstance(node, ast.If) and _is_type_checking(node.test):
        for child in node.orelse:
            yield from _walk(child)
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _walk(child)


def _module_name(path: Path) -> str:
    rel = path.relative_to(SOMNUS).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return "somnus" + ("" if not parts else "." + ".".join(parts))


def _source_package(module_name: str) -> str:
    parts = module_name.split(".")
    return parts[1] if len(parts) > 1 else ""


def _is_package(module_name: str, path: Path | None) -> bool:
    return path is not None and path.name == "__init__.py"


def _import_targets(
    node: ast.Import | ast.ImportFrom,
    module_name: str,
    *,
    is_package: bool,
) -> list[str]:
    targets: list[str] = []
    if isinstance(node, ast.Import):
        targets = [alias.name for alias in node.names]
    else:
        if node.level:
            package = module_name if is_package else module_name.rpartition(".")[0]
            parts = package.split(".")
            parts = parts[: len(parts) - (node.level - 1)]
            base = ".".join(parts)
            module = f"{base}.{node.module}" if node.module else base
        else:
            module = node.module or ""
        # ``from somnus import ir`` names a submodule, not an attribute. The
        # bare package is not the dependency; the named submodule is.
        if module == "somnus":
            targets.extend(f"somnus.{alias.name}" for alias in node.names)
        elif module:
            targets.append(module)
    return [target for target in targets if target.startswith("somnus")]


def _allowed(module_name: str, target: str, source: str) -> bool:
    for prefix in ALLOWED_PREFIXES[source]:
        if target == prefix or target.startswith(prefix + "."):
            return True
    return False


def _violations(
    module_name: str,
    source: str,
    *,
    is_package: bool = False,
    path: Path | None = None,
) -> list[tuple[str, str]]:
    """Return ``(imported module, source module)`` pairs that cross a boundary."""
    package = _source_package(module_name)
    if package not in ALLOWED_PREFIXES:
        raise AssertionError(
            f"{module_name} belongs to package {package!r}, which is missing from "
            "ALLOWED_PREFIXES"
        )
    tree = ast.parse(source, filename=str(path or module_name))
    out: list[tuple[str, str]] = []
    for node in _walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for target in _import_targets(
            node, module_name, is_package=is_package or _is_package(module_name, path)
        ):
            if not _allowed(module_name, target, package):
                out.append((target, module_name))
    return out


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_every_package_has_an_entry():
    """A new top-level package must be classified before it can import anything."""
    names = set()
    for path in SOMNUS.iterdir():
        if path.name == "__init__.py":
            names.add("")
        elif path.is_dir():
            if not path.name.startswith("__") and not path.name.startswith("."):
                names.add(path.name)
        elif path.suffix == ".py":
            names.add(path.stem)
    missing = sorted(names - set(ALLOWED_PREFIXES))
    assert not missing, f"classify these packages in ALLOWED_PREFIXES: {missing}"


def test_the_real_tree_stays_inside_its_boundaries():
    violations: list[str] = []
    for path in sorted(SOMNUS.rglob("*.py")):
        module = _module_name(path)
        source = path.read_text(encoding="utf-8")
        for target, importer in _violations(
            module, source, path=path, is_package=path.name == "__init__.py"
        ):
            violations.append(f"{importer} imports {target}")
    assert not violations, "\n".join(violations)


def test_a_deliberate_cross_layer_import_is_reported():
    """The check must fail on the bug it exists for, not only pass on today's tree."""
    source = "from somnus.protocol import tools\n"
    assert _violations("somnus.ir.node", source)


def test_a_deferred_cross_layer_import_is_reported():
    source = "def load():\n    from somnus.runtime.session import Session\n"
    assert _violations("somnus.ir.node", source)


def test_type_checking_imports_are_annotations_only():
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from somnus.protocol import tools\n"
    )
    assert _violations("somnus.ir.node", source) == []


def test_an_allowed_import_is_not_reported():
    source = "from somnus.ir.node import SemanticNode\n"
    assert _violations("somnus.compiler.compile", source) == []


def test_relative_imports_resolve_before_they_are_checked():
    source = "from .node import SemanticNode\n"
    assert _violations("somnus.compiler.compile", source) == []
