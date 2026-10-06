"""可选业务模块：核心之外、只有部分产线用得上的功能。

核心只做通用编排（能力、工位与接入、设备方法、流程、方案、批次、执行）；某类产线专用的生成器、导入器放在这里，
一个模块一个子包，自带表定义、规则、服务、路由与接口模型。依赖只能单向：模块可以用核心，核心不引用任何模块
（`tests/domain/test_module_boundary.py` 守着）。

`ILCS_MODULES` 列出启用哪些（逗号分隔，缺省全部启用，写空串全部停用）。停用只是不挂路由、界面不出菜单：
表照旧由迁移建好、数据留着，重新启用即可接着用。
"""
from __future__ import annotations

from importlib import import_module

from fastapi import APIRouter

# 模块名 → 子包。新增模块在这里登记
AVAILABLE = {
    "formulation": "app.modules.formulation",
}


def resolve(names: list[str]) -> list[str]:
    """校验启用清单：写错名字直接拒绝启动，不悄悄当成没启用。"""
    unknown = [name for name in names if name not in AVAILABLE]
    if unknown:
        raise ValueError(f"ILCS_MODULES 里有未知模块：{'、'.join(unknown)}（可选：{'、'.join(AVAILABLE)}）")
    return names


def load_models() -> None:
    """把所有模块的表注册进 metadata（不论是否启用）：迁移与库结构不随开关变。"""
    for package in AVAILABLE.values():
        import_module(f"{package}.models")


def routers(names: list[str]) -> list[APIRouter]:
    """启用的模块要挂的路由：模块导出 `routers`（界面用的与服务身份用的 /runtime 入口分开）就挂它们，否则挂 `router`。"""
    out: list[APIRouter] = []
    for name in resolve(names):
        module = import_module(AVAILABLE[name])
        out.extend(getattr(module, "routers", None) or [module.router])
    return out
