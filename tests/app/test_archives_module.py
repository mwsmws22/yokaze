import ast
from pathlib import Path


def _imports_lab(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
            "yokaze.lab"
        ):
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("yokaze.lab"):
                    return True
    return False


def test_archives_module_has_no_lab_imports() -> None:
    path = Path("src/yokaze/ui/archives.py")
    assert path.is_file()
    assert not _imports_lab(path)
