"""认证摘要格式的兼容与安全属性。"""
import hashlib


def test_password_hashes_use_independent_salts_and_verify():
    from app.core.security import hash_password, password_needs_rehash, verify_password

    first = hash_password("Correct-Horse-2026")
    second = hash_password("Correct-Horse-2026")

    assert first != second
    assert first.startswith("pbkdf2_sha256$")
    assert verify_password("Correct-Horse-2026", first)
    assert not verify_password("wrong-password", first)
    assert not password_needs_rehash(first)


def test_legacy_unsalted_hash_is_accepted_but_marked_for_upgrade():
    from app.core.config import settings
    from app.core.security import password_needs_rehash, verify_password

    raw = "Legacy-Password-2026"
    pepper = (settings.password_pepper or settings.secret_key).encode()
    legacy = hashlib.pbkdf2_hmac("sha256", raw.encode(), pepper, 120_000).hex()

    assert verify_password(raw, legacy)
    assert password_needs_rehash(legacy)
