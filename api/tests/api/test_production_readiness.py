"""正式部署配置的离线门禁。"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).parents[3]
SPEC = spec_from_file_location(
    "production_readiness", ROOT / "scripts" / "check-production-readiness.py"
)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def safe_values() -> dict[str, str]:
    password = "db-" + "d" * 30
    return {
        "POSTGRES_DB": "ilcs",
        "POSTGRES_USER": "ilcs",
        "POSTGRES_PASSWORD": password,
        "ILCS_DATABASE_URL": f"postgresql+psycopg2://ilcs:{password}@db:5432/ilcs",
        "ILCS_ENVIRONMENT": "production",
        "ILCS_SECRET_KEY": "jwt-" + "s" * 40,
        "ILCS_PASSWORD_PEPPER": "pepper-" + "p" * 40,
        "ILCS_INITIAL_PASSWORD": "Initial-Password-2026!",
        "ILCS_FILE_ROOT": "/data/files",
        "ILCS_FILE_MAX_BYTES": "20971520",
        "ILCS_FILE_ORPHAN_RETENTION_HOURS": "24",
        "ILCS_FILE_CLEANUP_INTERVAL_SEC": "3600",
        "ILCS_FILE_CLEANUP_BATCH_SIZE": "100",
        "ILCS_CORS_ORIGINS": "https://ilcs.example.lab",
        "ILCS_ADAPTER_ALLOWED_HOSTS": "instrument-gateway.lab.internal,10.20.1.31",
        "ILCS_EXECUTOR_SIMULATE_HEARTBEAT": "0",
        "ILCS_MIGRATION_ORG_ID": "ORG-LAB",
        "ILCS_MIGRATION_ORG_CODE": "LAB",
        "ILCS_MIGRATION_ORG_NAME": "正式实验室",
        "ILCS_MIGRATION_ORG_TIMEZONE": "Asia/Shanghai",
    }


def test_safe_initial_production_configuration_passes():
    errors, warnings = MODULE.inspect(safe_values(), initial_seed=True)
    assert errors == []
    assert warnings == []


def test_readiness_rejects_placeholders_mismatch_and_secret_reuse():
    values = safe_values()
    values.update(
        {
            "POSTGRES_PASSWORD": "same-secret-value",
            "ILCS_SECRET_KEY": "same-secret-value",
            "ILCS_DATABASE_URL": "sqlite:///ilcs.db",
            "ILCS_MIGRATION_ORG_TIMEZONE": "Mars/Olympus",
            "ILCS_INITIAL_PASSWORD": "__CHANGE_ME__",
        }
    )
    errors, _ = MODULE.inspect(values, initial_seed=True)
    joined = "；".join(errors)
    assert "PostgreSQL" in joined
    assert "用户名与 POSTGRES_USER 不一致" in joined
    assert "数据库或应用密钥" in joined or "占位" in joined
    assert "不能复用同一秘密" in joined
    assert "IANA 时区" in joined


def test_env_reader_does_not_execute_shell_syntax(tmp_path):
    marker = tmp_path / "must-not-exist"
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"SAFE=value\nDANGEROUS=$(touch {marker})\nSAFE=again\nBROKEN\n",
        encoding="utf-8",
    )
    values, errors = MODULE.read_env(env_file)
    assert values["DANGEROUS"].startswith("$(touch ")
    assert not marker.exists()
    assert any("重复定义" in error for error in errors)
    assert any("KEY=VALUE" in error for error in errors)
