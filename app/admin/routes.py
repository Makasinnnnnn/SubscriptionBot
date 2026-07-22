import html
import secrets
from collections.abc import Iterable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.rate_limit import SlidingWindowLimiter
from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import (
    AuditLog,
    Channel,
    Payment,
    Plan,
    Subscription,
    User,
    WebhookEvent,
)
from app.db.session import get_session

router = APIRouter(prefix="/admin/web", tags=["web-admin"])
security = HTTPBasic(auto_error=False)
auth_limiter = SlidingWindowLimiter()


async def require_web_admin(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
) -> None:
    settings = get_settings()
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="Web admin is not configured")
    provided_username = credentials.username if credentials is not None else ""
    provided_password = credentials.password if credentials is not None else ""
    client_host = request.client.host if request.client else "unknown"
    rate_key = f"{client_host}:{provided_username[:128]}"
    if await auth_limiter.is_blocked(rate_key, limit=10, window_seconds=300):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many authentication attempts",
            headers={"Retry-After": "300"},
        )
    username_ok = secrets.compare_digest(
        provided_username.encode(),
        settings.admin_username.encode(),
    )
    password_ok = secrets.compare_digest(
        provided_password.encode(),
        settings.admin_password.encode(),
    )
    if not username_ok or not password_ok:
        await auth_limiter.allow(rate_key, limit=10, window_seconds=300)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    await auth_limiter.reset(rate_key)


AdminAuth = Annotated[None, Depends(require_web_admin)]
DbSession = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_class=HTMLResponse)
async def dashboard(_: AdminAuth, session: DbSession) -> HTMLResponse:
    users = await session.scalar(select(func.count(User.id))) or 0
    active_subscriptions = await session.scalar(
        select(func.count(Subscription.id)).where(
            Subscription.status == SubscriptionStatus.ACTIVE
        )
    ) or 0
    paid_payments = await session.scalar(
        select(func.count(Payment.id)).where(Payment.status == PaymentStatus.PAID)
    ) or 0
    failed_webhooks = await session.scalar(
        select(func.count(WebhookEvent.id)).where(WebhookEvent.error.is_not(None))
    ) or 0
    body = (
        '<section class="metrics">'
        f'{_metric("Пользователи", users)}'
        f'{_metric("Активные подписки", active_subscriptions)}'
        f'{_metric("Успешные платежи", paid_payments)}'
        f'{_metric("Webhook с ошибкой", failed_webhooks)}'
        "</section>"
        '<section class="panel"><h2>Быстрые действия</h2>'
        "<p>Изменения доступа и настроек выполняются командами в Telegram-админке. "
        "Web-интерфейс оставлен обзорным, чтобы случайный клик не отозвал подписку или платёж.</p>"
        "</section>"
    )
    return HTMLResponse(_layout("Обзор", body, "dashboard"))


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    _: AdminAuth,
    session: DbSession,
    limit: int = Query(100, ge=1, le=500),
) -> HTMLResponse:
    users = list(await session.scalars(select(User).order_by(User.id.desc()).limit(limit)))
    rows = [
        (user.id, user.telegram_id, f"@{user.username}" if user.username else "—", user.first_name or "—")
        for user in users
    ]
    return HTMLResponse(
        _layout(
            "Пользователи",
            _table(("ID", "Telegram ID", "Username", "Имя"), rows),
            "users",
        )
    )


@router.get("/channels", response_class=HTMLResponse)
async def channels_page(_: AdminAuth, session: DbSession) -> HTMLResponse:
    channels = list(await session.scalars(select(Channel).order_by(Channel.id)))
    rows = [
        (channel.id, channel.telegram_chat_id, channel.title, _state(channel.is_active))
        for channel in channels
    ]
    return HTMLResponse(
        _layout("Каналы", _table(("ID", "Telegram chat", "Название", "Статус"), rows), "channels")
    )


@router.get("/plans", response_class=HTMLResponse)
async def plans_page(_: AdminAuth, session: DbSession) -> HTMLResponse:
    plans = list(
        await session.scalars(
            select(Plan).options(selectinload(Plan.channel)).order_by(Plan.id.desc())
        )
    )
    rows = [
        (
            plan.id,
            plan.channel.title,
            plan.title,
            plan.duration_days,
            f"{plan.price} {plan.currency}",
            _state(plan.is_active),
        )
        for plan in plans
    ]
    return HTMLResponse(
        _layout(
            "Тарифы",
            _table(("ID", "Канал", "Название", "Дни", "Цена", "Статус"), rows),
            "plans",
        )
    )


@router.get("/payments", response_class=HTMLResponse)
async def payments_page(
    _: AdminAuth,
    session: DbSession,
    limit: int = Query(100, ge=1, le=500),
) -> HTMLResponse:
    payments = list(
        await session.scalars(
            select(Payment)
            .options(selectinload(Payment.user), selectinload(Payment.plan))
            .order_by(Payment.id.desc())
            .limit(limit)
        )
    )
    rows = [
        (
            payment.id,
            payment.user.telegram_id,
            payment.plan.title,
            payment.provider,
            f"{payment.amount} {payment.currency}",
            payment.status.value,
            _date(payment.created_at),
        )
        for payment in payments
    ]
    return HTMLResponse(
        _layout(
            "Платежи",
            _table(("ID", "Telegram ID", "Тариф", "Провайдер", "Сумма", "Статус", "Создан"), rows),
            "payments",
        )
    )


