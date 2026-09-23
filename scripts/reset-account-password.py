#!/usr/bin/env python
"""运维救援：在无法登录治理页面时重置一个已有账号的临时口令。

口令从 ILCS_RESET_PASSWORD 读取；交互终端未设置时用 getpass 输入。明文不打印、
不写审计详情，只记录一次系统来源的重置事件。正式容器中数据库与 pepper 配置必须
与 API 完全一致，否则新摘要无法登录。
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.core.clock import now  # noqa: E402
from app.core.db import SessionLocal, engine  # noqa: E402
from app.core.schema import verify  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import AuditEvent, User  # noqa: E402
from app.repositories.organization import MembershipRepository  # noqa: E402
from app.services.identity_service import IdentityService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重置 ILCS 账号临时口令")
    parser.add_argument("username", help="要重置的登录账号，例如 admin")
    args = parser.parse_args()
    verify(engine)
    password = os.environ.get("ILCS_RESET_PASSWORD")
    if not password:
        password = getpass.getpass("新的临时口令（输入不回显）：")
        confirm = getpass.getpass("再次输入：")
        if password != confirm:
            print("两次口令不一致", file=sys.stderr)
            return 2
    IdentityService._validate_new_password(password)
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == args.username).first()
        if user is None:
            print(f"账号 {args.username} 不存在", file=sys.stderr)
            return 2
        memberships = MembershipRepository(db).active_for_user(user.id)
        user.password_hash = hash_password(password)
        user.must_change_password = True
        user.password_changed_at = None
        user.row_version += 1
        for membership in memberships:
            db.add(AuditEvent(
                org_id=membership.org_id,
                user="运维命令",
                user_id="system",
                role="system",
                action="重置账号口令",
                target=user.id,
                object_version=user.row_version,
                detail="救援命令重置临时口令；首次登录必须修改；明文未记录",
                time=now(),
            ))
        db.commit()
    print(f"账号 {args.username} 已重置；首次登录必须修改口令")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
