"""Stripe 支付与会员履约（v0.7.0）。

链路（见 docs/MEMBERSHIP.md §3）：
  下单（先建 pending 订单）→ Stripe Checkout 托管收银台 → Webhook 验签 → 幂等履约（开通/叠加时长）。

关键安全/幂等设计：
- Webhook 必须用 Stripe 签名密钥 + **原始请求体**验签（``client.construct_event``）；
- 事件按 event_id 去重（webhook_events 主键，重复/乱序投递短路）；
- 履约在单事务内做「订单 pending→paid 条件更新」，只有影响行数=1 才叠加会员时长，
  因此同一事件重放、并发回调都不会重复开通/重复加天数；
- 金额/天数只取自服务端套餐表与验签后的 Stripe 回执，前端无法指定金额。
"""
from __future__ import annotations

import logging
import uuid

from . import db
from .config import settings

logger = logging.getLogger("app.billing")


class BillingError(Exception):
    """业务可预期错误（未配置/套餐非法等）。"""

    def __init__(self, message: str, status: int = 400, code: str = "billing_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# ---------------- Stripe 客户端（延迟初始化） ----------------
_client = None


def get_client():
    """返回 stripe.StripeClient（v8+ 实例化方式）。延迟 import，未装 SDK/未配密钥时给出明确错误。"""
    global _client
    if not settings.billing_enabled:
        raise BillingError("支付功能未开通，请稍后再试", status=503, code="billing_disabled")
    if _client is None:
        try:
            import stripe  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise BillingError("支付服务暂不可用", status=503, code="billing_disabled") from exc
        _client = stripe.StripeClient(settings.stripe_secret_key)
    return _client


# ---------------- 下单 ----------------
def create_checkout(user: dict, plan_key: str, base_url: str) -> dict:
    """创建本地 pending 订单 + Stripe Checkout Session，返回 {order_id, url}。"""
    plan = settings.plans.get(plan_key)
    if not plan or not plan.get("price_id"):
        raise BillingError("套餐不存在或未配置", status=400, code="invalid_plan")

    client = get_client()
    order_id = uuid.uuid4().hex
    now = db.now_ts()
    db.execute(
        "INSERT INTO orders (order_id, user_id, plan_key, status, created_at)"
        " VALUES (?, ?, ?, 'pending', ?)",
        (order_id, user["user_id"], plan_key, now),
    )

    base_url = base_url.rstrip("/")
    try:
        session = client.checkout.sessions.create(
            params={
                "mode": "payment",
                "line_items": [{"price": plan["price_id"], "quantity": 1}],
                "client_reference_id": order_id,
                "customer_email": user["email"],
                "success_url": f"{base_url}/?pay=success#pricing",
                "cancel_url": f"{base_url}/?pay=cancel#pricing",
            }
        )
    except Exception:
        logger.exception("stripe checkout create failed order_id=%s", order_id)
        raise BillingError("创建支付失败，请稍后重试", status=502, code="checkout_failed")

    session_id = str(session.id)
    db.execute(
        "UPDATE orders SET stripe_session_id=? WHERE order_id=?",
        (session_id, order_id),
    )
    return {"order_id": order_id, "url": session.url}


# ---------------- Webhook 验签与去重 ----------------
def construct_event(raw_body: bytes, sig_header: str):
    """用签名密钥验证并解析事件；签名非法/密钥缺失会抛异常（路由层转 400）。"""
    secret = settings.stripe_webhook_secret
    if not secret:
        raise BillingError("Webhook 未配置", status=503, code="billing_disabled")
    client = get_client()
    return client.construct_event(raw_body, sig_header, secret)


def is_duplicate_event(event_id: str, event_type: str) -> bool:
    """登记事件；首次出现返回 False，已处理过（重复投递）返回 True。

    去重登记与履约放在同一请求内：先「条件插入」占住 event_id（主键冲突=重复），
    再执行履约；履约失败则让请求返回非 200 触发 Stripe 重试，并删掉占位记录，
    保证重试时能重新处理。
    """
    try:
        db.execute(
            "INSERT INTO webhook_events (event_id, event_type, received_at) VALUES (?, ?, ?)",
            (event_id, event_type, db.now_ts()),
        )
        return False
    except Exception as exc:
        if "UNIQUE" in str(exc):
            return True
        raise


def forget_event(event_id: str) -> None:
    """履约失败时移除事件占位，允许 Stripe 重试后重新处理。"""
    db.execute("DELETE FROM webhook_events WHERE event_id=?", (event_id,))


# ---------------- 事件分发与履约 ----------------
def handle_event(event) -> None:
    event_type = str(event.type)
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        fulfill_order(event.data.object)
    elif event_type == "checkout.session.async_payment_failed":
        _mark_status(event.data.object, "failed")
    elif event_type == "checkout.session.expired":
        _mark_status(event.data.object, "expired")
    else:
        logger.info("unhandled stripe event type=%s", event_type)


def _session_field(session, name: str, default=None):
    """StripeObject / dict 双兼容取值。

    ⚠️ stripe v15 的 StripeObject 没有 .get()（实测），静默 AttributeError 会让
    client_reference_id 永远取不到 → 履约必挂。属性访问与下标访问双兜底。
    """
    if isinstance(session, dict):
        return session.get(name, default)
    value = getattr(session, name, None)
    if value is None:
        try:
            value = session[name]
        except (KeyError, TypeError, IndexError):
            return default
    return value


def _mark_status(session, status: str) -> None:
    session_id = str(_session_field(session, "id", ""))
    order_id = _session_field(session, "client_reference_id")
    if order_id:
        db.execute(
            "UPDATE orders SET status=? WHERE order_id=? AND status='pending'",
            (status, str(order_id)),
        )
    elif session_id:
        db.execute(
            "UPDATE orders SET status=? WHERE stripe_session_id=? AND status='pending'",
            (status, session_id),
        )


def fulfill_order(session) -> None:
    """支付成功 → 开通/叠加会员时长。幂等核心（见模块文档）。

    事务内：
      1) orders: pending→paid 条件更新，影响行数=0 说明已处理，短路（不重复加时长）；
      2) users: member_expire_at = max(当前到期, 现在) + 套餐天数。
    """
    order_id = _session_field(session, "client_reference_id")
    if not order_id:
        raise BillingError("session missing client_reference_id", status=400, code="bad_session")
    order_id = str(order_id)

    payment_status = _session_field(session, "payment_status")
    if payment_status != "paid":
        # 未真正付款（异步未完成等）不开通；等 async_payment_succeeded 再履约
        logger.info("skip fulfill order=%s payment_status=%s", order_id, payment_status)
        return

    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE orders SET status='paid', paid_at=?, amount_cents=?, currency=?"
            " WHERE order_id=? AND status='pending'",
            (
                db.now_ts(),
                int(_session_field(session, "amount_total", 0) or 0),
                str(_session_field(session, "currency", "") or ""),
                order_id,
            ),
        )
        if cur.rowcount == 0:
            # 已支付/已失效：重复事件或并发回调，绝不重复叠加时长
            logger.info("order already processed, skip fulfillment order=%s", order_id)
            return

        row = conn.execute(
            "SELECT user_id, plan_key FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if row is None:  # 理论不可能
            raise BillingError("order vanished", status=500, code="internal")
        plan = settings.plans.get(row["plan_key"])
        if not plan:
            raise BillingError("unknown plan on order", status=500, code="internal")

        now = db.now_ts()
        conn.execute(
            "UPDATE users SET member_expire_at = MAX(COALESCE(member_expire_at, 0), ?) + ?"
            " WHERE user_id=?",
            (now, int(plan["days"]) * 86400, row["user_id"]),
        )
    logger.info("membership granted order=%s plan=%s", order_id, row["plan_key"])


# ---------------- 查询 ----------------
def list_orders(user_id: str) -> list[dict]:
    rows = db.query_all(
        "SELECT order_id, plan_key, amount_cents, currency, status, created_at, paid_at"
        " FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
        (user_id,),
    )
    return [dict(r) for r in rows]
