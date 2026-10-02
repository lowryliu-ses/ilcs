"""设备模块的测试不连 ILCS 数据库：只要仓库里的 api/（驱动、接入验收清单、模板检查）与
devices/simulators/common（统一控制口）。

模块放在 ILCS 仓库的 devices/gateway/ 下时自动找到；放在别处时设环境变量 ILCS_REPO 指向 ILCS 仓库根目录。
ILCS 的运行配置（作业台账目录、凭据目录、设备主机白名单）按用例指到临时目录与本机回环地址。
"""
import os
from pathlib import Path
import sys

import pytest

MODULE = Path(__file__).resolve().parents[1]
REPO = Path(os.environ["ILCS_REPO"]) if os.environ.get("ILCS_REPO") else MODULE.parents[2]
for path in (REPO / "api", REPO / "devices", MODULE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def ilcs_settings(tmp_path, monkeypatch):
    """驱动作业台账、凭据目录放临时目录；白名单只放本机回环地址；非正式环境（模拟设备才接得进来）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_state_root", str(tmp_path / "adapter-state"))
    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path / "secrets"))
    monkeypatch.setattr(settings, "adapter_allowed_hosts", "127.0.0.1,localhost")
    monkeypatch.setattr(settings, "environment", "development")
    return settings
