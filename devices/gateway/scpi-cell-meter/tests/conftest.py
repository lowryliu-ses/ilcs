"""设备模块的测试不连 ILCS 数据库：要仓库里的驱动宿主（devices/host：line_command 插件）、devices/simulators/common
（统一控制口），以及 ILCS 的 api/（sila2_v1 驱动与接入验收清单——验收和现场一样经 SiLA 2 走驱动宿主）。

模块放在 ILCS 仓库的 devices/gateway/ 下时自动找到；放在别处时设环境变量 ILCS_REPO 指向 ILCS 仓库根目录。
驱动宿主与 ILCS 的运行配置（台账目录、凭据目录、设备主机白名单）按用例指到临时目录与本机回环地址。
"""
import os
from pathlib import Path
import sys

import pytest

MODULE = Path(__file__).resolve().parents[1]
REPO = Path(os.environ["ILCS_REPO"]) if os.environ.get("ILCS_REPO") else MODULE.parents[2]
for path in (REPO / "api", REPO / "devices", REPO / "devices" / "host", MODULE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def runtime_settings(tmp_path, monkeypatch):
    """插件的作业台账、凭据目录放临时目录；白名单只放本机回环地址；非正式环境（模拟设备才接得进来）。
    ILCS 那边（sila2_v1 驱动、验收的模拟设备控制口）同样指到临时目录与回环地址。"""
    from app.core.config import settings as ilcs
    from ilcs_host.settings import settings as host

    monkeypatch.setattr(host, "state_dir", str(tmp_path / "adapter-state"))
    monkeypatch.setattr(host, "credential_root", str(tmp_path / "secrets"))
    monkeypatch.setattr(host, "allowed_hosts", "127.0.0.1,localhost")
    monkeypatch.setattr(host, "environment", "development")
    monkeypatch.setattr(ilcs, "adapter_credential_root", str(tmp_path / "secrets"))
    monkeypatch.setattr(ilcs, "adapter_allowed_hosts", "127.0.0.1,localhost")
    monkeypatch.setattr(ilcs, "environment", "development")
    (tmp_path / "secrets").mkdir(exist_ok=True)
    return tmp_path
