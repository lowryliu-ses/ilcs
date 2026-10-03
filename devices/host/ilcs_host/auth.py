"""ILCS 调用的认证：SiLA 核心特性 AuthorizationService 的 AccessToken 元数据，令牌预共享、宿主本地校验。

三组设备特性的全部命令与属性都要带令牌（缺了 sila2 直接拒绝）；SiLAService 不受约束，ILCS 先读它判断已实现特性。
令牌文件一行一个，`#` 开头的是注释；每个不少于 32 个字符。比对用常量时间。
"""
from __future__ import annotations

import hmac
from pathlib import Path

from sila2.features.authorizationservice import AuthorizationServiceBase, AuthorizationServiceFeature, InvalidAccessToken
from sila2.server import MetadataInterceptor

ACCESS_TOKEN = AuthorizationServiceFeature["AccessToken"]
MIN_TOKEN_LENGTH = 32


def read_tokens(path: Path) -> frozenset[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"令牌文件 {path} 读不了：{exc}") from exc
    tokens = frozenset(line.strip() for line in lines if line.strip() and not line.strip().startswith("#"))
    if not tokens:
        raise ValueError(f"令牌文件 {path} 里没有令牌")
    short = [token for token in tokens if len(token) < MIN_TOKEN_LENGTH]
    if short:
        raise ValueError(f"令牌文件 {path} 里有 {len(short)} 个令牌短于 {MIN_TOKEN_LENGTH} 个字符")
    return tokens


class TokenCheck(MetadataInterceptor):
    def __init__(self, tokens: frozenset[str]):
        super().__init__([ACCESS_TOKEN])
        self.tokens = tokens

    def intercept(self, parameters, metadata, target_call) -> None:
        token = str(metadata[ACCESS_TOKEN] or "")
        if not any(hmac.compare_digest(token.encode(), known.encode()) for known in self.tokens):
            raise InvalidAccessToken("令牌无效：这台设备服务不接受这个调用方")


class Authorization(AuthorizationServiceBase):
    def __init__(self, parent_server, protected):
        super().__init__(parent_server)
        self.protected = list(protected)

    def get_calls_affected_by_AccessToken(self):
        return self.protected
