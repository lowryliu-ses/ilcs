#!/usr/bin/env python3
"""打印一个批次的设备回报：每个设备步骤下发的设定值与设备实测值，按样本对齐。

界面的「指令与检查点」只列状态；录检测结果时要用设备实测的注液量、放电容量，用这个命令看：

    python3 scripts/case-readings.py B-260927-001                          # 本地 8090
    python3 scripts/case-readings.py B-260927-001 http://10.10.106.51:8090  # 远端

只读，用 operator 账号登录（演示口令）。
"""
from __future__ import annotations

import json
import sys
import urllib.request

PASSWORD = "ilcs1234"


def call(base: str, path: str, token: str = "", body: dict | None = None):
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET", headers={"Content-Type": "application/json"},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def well_key(well: str) -> tuple:
    return (well[:1], int(well[1:]) if well[1:].isdigit() else 0)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    batch_id = sys.argv[1]
    base = (sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8090").rstrip("/") + "/api"
    token = call(base, "/auth/login", body={"username": "operator", "password": PASSWORD})["access_token"]
    batch = call(base, f"/batches/{batch_id}", token)
    samples = sorted(batch["samples"], key=lambda s: s["position"])
    print(f"{batch_id} · {batch['state']} · {batch.get('recipe_name') or ''}")
    for checkpoint in sorted(batch["checkpoints"], key=lambda c: (c["step_index"], c.get("created_at") or "")):
        payload = checkpoint["payload"]
        params, delivered = payload.get("params") or {}, payload.get("delivered") or {}
        step = next((r for r in batch["step_runs"] if r["step_index"] == checkpoint["step_index"]), {})
        print(f"\n第 {checkpoint['step_index'] + 1} 步 {step.get('step_name', '')} @ {payload['station_id']}"
              f"（来源 {payload['origin']}）")
        fixed = {k: v for k, v in params.items() if k != "wells"}
        if fixed:
            print("  设定：" + "  ".join(f"{k}={v}" for k, v in fixed.items()))
        if "discharge_capacity_mAh" in delivered:
            print(f"  实测：通道 {delivered.get('channel')} · 完成 {delivered.get('cycles_completed')} 圈 · "
                  f"放电容量 {delivered['discharge_capacity_mAh']} mAh")
        wells = delivered.get("wells") or {}
        if params.get("wells") and not wells:
            # 设备只回报整体结果（如充放电柜）：列出按孔位下发的设定
            print("  按孔位下发：" + "  ".join(
                f"{well} " + ",".join(f"{k}={v}" for k, v in values.items())
                for well, values in sorted(params["wells"].items(), key=lambda item: well_key(item[0]))))
        if wells:
            # 设备按实体孔位回报；绑定托盘时实体孔位与布局孔位不同，按孔位顺序对应到样本
            ordered = sorted(wells, key=well_key)
            mapped = {s["well"]: s["well"] for s in samples} if all(s["well"] in wells for s in samples) \
                else {s["well"]: well for s, well in zip(samples, ordered)}
            setpoints = params.get("wells") or {}
            print("  样本            布局孔位  设备孔位  设定 → 实测")
            for sample in samples:
                device_well = mapped.get(sample["well"], "")
                actual = wells.get(device_well) or {}
                wanted = setpoints.get(device_well) or fixed
                cells = "  ".join(f"{k} {wanted.get(k, '—')} → {v}" for k, v in actual.items())
                print(f"  {sample['id']:<16}{sample['well']:<10}{device_well:<10}{cells}")
        for row in delivered.get("materials") or []:
            print(f"  物料消耗：{row['material']} {row['quantity']} {row['unit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
