"""大模型用量统计。

数据源是 ``llm_usage_records`` 账本（见 :mod:`app.observability.usage_ledger`），
一行 = 一次模型调用。这里只做聚合，不产生业务副作用。

口径：

* **天数分桶按请求方时区**，不按 UTC —— 用户看到的「10 月 2 日」应当是本地的
  10 月 2 日。账本统一存 UTC naive，因此查询与分桶都带 ``tz_offset_minutes``。
* **管理员看全租户，成员只看自己**。返回 ``scope`` 让前端能说明当前视野，
  成员不会被误导成「公司整体只用了一点点」。
* 会话被删除不会影响这里的数字：账本不随会话级联删除。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import AgentProfile, LlmUsageRecord, User
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import is_admin_user
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/usage",
    tags=["enterprise:usage"],
    dependencies=[Depends(get_current_user)],
)

DEFAULT_RANGE_DAYS = 30
MAX_RANGE_DAYS = 366
# 账本时间列是 UTC naive，分桶要按请求方时区；前端传 -new Date().getTimezoneOffset()
DEFAULT_TZ_OFFSET_MINUTES = 480
UNATTRIBUTED_USER_KEY = "__unattributed__"
UNATTRIBUTED_AGENT_KEY = "__unattributed__"

_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
)


@dataclass(frozen=True)
class _Range:
    start_day: date
    end_day: date
    tz_offset_minutes: int

    @property
    def start_utc(self) -> datetime:
        return _local_day_start_utc(self.start_day, self.tz_offset_minutes)

    @property
    def end_utc_exclusive(self) -> datetime:
        return _local_day_start_utc(
            self.end_day + timedelta(days=1), self.tz_offset_minutes
        )


def _local_day_start_utc(day: date, tz_offset_minutes: int) -> datetime:
    """本地某日 00:00 对应的 UTC naive 时刻。"""
    return datetime.combine(day, time.min) - timedelta(minutes=tz_offset_minutes)


def _resolve_range(
    start: str | None,
    end: str | None,
    tz_offset_minutes: int,
) -> _Range:
    today = (datetime.now(UTC) + timedelta(minutes=tz_offset_minutes)).date()
    end_day = _parse_day(end) or today
    start_day = _parse_day(start) or end_day - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if start_day > end_day:
        start_day, end_day = end_day, start_day
    if (end_day - start_day).days >= MAX_RANGE_DAYS:
        start_day = end_day - timedelta(days=MAX_RANGE_DAYS - 1)
    return _Range(start_day=start_day, end_day=end_day, tz_offset_minutes=tz_offset_minutes)


def _parse_day(raw: str | None) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


@router.get("/stats")
def get_usage_stats(
    tenant_id: str = Query(...),
    start: str | None = Query(None),
    end: str | None = Query(None),
    tz_offset_minutes: int = Query(DEFAULT_TZ_OFFSET_MINUTES, ge=-1440, le=1440),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    ensure_current_user_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    window = _resolve_range(start, end, tz_offset_minutes)
    admin_view = is_admin_user(current_user)

    rows = _usage_rows(db, tenant_id, window, None if admin_view else current_user.id)
    return {
        "range": {
            "start": window.start_day.isoformat(),
            "end": window.end_day.isoformat(),
            "days": (window.end_day - window.start_day).days + 1,
            "timezone_offset_minutes": window.tz_offset_minutes,
        },
        "scope": "tenant" if admin_view else "self",
        "totals": _totals(rows),
        "daily": _daily(rows, window),
        "by_agent": _named_breakdown(
            _breakdown(rows, "agent_id"),
            _agent_names(db, tenant_id, rows, "agent_id"),
            UNATTRIBUTED_AGENT_KEY,
        ),
        "by_user": _named_breakdown(
            _breakdown(rows, "user_id"),
            _user_names(db, tenant_id, rows, "user_id"),
            UNATTRIBUTED_USER_KEY,
        ),
        "by_model": _breakdown(rows, "model_name"),
        "by_operation": _breakdown(rows, "operation"),
    }


def _usage_rows(
    db: Session,
    tenant_id: str,
    window: _Range,
    user_id: str | None,
) -> list[LlmUsageRecord]:
    statement = select(LlmUsageRecord).where(
        LlmUsageRecord.tenant_id == tenant_id,
        LlmUsageRecord.created_at >= window.start_utc,
        LlmUsageRecord.created_at < window.end_utc_exclusive,
    )
    if user_id is not None:
        statement = statement.where(LlmUsageRecord.user_id == user_id)
    return list(db.exec(statement).all())


def _totals(rows: list[LlmUsageRecord]) -> dict[str, Any]:
    durations = [row.duration_ms for row in rows if row.duration_ms]
    totals: dict[str, Any] = {"calls": len(rows)}
    for column in _TOKEN_COLUMNS:
        totals[column] = sum(getattr(row, column) or 0 for row in rows)
    totals["avg_duration_ms"] = round(sum(durations) / len(durations), 3) if durations else 0.0
    return totals


def _daily(rows: list[LlmUsageRecord], window: _Range) -> list[dict[str, Any]]:
    """按本地日期补零输出，前端不必自己补空档。"""
    buckets: dict[date, dict[str, Any]] = {}
    for row in rows:
        day = (row.created_at + timedelta(minutes=window.tz_offset_minutes)).date()
        bucket = buckets.setdefault(day, {"calls": 0, **{c: 0 for c in _TOKEN_COLUMNS}})
        bucket["calls"] += 1
        for column in _TOKEN_COLUMNS:
            bucket[column] += getattr(row, column) or 0

    series: list[dict[str, Any]] = []
    current = window.start_day
    while current <= window.end_day:
        bucket = buckets.get(current, {"calls": 0, **{c: 0 for c in _TOKEN_COLUMNS}})
        series.append({"date": current.isoformat(), **bucket})
        current += timedelta(days=1)
    return series


def _breakdown(rows: list[LlmUsageRecord], field: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, **{c: 0 for c in _TOKEN_COLUMNS}}
    )
    for row in rows:
        key = str(getattr(row, field) or "").strip()
        bucket = buckets[key]
        bucket["calls"] += 1
        for column in _TOKEN_COLUMNS:
            bucket[column] += getattr(row, column) or 0
    return [
        {"key": key, **value}
        for key, value in sorted(
            buckets.items(),
            key=lambda item: (-int(item[1]["total_tokens"]), -int(item[1]["calls"]), item[0]),
        )
    ]


def _named_breakdown(
    breakdown: list[dict[str, Any]],
    names: dict[str, str],
    unattributed_key: str,
) -> list[dict[str, Any]]:
    named: list[dict[str, Any]] = []
    for entry in breakdown:
        key = str(entry["key"])
        if not key:
            named.append({**entry, "key": unattributed_key, "name": None})
            continue
        named.append({**entry, "name": names.get(key) or None})
    return named


def _agent_names(
    db: Session,
    tenant_id: str,
    rows: list[LlmUsageRecord],
    field: str,
) -> dict[str, str]:
    ids = {str(getattr(row, field)) for row in rows if getattr(row, field)}
    if not ids:
        return {}
    return {
        row.id: row.name
        for row in db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id, AgentProfile.id.in_(ids)
            )
        ).all()
    }


def _user_names(
    db: Session,
    tenant_id: str,
    rows: list[LlmUsageRecord],
    field: str,
) -> dict[str, str]:
    ids = {str(getattr(row, field)) for row in rows if getattr(row, field)}
    if not ids:
        return {}
    return {
        row.id: (row.display_name or row.username)
        for row in db.exec(
            select(User).where(User.tenant_id == tenant_id, User.id.in_(ids))
        ).all()
    }


@router.get("/operations")
def list_usage_operations(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[str]:
    """出现过的调用场景，供前端筛选下拉使用。"""
    ensure_current_user_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    statement = (
        select(LlmUsageRecord.operation, func.count())
        .where(LlmUsageRecord.tenant_id == tenant_id)
        .group_by(LlmUsageRecord.operation)
    )
    if not is_admin_user(current_user):
        statement = statement.where(LlmUsageRecord.user_id == current_user.id)
    return [row[0] for row in db.exec(statement).all() if row[0]]