@router.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(
    _: AdminAuth,
    session: DbSession,
    limit: int = Query(100, ge=1, le=500),
) -> HTMLResponse:
    subscriptions = list(
        await session.scalars(
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.channel))
            .order_by(Subscription.id.desc())
            .limit(limit)
        )
    )
    rows = [
        (
            subscription.id,
            subscription.user.telegram_id,
            subscription.channel.title,
            subscription.status.value,
            subscription.source,
            _date(subscription.expires_at),
        )
        for subscription in subscriptions
    ]
    return HTMLResponse(
        _layout(
            "Подписки",
            _table(("ID", "Telegram ID", "Канал", "Статус", "Источник", "До"), rows),
            "subscriptions",
        )
    )


@router.get("/webhooks", response_class=HTMLResponse)
async def webhooks_page(
    _: AdminAuth,
    session: DbSession,
    limit: int = Query(100, ge=1, le=500),
) -> HTMLResponse:
    events = list(
        await session.scalars(
            select(WebhookEvent).order_by(WebhookEvent.id.desc()).limit(limit)
        )
    )
    rows = [
        (
            event.id,
            event.provider,
            event.event_type,
            event.error or ("processed" if event.processed_at else "pending"),
            _date(event.created_at),
        )
        for event in events
    ]
    return HTMLResponse(
        _layout(
            "Webhook",
            _table(("ID", "Провайдер", "Событие", "Результат", "Создано"), rows),
            "webhooks",
        )
    )


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    _: AdminAuth,
    session: DbSession,
    limit: int = Query(100, ge=1, le=500),
) -> HTMLResponse:
    logs = list(
        await session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
    )
    rows = [
        (
            log.id,
            log.actor_telegram_id or "system",
            log.action,
            f"{log.target_type}:{log.target_id or '—'}",
            _date(log.created_at),
        )
        for log in logs
    ]
    return HTMLResponse(
        _layout(
            "Журнал действий",
            _table(("ID", "Кто", "Действие", "Цель", "Время"), rows),
            "audit",
        )
    )


def _layout(title: str, body: str, active: str) -> str:
    links = [
        ("dashboard", "/admin/web", "Обзор"),
        ("users", "/admin/web/users", "Пользователи"),
        ("channels", "/admin/web/channels", "Каналы"),
        ("plans", "/admin/web/plans", "Тарифы"),
        ("payments", "/admin/web/payments", "Платежи"),
        ("subscriptions", "/admin/web/subscriptions", "Подписки"),
        ("webhooks", "/admin/web/webhooks", "Webhook"),
        ("audit", "/admin/web/audit", "Audit log"),
    ]
    navigation = "".join(
        f'<a class="{"active" if key == active else ""}" href="{href}">{label}</a>'
        for key, href, label in links
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · PropGate Admin</title>
<style>
:root {{ color-scheme: light; --ink:#17191d; --muted:#69707a; --line:#dfe3e8; --paper:#fff; --wash:#f4f6f8; --accent:#16885f; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--wash); color:var(--ink); font:14px/1.45 Arial,sans-serif; }}
header {{ height:56px; display:flex; align-items:center; padding:0 24px; background:#17212b; color:#fff; border-bottom:3px solid var(--accent); }}
header strong {{ font-size:18px; }}
.shell {{ display:grid; grid-template-columns:210px minmax(0,1fr); min-height:calc(100vh - 56px); }}
nav {{ padding:16px 10px; background:#fff; border-right:1px solid var(--line); }}
nav a {{ display:block; padding:9px 12px; color:#363b42; text-decoration:none; border-radius:4px; margin-bottom:2px; }}
nav a:hover,nav a.active {{ background:#e9f6f0; color:#0b704b; }}
main {{ min-width:0; padding:24px; }}
h1 {{ font-size:24px; margin:0 0 18px; }} h2 {{ font-size:16px; margin:0 0 8px; }}
.metrics {{ display:grid; grid-template-columns:repeat(4,minmax(140px,1fr)); gap:12px; margin-bottom:18px; }}
.metric,.panel {{ background:var(--paper); border:1px solid var(--line); border-radius:6px; padding:16px; }}
.metric span {{ display:block; color:var(--muted); margin-bottom:6px; }} .metric b {{ font-size:25px; }}
.table-wrap {{ overflow:auto; background:#fff; border:1px solid var(--line); border-radius:6px; }}
table {{ width:100%; border-collapse:collapse; white-space:nowrap; }} th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); }}
th {{ background:#f8f9fa; color:#525962; font-size:12px; text-transform:uppercase; }} tr:last-child td {{ border-bottom:0; }}
.ok {{ color:#087a50; }} .off {{ color:#9a3d2f; }}
@media (max-width:800px) {{ .shell {{ grid-template-columns:1fr; }} nav {{ display:flex; overflow:auto; border-right:0; border-bottom:1px solid var(--line); }} nav a {{ white-space:nowrap; }} main {{ padding:16px; }} .metrics {{ grid-template-columns:repeat(2,1fr); }} }}
</style>
</head>
<body><header><strong>PropGate Admin</strong></header><div class="shell"><nav>{navigation}</nav><main><h1>{html.escape(title)}</h1>{body}</main></div></body>
</html>"""


def _table(headers: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> str:
    head = "".join(f"<th>{html.escape(value)}</th>" for value in headers)
    rendered_rows = []
    for row in rows:
        rendered_rows.append(
            "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>"
        )
    if not rendered_rows:
        rendered_rows.append(f'<tr><td colspan="{len(headers)}">Нет данных</td></tr>')
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rendered_rows)}</tbody></table></div>'


def _metric(label: str, value: object) -> str:
    return f'<div class="metric"><span>{html.escape(label)}</span><b>{html.escape(str(value))}</b></div>'


def _date(value: object) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if hasattr(value, "strftime") else "—"


def _state(active: bool) -> str:
    return "active" if active else "disabled"
