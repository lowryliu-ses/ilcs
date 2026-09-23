#!/usr/bin/env python
"""把老库里已有的事实补成人员档案与资产档案。

    python scripts/backfill-master-data.py plan               # 只打印将要建什么
    python scripts/backfill-master-data.py apply              # 真的建，并写出清单
    python scripts/backfill-master-data.py undo <清单路径>    # 按清单回退

只搬已经存在的数据：
- 人员档案 ← 账号（姓名取 display_name，职务取角色名称）。**不带任何资质。**
- 资产档案 ← 工位行（名称、型号、所在功能岛都照抄），并把工位指向它。

**刻意不做的事：**
不建校准记录。工位上的 cal_due 只说到期日，没说哪天校准的、证书编号是什么；
而系统明确要求「合格校准必须附证书文件」（`certificate_required`）。绕开这条去写一条
无证书的合格记录，等于用脚本伪造一份计量背书——那正是这套系统要防的事。
同理不建资质记录：谁有哪项资质、什么时候到期，老库里从来没有这个信息。

所以跑完之后批次仍然下发不了。变化只是开跑检查从「无法校验」变成「缺哪一项」，
并且责任人可以直接在界面上给这些档案挂资质与证书，不必先手工建 8 台资产、5 份档案。

幂等：已关联的工位与已有档案的账号会跳过，重复跑不会建第二份。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

# 转运车只实现它，不承接工步，也没有校准档案要核——不给它建资产
TRANSPORT_CAPABILITY = "cap.transfer"

ROLE_TITLE = {
    "operator": "实验操作员",
    "researcher": "研究员",
    "qa": "质量负责人",
    "ehs": "安全环保",
    "admin": "系统管理",
}


def source_note(source: str) -> str:
    return (
        f"由{source}派生补录（{datetime.now().isoformat(timespec='minutes')}）；"
        "未附资质 / 校准证书，需责任人补录"
    )


def collect(db, org_id: str) -> dict:
    """算出要建什么。不写库，plan 与 apply 共用，保证两者看到的是同一份。"""
    from app.models import Asset, Island, Person, Station, User

    islands = {row.id: row.name for row in db.query(Island).all()}

    people = []
    linked = {row.user_id for row in db.query(Person).all() if row.user_id}
    for user in db.query(User).filter(User.state == "active").order_by(User.username).all():
        if user.id in linked:
            continue
        people.append(
            {
                "code": f"P-{user.username.upper()}",
                "name": user.display_name,
                "title": ROLE_TITLE.get(user.role, user.role),
                "user_id": user.id,
                "username": user.username,
            }
        )

    taken = {row.asset_no for row in db.query(Asset).filter(Asset.org_id == org_id).all()}
    assets = []
    for station in db.query(Station).order_by(Station.id).all():
        if station.asset_id or station.retired:
            continue
        if not (set(station.limits or {}) - {TRANSPORT_CAPABILITY}):
            continue
        asset_no = f"AS-{station.id}"
        if asset_no in taken:
            continue
        assets.append(
            {
                "asset_no": asset_no,
                "name": station.name,
                "model": station.model,
                "location": islands.get(station.island, ""),
                "station_id": station.id,
                "positions": station.positions,
                "cal_due": station.cal_due,
            }
        )
    return {"people": people, "assets": assets}


def describe(plan: dict) -> None:
    print(f"人员档案 {len(plan['people'])} 份：")
    for row in plan["people"]:
        print(f"  {row['code']}  {row['name']}  {row['title']}  ← 账号 {row['username']}")
    print(f"\n资产档案 {len(plan['assets'])} 份（容量一律按 1 建）：")
    for row in plan["assets"]:
        print(
            f"  {row['asset_no']}  {row['name']}  {row['model'] or '型号未记录'}"
            f"  {row['location'] or '位置未记录'}  ← 工位 {row['station_id']}"
        )
    print(
        "\n口径：一个工位建一台资产，容量 1。工位上的「位数」是托盘孔位数，"
        "从来没有参与过并发判断，所以这样建出来的排程约束与原先逐工位时间窗完全一致，"
        "不会放松也不会收紧。"
    )
    print(
        "现实里若有一台设备同时承接多个工位（两个工位共用一台），需要把这两份资产合并并"
        "按实际并行通道调容量——那是物理事实，脚本不替你判断。"
    )
    print("\n不会建：校准记录、资质记录。理由见本脚本开头。")


def apply(db, org_id: str, plan: dict) -> dict:
    from app.core.context import system_context
    from app.models import Asset, Person, Station
    from app.services.audit_service import AuditService

    audit = AuditService(db, system_context(org_id, label="主数据补录"))
    created = {"people": [], "assets": [], "stations": []}

    for row in plan["people"]:
        person = Person(
            org_id=org_id, code=row["code"], name=row["name"], title=row["title"],
            user_id=row["user_id"], note=source_note(f"账号 {row['username']}"),
        )
        db.add(person)
        db.flush()
        created["people"].append(person.id)
        audit.record(
            None, "补录人员档案", person.id, after=f"{row['code']} {row['name']}",
            detail=f"由账号 {row['username']} 派生；未附任何资质记录",
        )

    for row in plan["assets"]:
        asset = Asset(
            org_id=org_id, asset_no=row["asset_no"], name=row["name"], model=row["model"],
            location=row["location"], capacity=1,
            note=source_note(f"工位 {row['station_id']}"),
        )
        db.add(asset)
        db.flush()
        station = db.get(Station, row["station_id"])
        station.asset_id = asset.id
        created["assets"].append(asset.id)
        created["stations"].append({"id": station.id, "asset_id": asset.id})
        audit.record(
            None, "补录资产档案", asset.id, after=f"{row['asset_no']} {row['name']}",
            detail=(
                f"由工位 {station.id} 派生（名称 / 型号 / 位置照抄）；容量 1；"
                f"工位原 cal_due {row['cal_due'] or '未记录'} 保留为展示字段，"
                "未据此生成校准记录"
            ),
        )

    db.commit()
    return created


def undo(db, org_id: str, manifest: dict) -> int:
    """按清单回退。只删还没被引用的行——挂过资质或校准的档案已经是别人的工作成果。"""
    from app.core.context import system_context
    from app.models import Asset, CalibrationRecord, Person, Qualification, ResourceBooking, Station
    from app.services.audit_service import AuditService

    audit = AuditService(db, system_context(org_id, label="主数据补录"))
    removed = 0
    for entry in manifest.get("stations", []):
        station = db.get(Station, entry["id"])
        if station is not None and station.asset_id == entry["asset_id"]:
            station.asset_id = ""

    for asset_id in manifest.get("assets", []):
        asset = db.get(Asset, asset_id)
        if asset is None:
            continue
        blockers = []
        if db.query(CalibrationRecord).filter(CalibrationRecord.asset_id == asset_id).count():
            blockers.append("已登记校准记录")
        if db.query(ResourceBooking).filter(ResourceBooking.asset_id == asset_id).count():
            blockers.append("已有资源占用")
        if blockers:
            print(f"  保留 {asset.asset_no}：{'、'.join(blockers)}")
            continue
        audit.record(None, "回退补录的资产档案", asset_id, before=f"{asset.asset_no} {asset.name}")
        db.delete(asset)
        removed += 1

    for person_id in manifest.get("people", []):
        person = db.get(Person, person_id)
        if person is None:
            continue
        if db.query(Qualification).filter(Qualification.person_id == person_id).count():
            print(f"  保留 {person.code}：已登记资质")
            continue
        audit.record(None, "回退补录的人员档案", person_id, before=f"{person.code} {person.name}")
        db.delete(person)
        removed += 1

    db.commit()
    return removed


def resolve_org(db) -> str:
    """组织归属与 0003 迁移用同一个来源；对不上就停下来问，不猜。"""
    import os

    from app.models import Organization

    wanted = os.environ.get("ILCS_MIGRATION_ORG_ID", "ORG-001")
    if db.get(Organization, wanted) is not None:
        return wanted
    rows = db.query(Organization).all()
    if len(rows) == 1:
        print(f"ILCS_MIGRATION_ORG_ID={wanted} 不存在；库里只有一个组织 {rows[0].id}，用它")
        return rows[0].id
    raise SystemExit(
        f"组织 {wanted} 不存在，且库里有 {len(rows)} 个组织，无法判断归属："
        f"{'、'.join(row.id for row in rows)}。请用 ILCS_MIGRATION_ORG_ID 指定。"
    )


def main() -> int:
    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.core.schema import verify

    command = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if command not in {"plan", "apply", "undo"}:
        print(__doc__)
        return 2

    from app.core.db import engine

    verify(engine)  # 结构不对就别动数据

    with SessionLocal() as db:
        org_id = resolve_org(db)
        if command == "undo":
            if len(sys.argv) < 3:
                print("用法：backfill-master-data.py undo <清单路径>", file=sys.stderr)
                return 2
            manifest = json.loads(Path(sys.argv[2]).read_text())
            print(f"按清单回退：{sys.argv[2]}")
            print(f"已删除 {undo(db, org_id, manifest)} 行")
            return 0

        plan = collect(db, org_id)
        describe(plan)
        if not plan["people"] and not plan["assets"]:
            print("\n没有要补录的行。")
            return 0
        if command == "plan":
            print("\n以上只是计划。确认后执行：backfill-master-data.py apply")
            return 0

        created = apply(db, org_id, plan)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = Path(settings.file_root).parent / f"backfill-{stamp}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(created, ensure_ascii=False, indent=2))
        print(
            f"\n已建人员档案 {len(created['people'])} 份、资产档案 {len(created['assets'])} 份"
            f"（工位关联 {len(created['stations'])} 个）"
        )
        print(f"清单：{out}（回退用：backfill-master-data.py undo {out}）")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
