"""账户与会话（v0.7.0）：邮箱+密码注册登录、不透明令牌、会员状态、AI 配额。

安全要点（见 docs/MEMBERSHIP.md §2/§7）：
- 密码：PBKDF2-HMAC-SHA256，每用户独立随机 salt，200k 次迭代（标准库实现，零新依赖）；
  校验用 hmac.compare_digest 常数时间比较。
- 会话：登录后下发 ``secrets.token_urlsafe(32)`` 不透明令牌；库中只存 SHA-256 哈希，
  明文只返回一次；登出即删（可吊销）。请求头 ``Authorization: Bearer <token>``。
- 登录失败统一报「邮箱或密码错误」，不区分账号是否存在（防邮箱枚举）。
- AI 配额：游客按 IP、登录用户按 user_id 计每日次数；PRO 不限日次数。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import datetime

from fastapi import HTTPException, Request

from . import db
from .config import settings

_PBKDF2_ROUNDS = 200_000
_PBKDF2_ALGO = "sha256"
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_PASSWORD_MIN = 8
_PASSWORD_MAX = 128
_TOKEN_SEEN_INTERVAL = 60  # last_seen 更新节流（秒），避免每请求写库


# ---------------- 错误 ----------------
class AuthError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "auth_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# ---------------- 邮箱 / 密码 ----------------
def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise AuthError("邮箱格式不正确", status=400, code="invalid_email")
    return email


def validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < _PASSWORD_MIN:
        raise AuthError(f"密码至少 {_PASSWORD_MIN} 位", status=400, code="weak_password")
    if len(password) > _PASSWORD_MAX:
        raise AuthError(f"密码最多 {_PASSWORD_MAX} 位", status=400, code="weak_password")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_{_PBKDF2_ALGO}${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds_s, salt_hex, hash_hex = stored.split("$")
        if scheme != f"pbkdf2_{_PBKDF2_ALGO}":
            return False
        digest = hashlib.pbkdf2_hmac(
            _PBKDF2_ALGO, password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds_s)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------------- 用户 ----------------
def create_user(email: str, password: str) -> dict:
    """注册；邮箱重复抛 AuthError。成功自动建会话，返回 (user_row, 明文 token)。"""
    email = normalize_email(email)
    validate_password(password)
    user_id = uuid.uuid4().hex
    now = db.now_ts()
    try:
        db.execute(
            "INSERT INTO users (user_id, email, password_hash, member_expire_at, created_at)"
            " VALUES (?, ?, ?, 0, ?)",
            (user_id, email, hash_password(password), now),
        )
    except Exception as exc:  # sqlite3.IntegrityError：email UNIQUE 冲突
        if "UNIQUE" in str(exc):
            raise AuthError("该邮箱已注册，请直接登录", status=409, code="email_taken")
        raise
    token = issue_token(user_id)
    row = get_user(user_id)
    return {"user": row, "token": token}


def authenticate(email: str, password: str) -> dict:
    """登录；账号不存在或密码错统一报同一文案（防枚举）。"""
    try:
        email = normalize_email(email)
    except AuthError:
        # 邮箱格式非法也给同样的模糊提示，不暴露「该邮箱是否存在」
        raise AuthError("邮箱或密码错误", status=401, code="invalid_credentials")
    row = db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if row is None or not verify_password(password, row["password_hash"]):
        raise AuthError("邮箱或密码错误", status=401, code="invalid_credentials")
    token = issue_token(row["user_id"])
    return {"user": dict(row), "token": token}  # dict() 转换：sqlite3.Row 无 .get()


def get_user(user_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM users WHERE user_id=?", (user_id,))
    return dict(row) if row else None


def is_pro(user: dict | None, at: int | None = None) -> bool:
    return bool(user) and int(user.get("member_expire_at") or 0) > (at if at is not None else db.now_ts())


# ---------------- 令牌（会话） ----------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = db.now_ts()
    db.execute(
        "INSERT INTO auth_tokens (token_hash, user_id, created_at, last_seen_at) VALUES (?, ?, ?, ?)",
        (_hash_token(token), user_id, now, now),
    )
    return token


def revoke_token(token: str) -> None:
    db.execute("DELETE FROM auth_tokens WHERE token_hash=?", (_hash_token(token),))


def user_from_request(request: Request) -> dict | None:
    """从 Authorization: Bearer 解析当前用户；无/无效令牌返回 None（不报错）。"""
    auth = request.headers.get("authorization") or request.headers.get("x-auth-token") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
    if not token:
        return None
    row = db.query_one(
        "SELECT u.* FROM auth_tokens t JOIN users u ON u.user_id=t.user_id"
        " WHERE t.token_hash=?",
        (_hash_token(token),),
    )
    if row is None:
        return None
    # 节流更新活跃时间（距上次超过 _TOKEN_SEEN_INTERVAL 才写库）
    _touch_token(_hash_token(token), db.now_ts())
    return dict(row)


def _touch_token(token_hash: str, now: int) -> None:
    db.execute(
        "UPDATE auth_tokens SET last_seen_at=? WHERE token_hash=? AND last_seen_at < ?",
        (now, token_hash, now - _TOKEN_SEEN_INTERVAL),
    )


def require_user(request: Request) -> dict:
    """需要登录的端点用；未登录/令牌失效 → 401。"""
    user = user_from_request(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={"ok": False, "error": "请先登录后再操作", "code": "login_required"},
        )
    return user


def bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("x-auth-token") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
    return token


# ---------------- AI 每日配额 ----------------
def _ymd(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def quota_subject(user: dict | None, ip: str) -> str:
    return f"u:{user['user_id']}" if user else f"ip:{ip}"


def ai_used_today(subject: str) -> int:
    row = db.query_one(
        "SELECT calls FROM ai_usage WHERE subject=? AND ymd=?",
        (subject, _ymd(db.now_ts())),
    )
    return int(row["calls"]) if row else 0


def consume_ai_call(subject: str) -> None:
    """成功调用后计数（UPSERT）。仅对非 PRO 调用。"""
    ymd = _ymd(db.now_ts())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO ai_usage (subject, ymd, calls) VALUES (?, ?, 1)"
            " ON CONFLICT(subject, ymd) DO UPDATE SET calls=calls+1",
            (subject, ymd),
        )


def check_ai_quota(user: dict | None, ip: str) -> None:
    """非 PRO 每日 AI 次数闸门；超限抛 403 pro_required。PRO 直接放行。"""
    if is_pro(user):
        return
    subject = quota_subject(user, ip)
    used = ai_used_today(subject)
    if used >= settings.ai_free_daily_limit:
        raise HTTPException(
            status_code=403,
            detail={
                "ok": False,
                "error": f"免费额度已用完（每日 {settings.ai_free_daily_limit} 次 AI 功能），升级 PRO 不限次",
                "code": "pro_required",
                "ai_used_today": used,
                "ai_daily_limit": settings.ai_free_daily_limit,
            },
        )


def public_user(user: dict | None, ip: str = "") -> dict | None:
    """对外用户视图：不含 user_id（内部用）/密码哈希；附当日 AI 配额。"""
    if user is None:
        return None
    subject = quota_subject(user, ip) if ip else ""
    used = ai_used_today(subject) if subject else 0
    expire = int(user.get("member_expire_at") or 0)
    return {
        "email": user["email"],
        "member_expire_at": expire if expire > 0 else None,
        "is_pro": is_pro(user),
        "ai_used_today": used,
        "ai_daily_limit": settings.ai_free_daily_limit,
    }
