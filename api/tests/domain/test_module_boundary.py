"""可选模块（app/modules）：依赖只能单向，模块可以用核心，核心不引用模块；只有组装根 main.py 挂载它们。"""
import ast
from pathlib import Path

import pytest

from app import modules
from app.core.config import Settings

APP = Path(__file__).resolve().parents[2] / "app"
WIRING = {APP / "main.py"}


def _targets(path: Path, node: ast.AST) -> list[str]:
    """一条 import 引用到的完整模块路径（相对导入按文件位置展开）。"""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level:
        base = path.parent
        for _ in range(node.level - 1):
            base = base.parent
        target = ".".join([*base.relative_to(APP.parent).parts, *([node.module] if node.module else [])])
    else:
        target = node.module or ""
    return [target, *(f"{target}.{alias.name}" for alias in node.names)]


def test_core_does_not_import_modules():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if APP / "modules" in path.parents or path in WIRING:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if any(name == "app.modules" or name.startswith("app.modules.") for name in _targets(path, node)):
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert offenders == [], f"核心引用了可选模块：{offenders}"


def test_module_list_parsing():
    assert Settings(modules=" formulation , ").module_list == ["formulation"]
    assert Settings(modules="").module_list == []


def test_disabled_modules_mount_nothing():
    assert modules.routers([]) == []
    assert [router.prefix for router in modules.routers(["formulation"])] == [
        "/formulation-templates", "/runtime/formulation-templates",
    ]


def test_unknown_module_rejected():
    with pytest.raises(ValueError, match="未知模块"):
        modules.routers(["formulaton"])
