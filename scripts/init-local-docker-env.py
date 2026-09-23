#!/usr/bin/env python3
"""Generate a private development-only Compose environment for local Docker."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / "deploy" / ".env"


def build_env() -> str:
    database_password = secrets.token_hex(24)
    secret_key = secrets.token_hex(48)
    password_pepper = secrets.token_hex(48)
    return f"""# Generated for local Docker development. Do not commit this file.
POSTGRES_DB=ilcs
POSTGRES_USER=ilcs
POSTGRES_PASSWORD={database_password}
ILCS_DATABASE_URL=postgresql+psycopg2://ilcs:{database_password}@db:5432/ilcs

ILCS_DB_POOL_SIZE=10
ILCS_DB_MAX_OVERFLOW=20
ILCS_DB_POOL_TIMEOUT_SEC=30
ILCS_DB_POOL_RECYCLE_SEC=1800

ILCS_ENVIRONMENT=development
ILCS_SECRET_KEY={secret_key}
ILCS_PASSWORD_PEPPER={password_pepper}

ILCS_FILE_ROOT=/data/files
ILCS_FILE_MAX_BYTES=20971520
ILCS_FILE_ORPHAN_RETENTION_HOURS=24
ILCS_FILE_CLEANUP_INTERVAL_SEC=3600
ILCS_FILE_CLEANUP_BATCH_SIZE=100

# Local demo adapters must never reach hosts outside this allow-list.
ILCS_ADAPTER_ALLOWED_HOSTS=127.0.0.1,localhost
ILCS_ADAPTER_CREDENTIAL_ROOT=/run/secrets/ilcs
ILCS_WEB_BIND=127.0.0.1
ILCS_CORS_ORIGINS=http://127.0.0.1:8090,http://localhost:8090

ILCS_ADVANCE_POLL_SEC=2
ILCS_EXECUTOR_POLL_SEC=1
ILCS_EXECUTOR_SIMULATE_HEARTBEAT=1

ILCS_MIGRATION_ORG_ID=ORG-001
ILCS_MIGRATION_ORG_CODE=MAIN
ILCS_MIGRATION_ORG_NAME=本部电池实验室
ILCS_MIGRATION_ORG_TIMEZONE=Asia/Shanghai
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing local environment file",
    )
    args = parser.parse_args()

    if ENV_PATH.exists() and not args.force:
        print(f"Kept existing local environment: {ENV_PATH}")
        return 0

    ENV_PATH.write_text(build_env(), encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)
    print(f"Generated private local environment: {ENV_PATH} (mode 0600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
