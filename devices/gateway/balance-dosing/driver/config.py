"""网关配置（`--config` 指的那份 JSON，样例见 config.example.json）：一台天平，加上可选的加粉、加液装置。

    {
      "device_id": "BAL-PWD-01", "model": "XPR206DRQ", "vendor": "Mettler Toledo",
      "balance": {"kind": "tcp", "host": "192.168.1.20", "port": 8001},
      "capabilities": {"weigh": "cap.weigh", "dose_solid": "cap.ely.dose_solid", "dose_liquid": "cap.ely.dose_liquid"},
      "solid": {"kind": "quantos", "tolerance_pct": 2, "substances": {"LiPF6": "LiPF6"}},
      "liquid": {"kind": "cavro", "link": {"kind": "serial", "port": "COM4", "baudrate": 9600},
                 "syringe_ul": 5000, "steps": 3000, "output_port": 12, "tolerance_g": 0.01,
                 "materials": {"EMC": {"port": 2, "density_g_ml": 1.01}}},
      "acceptance_material": {"dose_solid": "测试粉", "dose_liquid": "DMC"}
    }

- `capabilities`：这台站提供哪几项 ILCS 能力（键是动作，值是 ILCS 的能力 id）；没配装置的动作不提供。
- `solid.substances`：ILCS 物料名 → 加样头 RFID 里登记的物质名。加样头上的物质对不上就拒绝，不加错料。
- `liquid.materials`：ILCS 物料名 → 分配阀端口与密度（g/mL，按它把目标质量换成体积，最后由天平定量）。
- `acceptance_material`：ILCS 接入验收的指令不带物料，用这里指定的料核对（真机上放一种验收用的料）。
- `programs`：设备方法的「程序」→ `{"action": 动作, "name": 说明, 以及这一程序自己的 tolerance_g / tolerance_pct /
  first_fraction / max_rounds}`。ILCS 只把步骤排到自报了这个程序的站上，所以要和设备方法里写的程序名一致；
  不写就用 WEIGH / DOSE-POWDER / DOSE-LIQUID。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

ACTIONS = ("weigh", "dose_solid", "dose_liquid")


@dataclass(frozen=True)
class Liquid:
    port: int
    density: float


@dataclass(frozen=True)
class Config:
    device_id: str
    balance: dict[str, Any]
    capabilities: dict[str, str]
    model: str = ""
    vendor: str = "Mettler Toledo"
    mass_param: str = "mass"
    solid: dict[str, Any] = field(default_factory=dict)
    liquid: dict[str, Any] = field(default_factory=dict)
    liquids: dict[str, Liquid] = field(default_factory=dict)
    acceptance_material: dict[str, str] = field(default_factory=dict)
    programs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 天平稳定读数最多等多久（秒）
    stable_timeout_sec: float = 30.0

    def action_of(self, capability: str) -> str | None:
        return next((action for action, cap in self.capabilities.items() if cap == capability), None)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Config":
        problems: list[str] = []
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            problems.append("缺 device_id（这台站在 ILCS 登记的设备编号）")
        balance = data.get("balance") or {}
        if not isinstance(balance, dict) or balance.get("kind") not in {"tcp", "serial"}:
            problems.append("balance 要写天平的链路：{\"kind\": \"tcp\", \"host\", \"port\"} 或 {\"kind\": \"serial\", \"port\"}")
        capabilities = {str(k): str(v) for k, v in (data.get("capabilities") or {}).items() if v}
        unknown = sorted(set(capabilities) - set(ACTIONS))
        if unknown:
            problems.append(f"capabilities 只认 {', '.join(ACTIONS)}，不认 {', '.join(unknown)}")
        if not capabilities:
            problems.append("capabilities 是空的：至少提供一项（weigh / dose_solid / dose_liquid）")
        if len(set(capabilities.values())) != len(capabilities):
            problems.append("capabilities 里两个动作用了同一个 ILCS 能力 id")
        solid = data.get("solid") or {}
        if "dose_solid" in capabilities and solid.get("kind") != "quantos":
            problems.append("提供 dose_solid 要配 solid：{\"kind\": \"quantos\", ...}（梅特勒 Quantos 自动加粉）")
        liquid = data.get("liquid") or {}
        liquids: dict[str, Liquid] = {}
        if "dose_liquid" in capabilities:
            if liquid.get("kind") != "cavro":
                problems.append("提供 dose_liquid 要配 liquid：{\"kind\": \"cavro\", ...}（Cavro 协议注射泵 + 分配阀）")
            for key in ("syringe_ul", "steps", "output_port"):
                if not isinstance(liquid.get(key), (int, float)) or liquid.get(key) <= 0:
                    problems.append(f"liquid.{key} 要写正数")
            ports: dict[int, str] = {}
            for name, item in (liquid.get("materials") or {}).items():
                port, density = (item or {}).get("port"), (item or {}).get("density_g_ml")
                if not isinstance(port, int) or port <= 0 or not isinstance(density, (int, float)) or density <= 0:
                    problems.append(f"liquid.materials.{name} 要写端口 port（正整数）和密度 density_g_ml（g/mL）")
                    continue
                if port == liquid.get("output_port"):
                    problems.append(f"{name} 的端口 {port} 就是出液口 output_port")
                if port in ports:
                    problems.append(f"{ports[port]} 和 {name} 用了同一个端口 {port}")
                ports[port] = name
                liquids[str(name)] = Liquid(port=port, density=float(density))
            if not liquids:
                problems.append("liquid.materials 是空的：至少登记一种液体（端口与密度）")
        programs: dict[str, dict[str, Any]] = {}
        for code, spec in (data.get("programs") or {}).items():
            action = (spec or {}).get("action")
            if action not in capabilities:
                problems.append(f"程序 {code} 的 action 要是这台站提供的动作（{', '.join(capabilities)}），不是 {action!r}")
                continue
            programs[str(code)] = dict(spec)
        acceptance = {str(k): str(v) for k, v in (data.get("acceptance_material") or {}).items() if v}
        if acceptance.get("dose_liquid") and acceptance["dose_liquid"] not in liquids and "dose_liquid" in capabilities:
            problems.append(f"acceptance_material.dose_liquid {acceptance['dose_liquid']} 不在 liquid.materials 里")
        if problems:
            raise ValueError("网关配置有问题：" + "；".join(problems))
        return cls(
            device_id=device_id, balance=dict(balance), capabilities=capabilities, model=str(data.get("model") or ""),
            vendor=str(data.get("vendor") or "Mettler Toledo"), mass_param=str(data.get("mass_param") or "mass"),
            solid=dict(solid), liquid=dict(liquid), liquids=liquids, acceptance_material=acceptance, programs=programs,
            stable_timeout_sec=float(data.get("stable_timeout_sec") or 30),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        return cls.parse(json.loads(Path(path).read_text(encoding="utf-8")))
