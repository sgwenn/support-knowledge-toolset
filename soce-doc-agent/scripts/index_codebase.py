#!/usr/bin/env python3
"""Generate code-index.json — a symbol map for token-efficient codebase navigation."""

import ast
import json
import os
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SKIP_DIRS = {"__pycache__", ".venv", "venv", "dist", "build", ".git"}


def first_docstring(node: ast.AST) -> str:
    """Return the first line of the docstring for a module/function/class, or ''."""
    if not (
        isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
    ):
        return ""
    first = node.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return first.value.value.strip().splitlines()[0]
    return ""


def index_file(path: Path, rel: str) -> dict:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return {"summary": "(parse error)", "functions": [], "classes": [], "imports": []}

    summary = first_docstring(tree) or rel

    functions = []
    classes = []
    imports = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "async": isinstance(node, ast.AsyncFunctionDef),
                "doc": first_docstring(node),
            })
        elif isinstance(node, ast.ClassDef):
            methods = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append({"name": child.name, "line": child.lineno})
            classes.append({"name": node.name, "line": node.lineno, "methods": methods})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    return {
        "summary": summary,
        "functions": functions,
        "classes": classes,
        "imports": sorted(set(imports)),
    }


def build_index() -> dict:
    files = {}
    symbols = {}

    for py_file in sorted(REPO_ROOT.rglob("*.py")):
        # Skip unwanted dirs
        if any(part in SKIP_DIRS for part in py_file.parts):
            continue
        rel = str(py_file.relative_to(REPO_ROOT))
        entry = index_file(py_file, rel)
        files[rel] = entry

        for fn in entry["functions"]:
            symbols[fn["name"]] = {"file": rel, "line": fn["line"], "kind": "function"}
        for cls in entry["classes"]:
            symbols[cls["name"]] = {"file": rel, "line": cls["line"], "kind": "class"}
            for method in cls["methods"]:
                # prefix with ClassName. to avoid collisions
                symbols[f"{cls['name']}.{method['name']}"] = {
                    "file": rel,
                    "line": method["line"],
                    "kind": "method",
                }

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "symbols": symbols,
    }


if __name__ == "__main__":
    index = build_index()
    out = REPO_ROOT / "code-index.json"
    out.write_text(json.dumps(index, indent=2), encoding="utf-8")
    n_files = len(index["files"])
    n_symbols = len(index["symbols"])
    print(f"Indexed {n_files} files, {n_symbols} symbols → {out}")
