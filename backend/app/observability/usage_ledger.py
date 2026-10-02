"""大模型用量账本。

每次模型调用结束时，``app/llm/client.py`` 都会通过 span 机制抛出一条
``llm_call_finished`` 事件（带 ``input_tokens`` / ``output_tokens`` /
``total_tokens`` / ``cached_input_tokens``）。这里把它落成 ``llm_usage_records``
的一行，供「用量统计」页面按天 / 按数字员工 / 按用户聚合。

两条写入路径：

* :func:`record_llm_usage` —— 实时路径，挂在各入口的 span sink 上（chat、
  后台 Memory 任务、渠道入站）。只 ``add`` 不 ``commit``，由调用方原有的
  commit 一起提交，不额外引入事务。
* :func:`migrate_backfill_llm_usage` —— 升级时的一次性回填，用 SQLite 的
  JSON1 从既有 ``agent_events`` 补齐历史用量（``app_data_migrations`` 守卫）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from app.db.models import ChatSession, LlmUsageRecord

logger = logging.getLogger(__name__)

# 与 app/llm/client.py 中 start_llm_call 的事件前缀保持一致
USAGE_EVENT_TYPE = "llm_call_finished"

# 一次性回填的迁移 id
BACKFILL_MIGRATION_ID = "llm_usage_records_backfill_v1"

_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens")


def record_llm_usage(
    db: Session,
    *,
    tenant_id: str,
    session_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> LlmUsageRecord | None:
    """把一次模型调用追加进账本；不是模型结束事件时返回 ``None``。

    只 ``add`` 到会话，不 ``commit`` —— 调用方在写完自己的事件后提交，两者同批落库。
    """
    if event_type != USAGE_EVENT_TYPE:
        return None
    span_id = str(payload.get("span_id") or "").strip()
    if not span_id:
        return None
    try:
        if _already_recorded(db, span_id):
            return None
        user_id, agent_id = _session_attribution(db, tenant_id, session_id)
        row = LlmUsageRecord(
            tenant_id=tenant_id,
            session_id=session_id or None,
            user_id=user_id,
            agent_id=agent_id,
            span_id=span_id,
            operation=str(payload.get("operation") or ""),
            model_name=str(payload.get("model_name") or payload.get("model") or ""),
            provider_model=str(payload.get("model") or ""),
            status=str(payload.get("status") or "success"),
            duration_ms=_float_value(payload.get("duration_ms")),
            source="live",
            **{key: _int_value(payload.get(key)) for key in _TOKEN_KEYS},
        )
        finished_at = _usage_finished_at(payload)
        if finished_at is not None:
            row.created_at = finished_at
        db.add(row)
    except Exception:  # 用量统计失败不能拖垮业务请求
        logger.warning("记录大模型用量失败 span_id=%s", span_id, exc_info=True)
        return None
    return row


def _already_recorded(db: Session, span_id: str) -> bool:
    return (
        db.exec(select(LlmUsageRecord.id).where(LlmUsageRecord.span_id == span_id)).first()
        is not None
    )


def _session_attribution(
    db: Session,
    tenant_id: str,
    session_id: str | None,
) -> tuple[str | None, str | None]:
    """会话 -> (用户, 数字员工)。渠道入站会话同样落在这里，取不到则为空。"""
    if not session_id:
        return None, None
    row = db.get(ChatSession, session_id)
    if row is None or row.tenant_id != tenant_id:
        return None, None
    return row.user_id, row.agent_id


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _usage_finished_at(payload: dict[str, Any]) -> datetime | None:
    """账本时间取 span 的完成时刻，统一转成「UTC naive」，与 utc_now() 口径一致。"""
    raw = str(payload.get("finished_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.replace(tzinfo=None)


def migrate_backfill_llm_usage(conn, tables: set[str]) -> None:
    """从既有 ``agent_events`` 回填历史用量，只跑一次（SQLite）。"""
    if "llm_usage_records" not in tables or "agent_events" not in tables:
        return

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS app_data_migrations (
                id VARCHAR PRIMARY KEY,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    applied = conn.execute(
        text("SELECT id FROM app_data_migrations WHERE id = :id"),
        {"id": BACKFILL_MIGRATION_ID},
    ).first()
    if applied:
        return

    # JSON1 提取 + INSERT OR IGNORE：span_id 唯一，重复执行幂等。
    result = conn.execute(
        text(
            """
            INSERT OR IGNORE INTO llm_usage_records (
                id, tenant_id, session_id, user_id, agent_id, span_id, operation,
                model_name, provider_model, status, input_tokens, output_tokens,
                total_tokens, cached_input_tokens, duration_ms, source, created_at
            )
            SELECT
                'llmuse_bf' || substr(hex(randomblob(8)), 1, 16),
                e.tenant_id,
                e.session_id,
                s.user_id,
                s.agent_id,
                coalesce(json_extract(e.payload_json, '$.span_id'), e.id),
                coalesce(json_extract(e.payload_json, '$.operation'), ''),
                coalesce(
                    json_extract(e.payload_json, '$.model_name'),
                    json_extract(e.payload_json, '$.model'),
                    ''
                ),
                coalesce(json_extract(e.payload_json, '$.model'), ''),
                coalesce(json_extract(e.payload_json, '$.status'), 'success'),
                CAST(coalesce(json_extract(e.payload_json, '$.input_tokens'), 0) AS INTEGER),
                CAST(coalesce(json_extract(e.payload_json, '$.output_tokens'), 0) AS INTEGER),
                CAST(coalesce(json_extract(e.payload_json, '$.total_tokens'), 0) AS INTEGER),
                CAST(coalesce(json_extract(e.payload_json, '$.cached_input_tokens'), 0) AS INTEGER),
                CAST(coalesce(json_extract(e.payload_json, '$.duration_ms'), 0) AS REAL),
                'backfill',
                e.created_at
            FROM agent_events e
            LEFT JOIN sessions s ON s.id = e.session_id
            WHERE e.event_type = :event_type
            """
        ),
        {"event_type": USAGE_EVENT_TYPE},
    )
    conn.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": BACKFILL_MIGRATION_ID},
    )
    logger.info("大模型用量账本回填完成，写入 %s 行", result.rowcount)
