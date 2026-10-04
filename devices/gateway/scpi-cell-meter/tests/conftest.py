"""设备模块的测试不连 ILCS 数据库。插件直接测只要设备侧的驱动宿主（host/：line_command 插件）与 simulators/common（统一
控制口）；经驱动宿主跑 ILCS 接入验收清单的一致性测试还要 ILCS 仓库的 api/（sila2_v1 驱动与验收清单），找不到就跳过这几项
（`ilcs_gateway.testing`：设环境变量 ILCS_REPO 指向 ILCS 仓库根目录，或者设备项目放在 ILCS 仓库里 / 旁边）。

驱动宿主与 ILCS 的运行配置（台账目录、凭据目录、设备主机白名单）按用例指到临时目录与本机回环地址。
"""
from pathlib import Path
import sys

import pytest

MODULE = Path(__file__).resolve().parents[1]
DEVICES = MODULE.parents[1]  # 设备侧的根（ILCS 仓库里是 devices/）：simulators 包、host/、gateway/ 都在它下面
for path in (DEVICES, DEVICES / "host", DEVICES / "gateway", MODULE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def runtime_settings(tmp_path, monkeypatch):
    """插件的作业台账、凭据目录放临时目录；白名单只放本机回环地址；非正式环境（模拟设备才接得进来）。
    找得到 ILCS 时，ILCS 那边（sila2_v1 驱动、验收的模拟设备控制口）同样指到临时目录与回环地址。"""
    from ilcs_gateway.testing import find_ilcs_api
    from ilcs_host.settings import settings as host

    monkeypatch.setattr(host, "state_dir", str(tmp_path / "adapter-state"))
    monkeypatch.setattr(host, "credential_root", str(tmp_path / "secrets"))
    monkeypatch.setattr(host, "allowed_hosts", "127.0.0.1,localhost")
    monkeypatch.setattr(host, "environment", "development")
    (tmp_path / "secrets").mkdir(exist_ok=True)
    api = find_ilcs_api()
    if api is not None:
        if str(api) not in sys.path:
            sys.path.insert(0, str(api))
        from app.core.config import settings as ilcs

        monkeypatch.setattr(ilcs, "adapter_credential_root", str(tmp_path / "secrets"))
        monkeypatch.setattr(ilcs, "adapter_allowed_hosts", "127.0.0.1,localhost")
        monkeypatch.setattr(ilcs, "environment", "development")
    return tmp_path
