#!/usr/bin/env python3
"""离线检查正式部署配置，不连接数据库、不执行 env 文件里的任何内容。"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api"
sys.path.insert(0, str(API))

from app.core.config import Settings  # noqa: E402


REQUIRED = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "ILCS_DATABASE_URL",
    "ILCS_ENVIRONMENT",
    "ILCS_SECRET_KEY",
    "ILCS_PASSWORD_PEPPER",
    "ILCS_FILE_ROOT",
    "ILCS_CORS_ORIGINS",
    "ILCS_ADAPTER_ALLOWED_HOSTS",
    "ILCS_MIGRATION_ORG_ID",
    "ILCS_MIGRATION_ORG_CODE",
    "ILCS_MIGRATION_ORG_NAME",
    "ILCS_MIGRATION_ORG_TIMEZONE",
)
PLACEHOLDER = re.compile(r"__CHANGE_ME|change[-_ ]?me|example\.(?:com|internal)", re.I)


def read_env(path: Path) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    errors: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            errors.append(f"第 {number} 行不是 KEY=VALUE")
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            errors.append(f"第 {number} 行键名不合法")
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in values:
            errors.append(f"{key} 重复定义")
        values[key] = value
    return values, errors


def inspect(values: dict[str, str], initial_seed: bool = False) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for key in REQUIRED:
        value = values.get(key, "").strip()
        if not value:
            errors.append(f"缺少 {key}")
        elif PLACEHOLDER.search(value):
            errors.append(f"{key} 仍是示例占位值")
        elif "${" in value:
            errors.append(f"{key} 含未展开变量")

    settings_fields = {
        "environment": values.get("ILCS_ENVIRONMENT", "development"),
        "secret_key": values.get("ILCS_SECRET_KEY", ""),
        "password_pepper": values.get("ILCS_PASSWORD_PEPPER", ""),
        "database_url": values.get("ILCS_DATABASE_URL", ""),
        "cors_origins": values.get("ILCS_CORS_ORIGINS", ""),
        "adapter_allowed_hosts": values.get("ILCS_ADAPTER_ALLOWED_HOSTS", ""),
        "file_orphan_retention_hours": values.get("ILCS_FILE_ORPHAN_RETENTION_HOURS", "24"),
        "file_cleanup_interval_sec": values.get("ILCS_FILE_CLEANUP_INTERVAL_SEC", "3600"),
        "file_cleanup_batch_size": values.get("ILCS_FILE_CLEANUP_BATCH_SIZE", "100"),
    }
    try:
        configured = Settings(**settings_fields)
        errors.extend(issue for issue in configured.production_issues() if issue not in errors)
    except Exception as exc:  # Pydantic 会列明字段与类型，不打印任何秘密值
        errors.append(f"应用配置类型校验失败：{exc}")

    database_url = values.get("ILCS_DATABASE_URL", "")
    if database_url:
        try:
            url = make_url(database_url)
            if not url.drivername.startswith("postgresql"):
                errors.append("ILCS_DATABASE_URL 必须使用 PostgreSQL")
            if (url.username or "") != values.get("POSTGRES_USER", ""):
                errors.append("ILCS_DATABASE_URL 用户名与 POSTGRES_USER 不一致")
            if unquote(url.password or "") != values.get("POSTGRES_PASSWORD", ""):
                errors.append("ILCS_DATABASE_URL 密码与 POSTGRES_PASSWORD 不一致")
            if (url.database or "") != values.get("POSTGRES_DB", ""):
                errors.append("ILCS_DATABASE_URL 库名与 POSTGRES_DB 不一致")
        except Exception:
            errors.append("ILCS_DATABASE_URL 不是合法数据库 URL")

    secrets = {
        "POSTGRES_PASSWORD": values.get("POSTGRES_PASSWORD", ""),
        "ILCS_SECRET_KEY": values.get("ILCS_SECRET_KEY", ""),
        "ILCS_PASSWORD_PEPPER": values.get("ILCS_PASSWORD_PEPPER", ""),
    }
    for key, value in secrets.items():
        if value and len(value) < 16:
            errors.append(f"{key} 长度不足 16 位")
    nonempty = [(key, value) for key, value in secrets.items() if value]
    for index, (left_key, left) in enumerate(nonempty):
        for right_key, right in nonempty[index + 1:]:
            if left == right:
                errors.append(f"{left_key} 与 {right_key} 不能复用同一秘密")

    initial_password = values.get("ILCS_INITIAL_PASSWORD", "")
    if initial_seed:
        if len(initial_password) < 16 or PLACEHOLDER.search(initial_password):
            errors.append("首次初始化必须设置至少 16 位且非占位的 ILCS_INITIAL_PASSWORD")
        if initial_password and initial_password in secrets.values():
            errors.append("ILCS_INITIAL_PASSWORD 不能与数据库或应用密钥相同")
    elif initial_password:
        warnings.append("首次初始化完成后应从 .env 删除 ILCS_INITIAL_PASSWORD")

    timezone = values.get("ILCS_MIGRATION_ORG_TIMEZONE", "")
    if timezone:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            errors.append("ILCS_MIGRATION_ORG_TIMEZONE 不是有效 IANA 时区")

    origins = [item.strip() for item in values.get("ILCS_CORS_ORIGINS", "").split(",") if item.strip()]
    if any(not origin.startswith("https://") for origin in origins):
        warnings.append("CORS 中存在非 HTTPS 来源；仅允许它位于受控内网并由上游网关终止 TLS")
    if values.get("ILCS_EXECUTOR_SIMULATE_HEARTBEAT", "0").strip().lower() in {"1", "true", "yes", "on"}:
        errors.append("正式环境不允许模拟心跳：ILCS_EXECUTOR_SIMULATE_HEARTBEAT 必须为 0 或删除")

    stale = values.get("ILCS_EXECUTOR_STALE_SEC", "60").strip()
    try:
        if int(stale) <= 0:
            errors.append("ILCS_EXECUTOR_STALE_SEC 必须大于 0：正式环境必须检查执行器存活")
    except ValueError:
        errors.append("ILCS_EXECUTOR_STALE_SEC 必须是整数")

    max_bytes = values.get("ILCS_FILE_MAX_BYTES", "20971520")
    try:
        if int(max_bytes) != 20 * 1024 * 1024:
            warnings.append("ILCS_FILE_MAX_BYTES 已偏离 nginx.conf 的 20 MiB，需同步修改网关上限")
    except ValueError:
        errors.append("ILCS_FILE_MAX_BYTES 必须是整数")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def artifact_checks(root: Path) -> list[str]:
    errors: list[str] = []
    if not (root / "web" / "dist" / "index.html").is_file():
        errors.append("缺少 web/dist/index.html，请先执行 npm run build")
    if not any((root / "web" / "dist" / "assets").glob("*.js")):
        errors.append("缺少前端 JavaScript 构建制品")
    else:
        compiled = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (root / "web" / "dist" / "assets").glob("*.js")
        )
        for secret in ("ilcs1234", "ilcs-lims-dev-secret", "ilcs-executor-dev-secret"):
            if secret in compiled:
                errors.append("生产前端制品包含演示凭据")
                break
    for relative in ("deploy/docker-compose.yml", "deploy/nginx.conf", "deploy/Dockerfile"):
        if not (root / relative).is_file():
            errors.append(f"缺少 {relative}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="离线检查 ILCS 正式部署配置与制品")
    parser.add_argument("--env-file", type=Path, default=ROOT / "deploy" / ".env")
    parser.add_argument(
        "--from-environment", action="store_true",
        help="从当前进程环境读取配置（用于 docker compose run migrate）",
    )
    parser.add_argument(
        "--skip-artifacts", action="store_true",
        help="跳过宿主前端制品检查（迁移容器内没有 web/dist）",
    )
    parser.add_argument("--initial-seed", action="store_true", help="要求首次初始化临时口令存在")
    args = parser.parse_args()
    if args.from_environment:
        values, errors = dict(os.environ), []
        source = "当前进程环境"
    else:
        if not args.env_file.is_file():
            print(f"✗ 配置文件不存在：{args.env_file}")
            return 2
        values, errors = read_env(args.env_file)
        source = str(args.env_file)
    config_errors, warnings = inspect(values, initial_seed=args.initial_seed)
    errors.extend(config_errors)
    if not args.skip_artifacts:
        errors.extend(artifact_checks(ROOT))

    print(f"检查：{source}")
    for warning in warnings:
        print(f"  ! {warning}")
    for error in errors:
        print(f"  ✗ {error}")
    if errors:
        print(f"结论：未就绪（{len(errors)} 项错误，{len(warnings)} 项提醒）")
        return 1
    print(f"结论：配置与制品检查通过（{len(warnings)} 项提醒）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
