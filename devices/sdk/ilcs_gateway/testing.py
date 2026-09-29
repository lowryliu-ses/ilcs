"""对一个网关跑 ILCS 的接入验收清单（设备模块自己的测试用）。

走的是 ILCS 真正接入时的那条路：`http_json_v1` 驱动 + `api/app/adapters/acceptance.py` 的检查清单 +
统一控制口的故障注入。要能找到 ILCS 仓库的 `api/`（设备模块放在仓库的 devices/modules/ 下时自动找到；
放在别处时设环境变量 ILCS_REPO 指向仓库根目录）。
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
from typing import Any


def _ilcs_api() -> Path:
    candidates = [Path(os.environ["ILCS_REPO"]) / "api"] if os.environ.get("ILCS_REPO") else []
    candidates += [parent / "api" for parent in Path(__file__).resolve().parents]
    for candidate in candidates:
        if (candidate / "app" / "adapters" / "acceptance.py").exists():
            return candidate
    raise RuntimeError("找不到 ILCS 仓库的 api/：设环境变量 ILCS_REPO 指向 ILCS 仓库根目录")


@contextmanager
def _ilcs_settings(credential_root: Path, state_root: Path):
    api = str(_ilcs_api())
    if api not in sys.path:
        sys.path.insert(0, api)
    from app.core.config import settings

    saved = settings.adapter_credential_root, settings.adapter_state_root, settings.adapter_allowed_hosts
    settings.adapter_credential_root, settings.adapter_state_root = str(credential_root), str(state_root)
    settings.adapter_allowed_hosts = ",".join([saved[2], "127.0.0.1", "localhost"])
    try:
        yield
    finally:
        settings.adapter_credential_root, settings.adapter_state_root, settings.adapter_allowed_hosts = saved


def acceptance(base_url: str, *, token_file: str | Path, ca_file: str | Path | None = None, capability: str,
               params: dict[str, Any], expected_device_id: str = "", physical: bool = True, faults: bool = True,
               timeout: float = 15.0, state_root: str | Path | None = None):
    """返回 ILCS 的验收报告（`app.adapters.acceptance.Report`）。`token_file`、`ca_file` 要在同一个目录里。"""
    token_file = Path(token_file)
    credential_root = token_file.parent
    state = Path(state_root) if state_root else credential_root / "ilcs-ledger"
    with _ilcs_settings(credential_root, state):
        from app.adapters.acceptance import AcceptanceRecord, SimulatorControlInjector, run_acceptance
        from app.adapters.base import CommandRequest
        from app.adapters.drivers.http_json import HttpJsonAdapter
        from app.adapters.registry import describe

        config = {
            "base_url": base_url, "request_timeout_sec": 5, "connect_timeout_sec": 2, "heartbeat_mode": "probe",
            "expected_device_id": expected_device_id, "allow_insecure_http": base_url.startswith("http://"),
            **({"ca_file": str(ca_file)} if ca_file else {}),
        }
        record = AcceptanceRecord(station_id="MODULE-TEST", driver="http_json_v1", protocol="HTTPS JSON",
                                  version="module", config=config, credential_ref=f"file://{token_file}")
        injector = SimulatorControlInjector({**config, "url": base_url}, credential_ref=record.credential_ref) \
            if faults else None
        template = CommandRequest(command_id="", station_id=record.station_id, capability=capability, params=params,
                                  type="dispatch", batch_id="ACCEPTANCE", step_index=0, step_id="acceptance")
        contract = {"protocol": record.protocol, "version": record.version, "supports_hold": True,
                    "supports_abort": True, "supports_query": True, "supports_dedup": True}
        return run_acceptance(record, lambda: HttpJsonAdapter(record), template, contract=contract,
                              describe=lambda instance: describe(instance, record), physical=physical,
                              injector=injector, poll_timeout=timeout, poll_interval=0.1)
