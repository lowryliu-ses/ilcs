"""口令、服务凭据与令牌。身份模块的边界只有这几个函数，将来换 Keycloak OIDC 不动业务层。"""
import hashlib
import hmac
import secrets
from datetime import timedelta

import jwt

from .clock import now
from .config import settings
from .errors import Unauthenticated

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 310_000
LEGACY_PASSWORD_ITERATIONS = 120_000


def _pepper() -> bytes:
    return (settings.password_pepper or settings.secret_key).encode()


def hash_password(raw: str) -> str:
    """生成可自描述、每账号独立盐值的口令摘要。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", raw.encode(), salt + _pepper(), PASSWORD_ITERATIONS
    )
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(raw: str, hashed: str) -> bool:
    try:
        scheme, iterations, salt_hex, expected = hashed.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        salt = bytes.fromhex(salt_hex)
        rounds = int(iterations)
        if rounds < LEGACY_PASSWORD_ITERATIONS or len(salt) < 16:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt + _pepper(), rounds).hex()
        return hmac.compare_digest(actual, expected)
    except (AttributeError, TypeError, ValueError):
        # 0001–0006 数据的兼容路径：旧格式没有 scheme/salt，成功登录后会升级。
        legacy = hashlib.pbkdf2_hmac(
            "sha256", raw.encode(), _pepper(), LEGACY_PASSWORD_ITERATIONS
        ).hex()
        return hmac.compare_digest(legacy, hashed or "")


def password_needs_rehash(hashed: str) -> bool:
    """旧无盐格式或低迭代格式在下一次成功登录时升级，不强制作废账号。"""
    try:
        scheme, iterations, salt_hex, _ = hashed.split("$", 3)
        return (
            scheme != PASSWORD_SCHEME
            or int(iterations) < PASSWORD_ITERATIONS
            or len(bytes.fromhex(salt_hex)) < 16
        )
    except (AttributeError, TypeError, ValueError):
        return True


def new_secret() -> str:
    """服务凭据原文。只在签发响应里出现一次，库里只存摘要。"""
    return secrets.token_urlsafe(32)


def hash_secret(raw: str) -> str:
    return hashlib.sha256(f"{settings.secret_key}:{raw}".encode()).hexdigest()


def verify_secret(raw: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_secret(raw), hashed)


def issue_token(subject: str, role: str, org_id: str = "") -> str:
    payload = {
        "sub": subject,
        "role": role,
        "org": org_id,
        "exp": now() + timedelta(minutes=settings.access_ttl_min),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise Unauthenticated("会话无效，请重新登录") from exc
