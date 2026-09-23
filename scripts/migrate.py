#!/usr/bin/env python
"""迁移入口。部署时独立执行，不由 API 或执行器在启动时代劳。

    api/.venv/bin/python scripts/migrate.py status
    api/.venv/bin/python scripts/migrate.py upgrade [--stamp-baseline] [--report 路径]
    api/.venv/bin/python scripts/migrate.py seed         # 仅显式初始化或测试环境
    api/.venv/bin/python scripts/migrate.py verify       # 核对迁移结果

`upgrade --stamp-baseline` 用于首次接入 Alembic 的老库：先打 0001 基线标记，
再跑后续迁移。空库不需要它。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api"
sys.path.insert(0, str(API))

BASELINE = "0001_legacy_baseline"
# 基线里就有的表。用它分辨「首次接入 Alembic 的老库」与「全新空库」。
LEGACY_SENTINEL = "batches"


def alembic(*args: str) -> int:
    """开发机走 api/.venv 里的 alembic；容器里依赖装在系统 site-packages，没有那个目录。"""
    binary = API / ".venv" / "bin" / "alembic"
    command = [str(binary)] if binary.exists() else [sys.executable, "-m", "alembic"]
    return subprocess.call([*command, *args], cwd=API)


def cmd_status(_: argparse.Namespace) -> int:
    from app.core.config import settings
    from app.core.db import engine
    from app.core.schema import EXPECTED_REVISION, current_revision, known_revisions

    print(f"连接：{settings.database_url}")
    print(f"当前版本：{current_revision(engine) or '（无 alembic_version 表）'}")
    print(f"应用期待：{EXPECTED_REVISION}")
    print(f"已知迁移：{'、'.join(known_revisions())}")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    from sqlalchemy import inspect

    from app.core.db import engine
    from app.core.schema import current_revision

    if args.report:
        os.environ["ILCS_MIGRATION_REPORT"] = args.report
    if args.stamp_baseline and current_revision(engine) is None:
        # 只有「有老表、没版本记录」才是需要打基线的老库。空库打了基线就再也不会
        # 建基线里的那些表，后续迁移会去改不存在的表——所以这里必须看有没有老表，
        # 不能只看有没有 alembic_version。部署脚本一律带 --stamp-baseline，
        # 空库与老库的分辨交给这一处，省得调用方自己判断。
        tables = set(inspect(engine).get_table_names())
        if LEGACY_SENTINEL in tables:
            print(f"老库打基线标记 {BASELINE}（已存在 {len(tables)} 张表）")
            code = alembic("stamp", BASELINE)
            if code:
                return code
        else:
            print("空库：从头建结构，不打基线标记")
    return alembic("upgrade", "head")


def cmd_seed(args: argparse.Namespace) -> int:
    """显式播种。生产库上默认拒绝，除非带 --force。"""
    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.seed import seed

    url = settings.database_url
    if not args.force and ("postgres" in url or "/opt/ilcs" in url):
        print("正式库不自动播种；确实要初始化请加 --force", file=sys.stderr)
        return 2
    initial_password = None
    force_password_change = False
    include_service_identities = True
    if args.master_only:
        issues = settings.production_issues()
        if issues:
            print("正式配置未通过：" + "；".join(issues), file=sys.stderr)
            return 2
        initial_password = os.environ.get("ILCS_INITIAL_PASSWORD", "")
        if len(initial_password) < 16:
            print(
                "正式主数据初始化必须通过 ILCS_INITIAL_PASSWORD 提供至少 16 位临时口令",
                file=sys.stderr,
            )
            return 2
        force_password_change = True
        include_service_identities = False
    with SessionLocal() as db:
        summary = seed(
            db,
            org_id=args.org,
            org_code=os.environ.get("ILCS_MIGRATION_ORG_CODE", "MAIN"),
            org_name=os.environ.get("ILCS_MIGRATION_ORG_NAME", "本部电池实验室"),
            org_timezone=os.environ.get("ILCS_MIGRATION_ORG_TIMEZONE", "Asia/Shanghai"),
            master_only=args.master_only,
            initial_password=initial_password,
            force_password_change=force_password_change,
            include_service_identities=include_service_identities,
        )
    print(f"播种完成：{summary}")
    return 0


def cmd_verify(_: argparse.Namespace) -> int:
    from app.core.db import SessionLocal
    from app.services.migration_report_service import MigrationReportService

    with SessionLocal() as db:
        report = MigrationReportService(db).reconcile()
    for line in report["lines"]:
        print(("  ✗ " if not line["ok"] else "  ✓ ") + line["label"] + "：" + line["detail"])
    print(f"\n核对结论：{'通过' if report['ok'] else '存在待处理项'}")
    return 0 if report["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="ILCS 数据库迁移")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status").set_defaults(func=cmd_status)
    upgrade = subparsers.add_parser("upgrade")
    upgrade.add_argument("--stamp-baseline", action="store_true")
    upgrade.add_argument("--report", default="")
    upgrade.set_defaults(func=cmd_upgrade)
    seed_parser = subparsers.add_parser("seed")
    seed_parser.add_argument("--force", action="store_true")
    seed_parser.add_argument("--org", default="ORG-001")
    seed_parser.add_argument(
        "--master-only", action="store_true",
        help="正式最小初始化：只建指定组织、默认实验室和管理员，不写任何演示业务数据",
    )
    seed_parser.set_defaults(func=cmd_seed)
    subparsers.add_parser("verify").set_defaults(func=cmd_verify)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
