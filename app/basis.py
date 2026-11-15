"""建议依据令牌。

页面拿到的匹配建议必须和服务端某个**已提交状态**绑定：令牌里带
各员工的开放数据版本号（employee_revisions 只增不减），并用服务端
密钥做 HMAC 签名，页面无法伪造/改写。

确认时在 `BEGIN IMMEDIATE` 写锁内重新读取版本并逐员工比对：
- 同员工在建议生成后补录/确认过 → 版本不一致 → 409，提示刷新；
- 其他员工变化 → 不参与本组比对 → 不影响确认。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Dict, Mapping

TOKEN_VERSION = 1


class InvalidBasis(ValueError):
    """令牌缺失、格式错误或签名不合法。"""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _signature(secret: str, body: str) -> str:
    return _b64encode(
        hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    )


def issue(secret: str, revisions: Mapping[str, int]) -> str:
    payload = {
        "v": TOKEN_VERSION,
        "nonce": secrets.token_hex(8),
        "revs": {str(e): int(r) for e, r in revisions.items()},
    }
    body = _b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return f"{body}.{_signature(secret, body)}"


def verify(secret: str, token: str) -> Dict[str, int]:
    if not isinstance(token, str) or token.count(".") != 1:
        raise InvalidBasis("建议依据格式不正确")
    body, signature = token.split(".", 1)
    expected = _signature(secret, body)
    if not hmac.compare_digest(signature, expected):
        raise InvalidBasis("建议依据签名不合法")
    try:
        payload = json.loads(_b64decode(body))
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都视为非法令牌
        raise InvalidBasis("建议依据无法解析") from exc
    if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION:
        raise InvalidBasis("建议依据版本不受支持")
    revs = payload.get("revs")
    if not isinstance(revs, dict):
        raise InvalidBasis("建议依据缺少版本信息")
    result: Dict[str, int] = {}
    for employee_no, revision in revs.items():
        if not isinstance(employee_no, str) or isinstance(revision, bool):
            raise InvalidBasis("建议依据版本信息不合法")
        if not isinstance(revision, int) or revision < 0:
            raise InvalidBasis("建议依据版本信息不合法")
        result[employee_no] = revision
    return result
