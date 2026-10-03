"""驱动宿主的运行配置：协议插件读到的那几项（运行环境、主机白名单、凭据目录、台账目录）。

插件从 ILCS 抽出来时只依赖这几项，属性名沿用 ILCS 的 `settings`，插件代码不用改。宿主启动时按现场配置
（`host.json`）调用 `configure()` 填好；插件在用到时才读，所以模块级的单例就够了。
"""
from __future__ import annotations

from dataclasses import dataclass

from .hosts import parse_allowlist


@dataclass
class HostSettings:
    environment: str = "development"  # development | test | production
    allowed_hosts: str = "127.0.0.1,localhost"
    credential_root: str = "/run/secrets/ilcs-host"
    state_dir: str = "/data/ilcs-host"

    def configure(self, *, environment: str, allowed_hosts: str, credential_root: str, state_dir: str) -> None:
        if environment not in {"development", "test", "production"}:
            raise ValueError(f"environment 只能是 development / test / production，收到 {environment!r}")
        if environment == "production":
            problems = parse_allowlist(allowed_hosts).production_problems()
            if problems:
                raise ValueError("正式环境的 allowed_hosts 不合格：" + "；".join(problems))
        self.environment = environment
        self.allowed_hosts = allowed_hosts
        self.credential_root = credential_root
        self.state_dir = state_dir

    # ---------- 插件沿用的 ILCS 属性名 ----------

    @property
    def adapter_credential_root(self) -> str:
        return self.credential_root

    @property
    def adapter_state_dir(self) -> str:
        return self.state_dir

    def adapter_host_allowed(self, host: str) -> bool:
        """设备主机在不在白名单里；`*` 只在非正式环境生效。"""
        return parse_allowlist(self.allowed_hosts).allows(host, wildcard=self.environment != "production")


settings = HostSettings()
