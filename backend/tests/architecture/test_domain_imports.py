import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "seokpan"
FORBIDDEN_ROOT_IMPORTS = {"fastapi", "redis", "sqlalchemy"}


def forbidden_imports(source_file: Path) -> set[str]:
    syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    imported_roots: set[str] = set()

    for node in ast.walk(syntax_tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", maxsplit=1)[0])

    return imported_roots & FORBIDDEN_ROOT_IMPORTS


def domain_source_files() -> list[Path]:
    return [
        path
        for path in SOURCE_ROOT.rglob("*.py")
        if "domain" in path.relative_to(SOURCE_ROOT).parts
    ]


def test_domain_packages_do_not_import_framework_or_provider_clients() -> None:
    violations = {
        str(path.relative_to(SOURCE_ROOT)): sorted(forbidden_imports(path))
        for path in domain_source_files()
        if forbidden_imports(path)
    }

    assert violations == {}


@pytest.mark.parametrize("module_name", sorted(FORBIDDEN_ROOT_IMPORTS))
def test_architecture_scanner_detects_forbidden_imports(
    tmp_path: Path,
    module_name: str,
) -> None:
    source_file = tmp_path / "rule.py"
    source_file.write_text(f"import {module_name}\n", encoding="utf-8")

    assert forbidden_imports(source_file) == {module_name}
