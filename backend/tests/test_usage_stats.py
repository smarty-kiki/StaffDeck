"""大模型用量账本与统计接口。

覆盖三件事：账本写入（去重/非模型事件/归因）、历史回填的幂等性、
统计接口的口径（时区分桶、管理员 vs 成员视野、区间上限）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.usage import router as usage_router
from app.db import get_session
from app.db.models import AgentEvent, AgentProfile, ChatSession, LlmUsageRecord, Tenant, User
from app.observability.usage_ledger import (
    BACKFILL_MIGRATION_ID,
    migrate_backfill_llm_usage,
    record_llm_usage,
)
from app.security.auth import get_current_user

TENANT_ID = "tenant_demo"


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)
        yield db


def _seed(db: Session) -> None:
    db.add(Tenant(id=TENANT_ID, name="Demo"))
    db.add(
        User(
            id="user_admin",
            tenant_id=TENANT_ID,
            username="admin",
            display_name="管理员",
            role="admin",
            password_hash="x",
        )
    )
    db.add(
        User(
            id="user_member",
            tenant_id=TENANT_ID,
            username="member",
            display_name="成员",
            role="member",
            password_hash="x",
        )
    )
    db.add(
        AgentProfile(
            id="agent_a",
            tenant_id=TENANT_ID,
            owner_user_id="user_admin",
            name="客服助手",
        )
    )
    db.add(
        ChatSession(
            id="session_a",
            tenant_id=TENANT_ID,
            user_id="user_admin",
            agent_id="agent_a",
        )
    )
    db.add(
        ChatSession(
            id="session_b",
            tenant_id=TENANT_ID,
            user_id="user_member",
            agent_id="agent_a",
        )
    )
    db.commit()


def _usage_payload(**overrides) -> dict:
    payload = {
        "span_id": "span_default",
        "operation": "harness.task_action",
        "model_name": "deepseek-flash",
        "model": "deepseek-flash-prod",
        "status": "success",
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cached_input_tokens": 30,
        "duration_ms": 200.0,
        "finished_at": "2026-10-01T04:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _client(db: Session, user_id: str) -> TestClient:
    app = FastAPI()
    app.include_router(usage_router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return TestClient(app)


# --------------------------------------------------------------------------
# 账本写入
# --------------------------------------------------------------------------


def test_record_llm_usage_attributes_session_to_user_and_agent(session: Session) -> None:
    record_llm_usage(
        session,
        tenant_id=TENANT_ID,
        session_id="session_a",
        event_type="llm_call_finished",
        payload=_usage_payload(),
    )
    session.commit()

    row = session.exec(select(LlmUsageRecord)).one()
    assert row.user_id == "user_admin"
    assert row.agent_id == "agent_a"
    assert row.total_tokens == 120
    assert row.cached_input_tokens == 30
    # provider_model 记的是厂商模型 id，model_name 是配置里的展示名
    assert row.model_name == "deepseek-flash"
    assert row.provider_model == "deepseek-flash-prod"
    # finished_at 是 UTC 带偏移，落库统一成 UTC naive
    # 账本时间统一为 UTC naive（与 utc_now() 同口径），故直接与朴素时间比较
    assert row.created_at == datetime.fromisoformat("2026-10-01T04:00:00")


def test_record_llm_usage_ignores_replays_and_other_events(session: Session) -> None:
    first = record_llm_usage(
        session,
        tenant_id=TENANT_ID,
        session_id="session_a",
        event_type="llm_call_finished",
        payload=_usage_payload(),
    )
    replay = record_llm_usage(
        session,
        tenant_id=TENANT_ID,
        session_id="session_a",
        event_type="llm_call_finished",
        payload=_usage_payload(),
    )
    other_event = record_llm_usage(
        session,
        tenant_id=TENANT_ID,
        session_id="session_a",
        event_type="stream_delta",
        payload=_usage_payload(span_id="span_other"),
    )
    no_span = record_llm_usage(
        session,
        tenant_id=TENANT_ID,
        session_id="session_a",
        event_type="llm_call_finished",
        payload={"input_tokens": 5},
    )
    session.commit()

    assert first is not None
    assert replay is None
    assert other_event is None
    assert no_span is None
    assert len(session.exec(select(LlmUsageRecord)).all()) == 1


def test_record_llm_usage_keeps_unattributed_channel_sessions(session: Session) -> None:
    """渠道会话可能没有平台用户；用量仍要计数，只是归因列为空。"""
    record_llm_usage(
        session,
        tenant_id=TENANT_ID,
        session_id=None,
        event_type="llm_call_finished",
        payload=_usage_payload(span_id="span_channel"),
    )
    session.commit()

    row = session.exec(select(LlmUsageRecord)).one()
    assert row.session_id is None
    assert row.user_id is None
    assert row.agent_id is None


# --------------------------------------------------------------------------
# 历史回填
# --------------------------------------------------------------------------


def test_backfill_reads_legacy_events_and_is_idempotent() -> None:
    # 回填是「一次性迁移」形态：吃 SQLAlchemy Connection，在 init_db 的迁移管线里调用
    engine = _engine()
    with Session(engine) as db:
        _seed(db)
        db.add(
            AgentEvent(
                id="evt_1",
                tenant_id=TENANT_ID,
                session_id="session_a",
                event_type="llm_call_finished",
                payload_json=_usage_payload(span_id="span_legacy_1", total_tokens=500),
            )
        )
        db.add(
            AgentEvent(
                id="evt_2",
                tenant_id=TENANT_ID,
                session_id="session_a",
                event_type="stream_delta",
                payload_json={"span_id": "span_not_usage"},
            )
        )
        db.commit()

    with engine.begin() as conn:
        migrate_backfill_llm_usage(conn, {"llm_usage_records", "agent_events"})
        # 再跑一次：span_id 唯一 + 迁移守卫，行数不能涨
        migrate_backfill_llm_usage(conn, {"llm_usage_records", "agent_events"})

    with Session(engine) as db:
        rows = db.exec(select(LlmUsageRecord)).all()
        assert len(rows) == 1
        assert rows[0].span_id == "span_legacy_1"
        assert rows[0].source == "backfill"
        assert rows[0].user_id == "user_admin"
        assert rows[0].agent_id == "agent_a"
        assert rows[0].total_tokens == 500
        applied = db.connection().exec_driver_sql(
            "SELECT id FROM app_data_migrations WHERE id = ?", (BACKFILL_MIGRATION_ID,)
        ).fetchall()
        assert len(applied) == 1


def test_backfill_skips_when_tables_are_absent() -> None:
    engine = _engine()
    with engine.begin() as conn:
        # 表还没建（例如全新库在建表前跑到了这里）—— 直接跳过，不抛错
        migrate_backfill_llm_usage(conn, {"llm_usage_records"})


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


# --------------------------------------------------------------------------
# 统计接口
# --------------------------------------------------------------------------


def test_usage_stats_requires_authentication() -> None:
    app = FastAPI()
    app.include_router(usage_router)
    response = TestClient(app).get(
        "/api/enterprise/usage/stats", params={"tenant_id": TENANT_ID}
    )
    assert response.status_code == 401


def test_usage_stats_rejects_foreign_tenant(session: Session) -> None:
    response = _client(session, "user_admin").get(
        "/api/enterprise/usage/stats", params={"tenant_id": "tenant_other"}
    )
    assert response.status_code == 403


def test_admin_sees_whole_tenant_while_member_sees_only_self(session: Session) -> None:
    _record(session, "span_a", session_id="session_a", total_tokens=100)
    _record(session, "span_b", session_id="session_b", total_tokens=300)

    admin = _client(session, "user_admin").get(
        "/api/enterprise/usage/stats", params=_range()
    ).json()
    member = _client(session, "user_member").get(
        "/api/enterprise/usage/stats", params=_range()
    ).json()

    assert admin["scope"] == "tenant"
    assert admin["totals"]["calls"] == 2
    assert admin["totals"]["total_tokens"] == 400

    assert member["scope"] == "self"
    assert member["totals"]["calls"] == 1
    assert member["totals"]["total_tokens"] == 300
    assert [row["key"] for row in member["by_user"]] == ["user_member"]


def test_breakdown_resolves_agent_and_user_display_names(session: Session) -> None:
    _record(session, "span_a", session_id="session_a", total_tokens=100)

    body = _client(session, "user_admin").get(
        "/api/enterprise/usage/stats", params=_range()
    ).json()

    assert body["by_agent"][0]["name"] == "客服助手"
    assert body["by_agent"][0]["key"] == "agent_a"
    assert body["by_user"][0]["name"] == "管理员"
    assert body["by_model"][0]["key"] == "deepseek-flash"
    assert body["by_operation"][0]["key"] == "harness.task_action"


def test_daily_series_follows_requested_timezone(session: Session) -> None:
    """UTC 10-01 17:00 在 UTC+8 已经是 10-02 —— 分桶必须按请求方时区。"""
    _record(
        session,
        "span_late",
        session_id="session_a",
        total_tokens=42,
        finished_at="2026-10-01T17:00:00+00:00",
    )
    client = _client(session, "user_admin")

    east = client.get(
        "/api/enterprise/usage/stats",
        params={**_range(), "tz_offset_minutes": 480},
    ).json()
    utc = client.get(
        "/api/enterprise/usage/stats",
        params={**_range(), "tz_offset_minutes": 0},
    ).json()

    def non_zero_days(body: dict) -> list[str]:
        return [point["date"] for point in body["daily"] if point["calls"]]

    assert non_zero_days(east) == ["2026-10-02"]
    assert non_zero_days(utc) == ["2026-10-01"]
    # 区间内每一天都要在，前端不用自己补空档
    assert len(east["daily"]) == 32


def test_range_length_is_capped(session: Session) -> None:
    body = _client(session, "user_admin").get(
        "/api/enterprise/usage/stats",
        params={"tenant_id": TENANT_ID, "start": "2000-01-01", "end": "2026-10-02"},
    ).json()
    assert body["range"]["days"] == 366


def test_totals_exclude_usage_outside_the_range(session: Session) -> None:
    _record(session, "span_in", session_id="session_a", total_tokens=10)
    _record(
        session,
        "span_out",
        session_id="session_a",
        total_tokens=999,
        finished_at="2026-08-01T04:00:00+00:00",
    )

    body = _client(session, "user_admin").get(
        "/api/enterprise/usage/stats", params=_range()
    ).json()
    assert body["totals"]["total_tokens"] == 10


def test_usage_operations_lists_scenarios_only_from_visible_rows(session: Session) -> None:
    _record(session, "span_a", session_id="session_a", operation="session.title")
    _record(session, "span_b", session_id="session_b", operation="harness.task_action")

    admin_ops = _client(session, "user_admin").get(
        "/api/enterprise/usage/operations", params={"tenant_id": TENANT_ID}
    ).json()
    member_ops = _client(session, "user_member").get(
        "/api/enterprise/usage/operations", params={"tenant_id": TENANT_ID}
    ).json()

    assert sorted(admin_ops) == ["harness.task_action", "session.title"]
    assert member_ops == ["harness.task_action"]


def _range() -> dict:
    return {"tenant_id": TENANT_ID, "start": "2026-10-01", "end": "2026-11-01"}


def _record(
    session: Session,
    span_id: str,
    *,
    session_id: str,
    total_tokens: int = 120,
    operation: str = "harness.task_action",
    finished_at: str = "2026-10-01T04:00:00+00:00",
) -> None:
    record_llm_usage(
        session,
        tenant_id=TENANT_ID,
        session_id=session_id,
        event_type="llm_call_finished",
        payload=_usage_payload(
            span_id=span_id,
            operation=operation,
            total_tokens=total_tokens,
            input_tokens=total_tokens - 20,
            finished_at=finished_at,
        ),
    )
    session.commit()
