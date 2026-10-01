"""网关配置（`--config` 指的那份 JSON，样例见 config.example.json）：连哪台 BTS、ILCS 能用哪些通道、有哪些工步文件。

通道白名单是安全边界：一台柜子常有人工在用，不在 `channels` 里的通道网关既不启动也不停止。
工步（充放电程序）在 BTS 里做好、存成工步文件，ILCS 的设备方法按 `programs` 的键选用，网关不改工步内容。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

PIPELINE = re.compile(r"^\d+-\d+-\d+$")


@dataclass(frozen=True)
class Program:
    name: str
    file: str


@dataclass(frozen=True)
class Config:
    device_id: str
    channels: tuple[str, ...]
    programs: dict[str, Program]
    model: str = ""
    vendor: str = "Neware"
    host: str = "127.0.0.1"
    port: int = 502
    timeout_sec: float = 10.0
    # 指令里没带通道时自动挑一个空闲的白名单通道：只在白名单里全是验收用假电池 / 空位时打开
    auto_channel: bool = False
    default_program: str = ""
    data_dir: str = ""

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Config":
        problems = []
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            problems.append("缺 device_id（这台柜子在 ILCS 登记的设备编号）")
        channels = tuple(str(item).strip() for item in data.get("channels") or [])
        if not channels:
            problems.append("channels 是空的：至少列一个 ILCS 能用的通道（设备号-子设备号-通道号）")
        bad = [item for item in channels if not PIPELINE.fullmatch(item)]
        if bad:
            problems.append(f"通道号要写成 设备号-子设备号-通道号（如 21-1-3）：{', '.join(bad)}")
        if len(set(channels)) != len(channels):
            problems.append("channels 里有重复的通道")
        programs: dict[str, Program] = {}
        for code, item in (data.get("programs") or {}).items():
            if not isinstance(item, dict) or not item.get("file"):
                problems.append(f"工步 {code} 要写 file（BTS 工步文件的路径）")
                continue
            programs[str(code)] = Program(name=str(item.get("name") or code), file=str(item["file"]))
        if not programs:
            problems.append("programs 是空的：至少登记一个工步文件")
        default = str(data.get("default_program") or "")
        if default and default not in programs:
            problems.append(f"default_program {default} 不在 programs 里")
        bts = data.get("bts") or {}
        if problems:
            raise ValueError("网关配置有问题：" + "；".join(problems))
        return cls(
            device_id=device_id, channels=channels, programs=programs, model=str(data.get("model") or ""),
            vendor=str(data.get("vendor") or "Neware"), host=str(bts.get("host") or "127.0.0.1"),
            port=int(bts.get("port") or 502), timeout_sec=float(bts.get("timeout_sec") or 10),
            auto_channel=bool(data.get("auto_channel")), default_program=default,
            data_dir=str(data.get("data_dir") or ""),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        return cls.parse(json.loads(Path(path).read_text(encoding="utf-8")))
