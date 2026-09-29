#!/usr/bin/env python
"""新建一个设备模块：从样板 devices/modules/sample-cycler 复制，替换名称、型号、能力、参数与程序。生成的测试开箱就能过。

    python scripts/new-device-module.py acme-vd80 --title "ACME 真空干燥箱" --model VD-80 --vendor ACME \\
        --capability cap.vacuum_dry --param temp=60:180 --param vacuum=0.1:5 --program VD-120=120℃干燥

生成之后：
1. `api/.venv/bin/pytest devices/modules/<名称>/tests` 先确认是绿的；
2. 按设备手册改 `driver/vendor_sdk.py`（厂家 SDK 的调用面）与 `driver/device.py`（状态映射、实测值），
   `simulator/fake_sdk.py` 跟着改成同一组方法——测试一直对着它跑；
3. 核对 `profile.json` 里的连接参数示例与验收参数，交付时连同测试记录一起给 ILCS 侧导入。
模块结构与交付要求见 devices/modules/README.md。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "devices" / "modules" / "sample-cycler"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "state", "secrets", ".pytest_cache")


def _param(text: str) -> tuple[str, float, float]:
    name, _, span = text.partition("=")
    low, _, high = span.partition(":")
    try:
        low_value, high_value = float(low), float(high)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"参数写成 名称=下限:上限，如 temp=60:180，不是 {text}") from exc
    if not re.fullmatch(r"[A-Za-z_]\w*", name) or low_value > high_value:
        raise argparse.ArgumentTypeError(f"参数 {text} 无效：名称只能是字母数字下划线，下限不能大于上限")
    return name, low_value, high_value


def _program(text: str) -> tuple[str, str]:
    code, _, label = text.partition("=")
    if not code.strip():
        raise argparse.ArgumentTypeError("程序写成 程序号=说明，如 VD-120=120℃干燥")
    return code.strip(), (label or code).strip()


def _replace(path: Path, pairs: list[tuple[str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if old not in text:
            raise SystemExit(f"样板 {path.relative_to(ROOT)} 里找不到 {old[:40]!r}：样板改过了，请同步修改本脚本")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="模块目录名：小写字母、数字、横线，如 acme-vd80")
    parser.add_argument("--title", required=True, help="设备名称，如「ACME 真空干燥箱」")
    parser.add_argument("--model", required=True, help="设备型号（与资产登记的型号一致）")
    parser.add_argument("--vendor", default="厂家", help="厂家")
    parser.add_argument("--capability", required=True, help="ILCS 能力，如 cap.vacuum_dry")
    parser.add_argument("--param", action="append", type=_param, required=True, help="参数=下限:上限，可写多个")
    parser.add_argument("--program", action="append", type=_program, help="设备端程序=说明，可写多个；缺省一个 DEFAULT")
    parser.add_argument("--output", default=str(ROOT / "devices" / "modules"), help="放在哪个目录下（缺省 devices/modules/）")
    args = parser.parse_args(argv)
    if not NAME.fullmatch(args.name):
        raise SystemExit("模块名只能是小写字母、数字、横线，2–41 位")
    target = Path(args.output) / args.name
    if target.exists():
        raise SystemExit(f"{target} 已存在")
    device_id = "SIM-" + re.sub(r"[^A-Z0-9]+", "-", args.name.upper()).strip("-")[:24]
    params = {name: (low, high) for name, low, high in args.param}
    programs = dict(args.program or [("DEFAULT", "设备缺省程序")])

    shutil.copytree(SAMPLE, target, ignore=IGNORE)
    _replace(target / "driver" / "device.py", [
        ('CAPABILITY = "cap.test"', f"CAPABILITY = {args.capability!r}"),
        ('PARAMETERS = {"rate": (0.01, 10.0), "vmax": (2.0, 5.0)}', f"PARAMETERS = {params!r}"),
        ('PROGRAMS = {"CC-CV": "恒流恒压循环", "GITT": "恒电流间歇滴定"}', f"PROGRAMS = {json.dumps(programs, ensure_ascii=False)}"),
        ('DEFAULT_PROGRAM = "CC-CV"', f"DEFAULT_PROGRAM = {next(iter(programs))!r}"),
    ])
    _replace(target / "driver" / "vendor_sdk.py", [
        ("样板是一台 8 通道充放电柜，厂家只给了 Windows SDK。", f"{args.title}（{args.vendor} {args.model}）。"),
    ])
    _replace(target / "simulator" / "fake_sdk.py", [
        ('PROGRAMS = {"CC-CV", "GITT"}', f"PROGRAMS = {set(programs)!r}"),
        ('serial: str = "SIM-CYC-M1"', f"serial: str = {device_id!r}"),
        ('model: str = "CYCLER-32"', f"model: str = {args.model!r}"),
        ('vendor: str = "示例厂家（模拟）"', f"vendor: str = {args.vendor + '（模拟）'!r}"),
    ])
    _replace(target / "gateway.py", [('"GATEWAY_DEVICE_ID", "SIM-CYC-M1"', f'"GATEWAY_DEVICE_ID", {device_id!r}')])
    for name in ("Dockerfile", "compose.yml"):
        _replace(target / "deploy" / name, [("sample-cycler", args.name)])
    compose = target / "deploy" / "compose.yml"
    compose.write_text(compose.read_text(encoding="utf-8").replace("SIM-CYC-M1", device_id), encoding="utf-8")

    sys.path.insert(0, str(ROOT / "api"))
    from app.services.template_service import FORMAT, template_digest

    profile = json.loads((SAMPLE / "profile.json").read_text(encoding="utf-8"))
    code = "TPL-" + re.sub(r"[^A-Z0-9]+", "-", args.name.upper()).strip("-")
    profile.update(
        format=FORMAT, code=code, revision=1, name=args.title, model=args.model, vendor=args.vendor,
        version=f"{args.name} 0.1", state="draft",
        connection={**profile["connection"], "base_url": f"https://{args.name}-gw.lab.internal:8443/api/v1"},
        acceptance={"capability": args.capability,
                    "params": {name: round((low + high) / 2, 6) for name, (low, high) in params.items()}},
        note=f"设备模块 devices/modules/{args.name}：{args.title}（厂家 SDK 接口服务）。",
    )
    profile["digest"] = template_digest(profile)
    (target / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "README.md").write_text(f"""# 设备模块：{args.title}

{args.vendor} {args.model}，ILCS 能力 `{args.capability}`，经本模块的网关（`http_json_v1`）接入。
从样板 `devices/modules/sample-cycler` 生成，结构与交付要求见 [devices/modules/README.md](../README.md)。

```bash
api/.venv/bin/pytest devices/modules/{args.name}/tests                                # 自测（对模拟接口跑 ILCS 验收清单）
python devices/modules/{args.name}/gateway.py --simulate --insecure --port 8443        # 本机联调
```

## 还要按设备手册改的

- [ ] `driver/vendor_sdk.py`：厂家 SDK 的调用面与 `load_sdk()`
- [ ] `driver/device.py`：状态映射 `STATES`、实测值（现在是样板里充放电柜的循环数与容量）、急停 / 通道判断
- [ ] `simulator/fake_sdk.py`：跟着改成同一组方法，实测值换成这台设备的
- [ ] `profile.json`：连接参数示例、支持标志（设备真支持保持 / 终止才写 true）、验收参数
- [ ] `deploy/`：证书主机名、网关放在哪台机器
""", encoding="utf-8")
    print(f"已生成 {target}（设备编号 {device_id}，模板编号 {code}）")
    print(f"下一步：api/.venv/bin/pytest {target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}/tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
