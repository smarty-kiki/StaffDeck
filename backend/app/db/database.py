import hashlib
import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import get_settings
from app.db.database_path import normalize_database_url
from app.db.models import AGENT_SCOPE, GALLERY_SCOPE, new_id, utc_now


def _normalize_database_url(url: str) -> str:
    """Backward-compatible import seam for callers and migration tests."""
    return normalize_database_url(url)


settings = get_settings()

database_url = _normalize_database_url(settings.database_url)
connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
engine: Engine = create_engine(database_url, echo=False, connect_args=connect_args)

_DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID = "20260712_default_model_output_tokens_8192"
_LEGACY_DEFAULT_MODEL_OUTPUT_TOKENS = 2048
_MODEL_API_PROTOCOLS_MIGRATION_ID = "20260722_model_api_protocols_v1"
_DEFAULT_MODEL_OUTPUT_TOKENS = 8192
_MODEL_API_PROTOCOL_COLUMNS = {
    "extra_body_json",
    "api_protocol",
    "protocol_options_json",
    "legacy_unmapped_options_json",
    "trust_status",
    "verified_at",
    "verified_fingerprint",
    "verification_attempt_id",
    "verification_started_at",
    "verification_attempt_status",
    "verification_attempt_error_code",
    "config_revision",
    "security_revision",
    "key_revision",
}

_CHANNEL_BINDING_AGENTS_BACKFILL_MIGRATION_ID = "20260718_channel_binding_agents_backfill"
_USER_SOURCE_BACKFILL_MIGRATION_ID = "20260718_user_source_wechat_backfill"
_CHANNEL_SCOPE_REBUILD_MIGRATION_ID = "20260719_channel_scope_rebuild"
_CHANNEL_BINDINGS_MULTI_MIGRATION_ID = "20260721_channel_bindings_multi"
_CHANNEL_ACCOUNT_KEY_MIGRATION_ID = "20260723_channel_account_key_v1"
_FEISHU_CHANNEL_SCHEMA_MIGRATION_ID = "20260724_feishu_channel_schema_v1"
_RESOURCE_OWNERSHIP_MIGRATION_ID = "20260924_resource_ownership_v1"
_DROP_OVERALL_AGENT_MIGRATION_ID = "20260924_drop_overall_agent_v1"
# 迁移是**历史快照**：这里写死的 DDL 是「改造完成时」的库结构，
# 不跟随 models.py 继续漂移；索引集则从模型元数据派生（索引是约束的机械派生物）。
_RESOURCE_OWNERSHIP_SCOPED_TABLES: tuple[tuple[str, str, str, str | None], ...] = (
    # (resource_type, table, 业务键列, metadata 列) —— skills/tools 没有 metadata 列，
    # 它们的归属只能从分支表与绑定表推断。
    ("skill", "skills", "skill_id", None),
    ("general_skill", "general_skills", "slug", "metadata_json"),
    ("knowledge_base", "knowledge_bases", "name", "metadata_json"),
    ("tool", "tools", "name", None),
)
_RESOURCE_OWNERSHIP_LEGACY_BRANCH_TABLES = (
    "agent_skill_branches",
    "agent_skill_branch_versions",
    "agent_knowledge_branches",
)
# 改造前归属藏在 metadata_json 里（branching.py 的 open_gallery_metadata /
# _agent_private_metadata_for 写入）。迁移后这些键失效，必须清掉，否则两处真相。
_RESOURCE_OWNERSHIP_STATE_METADATA_KEYS = (
    "scope",
    "visibility",
    "owner_agent_id",
)
_RESOURCE_OWNERSHIP_AGENT_STATE_METADATA_KEYS = ("published_to_gallery",)
_CAPABILITY_SCOPE_TABLES = (
    "general_skills",
    "tools",
    "mcp_servers",
    "knowledge_bases",
    "knowledge_base_versions",
)


def init_db() -> None:
    import app.db.models  # noqa: F401

    _configure_sqlite_runtime()
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite_skill_schema()
    _purge_orphaned_chat_sessions()


def _purge_orphaned_chat_sessions() -> None:
    """清理孤儿会话:团队/员工已被删除但会话残留(级联清理上线前的历史数据)。"""
    from app.db.models import AgentProfile, ChatSession, Team
    from app.session.cleanup import (
        purge_chat_session_records,
        remove_chat_session_workspace,
    )

    with Session(engine) as db:
        referenced = db.exec(
            select(ChatSession).where(
                ChatSession.team_id.is_not(None) | ChatSession.agent_id.is_not(None)
            )
        ).all()
        if not referenced:
            return
        team_ids = {team_id for team_id in db.exec(select(Team.id)).all()}
        agent_ids = {agent_id for agent_id in db.exec(select(AgentProfile.id)).all()}
        orphaned = [
            session
            for session in referenced
            if (session.team_id and session.team_id not in team_ids)
            or (session.agent_id and session.agent_id not in agent_ids)
        ]
        if not orphaned:
            return
        workspace_keys = [(session.tenant_id, session.id) for session in orphaned]
        for session in orphaned:
            purge_chat_session_records(db, session)
        db.commit()
        for tenant_id, session_id in workspace_keys:
            remove_chat_session_workspace(tenant_id=tenant_id, session_id=session_id, db=db)


def _configure_sqlite_runtime() -> None:
    if not database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=30000"))


def _migrate_sqlite_skill_schema() -> None:
    if not database_url.startswith("sqlite"):
        return

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    legacy_key = "so" + "p"
    legacy_active_column = f"active_{legacy_key}_id"
    legacy_stack_column = f"{legacy_key}_stack_json"
    legacy_allowed_column = f"allowed_{legacy_key}s_json"
    legacy_table = f"{legacy_key}_skills"
    legacy_id_column = f"{legacy_key}_id"
    legacy_id_prefix = f"{legacy_key}_"
    with _sqlite_immediate_connection() as conn:
        _migrate_model_api_protocols(conn, tables)
        _migrate_default_model_output_limit(conn, tables)
        _migrate_channel_binding_agents_backfill(conn, tables)
        _migrate_channel_scope_rebuild(conn, inspector, tables)
        _migrate_channel_bindings_multi(conn, inspector, tables)
        _migrate_channel_account_key_schema(conn, tables)
        _migrate_feishu_channel_schema(conn, tables)
        _migrate_channel_inbound_run_schema(conn, tables)
        _migrate_channel_bind_code_constraints(conn, tables)
        _migrate_wechat_kf_accounts(conn, tables)
        _migrate_capability_scope_schema(conn, inspector, tables)
        _migrate_harness_v2_schema(conn, inspector, tables)
        # 大模型用量账本：从既有 agent_events 补齐历史用量（一次性）
        from app.observability.usage_ledger import migrate_backfill_llm_usage

        migrate_backfill_llm_usage(conn, tables)

        if "api_jobs" in tables:
            job_columns = {column["name"] for column in inspector.get_columns("api_jobs")}
            api_job_columns = {
                "execution_owner": "ALTER TABLE api_jobs ADD COLUMN execution_owner VARCHAR",
                "execution_generation": (
                    "ALTER TABLE api_jobs ADD COLUMN execution_generation INTEGER NOT NULL DEFAULT 0"
                ),
                "lease_expires_at": "ALTER TABLE api_jobs ADD COLUMN lease_expires_at DATETIME",
            }
            for column_name, ddl in api_job_columns.items():
                if column_name not in job_columns:
                    conn.execute(text(ddl))

        if "webhook_deliveries" in tables:
            webhook_columns = {
                column["name"] for column in inspector.get_columns("webhook_deliveries")
            }
            webhook_delivery_columns = {
                "delivery_owner": (
                    "ALTER TABLE webhook_deliveries ADD COLUMN delivery_owner VARCHAR"
                ),
                "lease_expires_at": (
                    "ALTER TABLE webhook_deliveries ADD COLUMN lease_expires_at DATETIME"
                ),
            }
            for column_name, ddl in webhook_delivery_columns.items():
                if column_name not in webhook_columns:
                    conn.execute(text(ddl))

        if "users" in tables:
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            if "role" not in user_columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR NOT NULL DEFAULT 'member'"))
            if "source" not in user_columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN source VARCHAR NOT NULL DEFAULT 'web'"))
            if "display_name" in user_columns:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_users_tenant_id_display_name "
                        "ON users(tenant_id, display_name)"
                    )
                )
            _migrate_user_source_backfill(conn)

        if "sessions" in tables:
            session_columns = {column["name"] for column in inspector.get_columns("sessions")}
            if "agent_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN agent_id VARCHAR"))
            if "title" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN title VARCHAR"))
            if "active_skill_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN active_skill_id VARCHAR"))
                if legacy_active_column in session_columns:
                    conn.execute(text(f"UPDATE sessions SET active_skill_id = {legacy_active_column}"))
            if "skill_stack_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN skill_stack_json JSON"))
                if legacy_stack_column in session_columns:
                    conn.execute(text(f"UPDATE sessions SET skill_stack_json = {legacy_stack_column}"))
                else:
                    conn.execute(text("UPDATE sessions SET skill_stack_json = '[]'"))
            if "pending_tasks_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN pending_tasks_json JSON"))
                conn.execute(text("UPDATE sessions SET pending_tasks_json = '[]'"))
            if "awaiting_input_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN awaiting_input_json JSON"))
            if "knowledge_context_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN knowledge_context_json JSON"))
                conn.execute(text("UPDATE sessions SET knowledge_context_json = '[]'"))
            if "context_state_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN context_state_json JSON"))
                conn.execute(text("UPDATE sessions SET context_state_json = '{}'"))
            if "channel" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN channel VARCHAR"))
            if "external_conv_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN external_conv_id VARCHAR"))
            if "channel_target_json" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN channel_target_json JSON"))
            if "channel_binding_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN channel_binding_id VARCHAR"))
            if "channel_account_key" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN channel_account_key VARCHAR"))
            if "team_id" not in session_columns:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN team_id VARCHAR"))
            # SQLite 唯一索引中 NULL 互不相等，web 会话（channel 为空）不受约束；
            # 含 channel_binding_id 以隔离同企业多 Bot(老三列索引先 DROP 再按新四列重建)
            session_index_columns = {
                tuple(index["column_names"])
                for index in inspector.get_indexes("sessions")
                if index["name"] == "uq_sessions_agent_channel_extconv"
            }
            if ("agent_id", "channel", "external_conv_id") in session_index_columns:
                conn.execute(text("DROP INDEX IF EXISTS uq_sessions_agent_channel_extconv"))
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_agent_channel_extconv "
                    "ON sessions(agent_id, channel, channel_binding_id, external_conv_id)"
                )
            )
            if "channel_bindings" in tables:
                conn.execute(
                    text(
                        "UPDATE sessions SET channel_account_key = ("
                        "SELECT external_account_key FROM channel_bindings "
                        "WHERE channel_bindings.id = sessions.channel_binding_id) "
                        "WHERE channel_binding_id IS NOT NULL AND EXISTS ("
                        "SELECT 1 FROM channel_bindings "
                        "WHERE channel_bindings.id = sessions.channel_binding_id)"
                    )
                )

        if "channel_conv_states" in tables:
            conv_columns = {column["name"] for column in inspector.get_columns("channel_conv_states")}
            if "manual_pin_until" not in conv_columns:
                conn.execute(text("ALTER TABLE channel_conv_states ADD COLUMN manual_pin_until DATETIME"))
            if "routing_revision" not in conv_columns:
                conn.execute(
                    text(
                        "ALTER TABLE channel_conv_states ADD COLUMN routing_revision "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                )

        if "channel_bindings" in tables:
            binding_columns = {column["name"] for column in inspector.get_columns("channel_bindings")}
            if "last_connected_at" not in binding_columns:
                conn.execute(text("ALTER TABLE channel_bindings ADD COLUMN last_connected_at DATETIME"))
            if "team_id" not in binding_columns:
                conn.execute(text("ALTER TABLE channel_bindings ADD COLUMN team_id VARCHAR"))
            if "name" not in binding_columns:
                conn.execute(text("ALTER TABLE channel_bindings ADD COLUMN name VARCHAR"))

        if "channel_deliveries" in tables:
            delivery_columns = {column["name"] for column in inspector.get_columns("channel_deliveries")}
            if "sending_since" not in delivery_columns:
                conn.execute(text("ALTER TABLE channel_deliveries ADD COLUMN sending_since DATETIME"))
            if "delivery_owner" not in delivery_columns:
                conn.execute(text("ALTER TABLE channel_deliveries ADD COLUMN delivery_owner VARCHAR"))
            if "delivery_generation" not in delivery_columns:
                conn.execute(
                    text(
                        "ALTER TABLE channel_deliveries ADD COLUMN delivery_generation "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                )

        if "channel_inbound_events" in tables:
            inbound_columns = {
                column["name"] for column in inspector.get_columns("channel_inbound_events")
            }
            if "processor_lease_expires_at" not in inbound_columns:
                conn.execute(
                    text(
                        "ALTER TABLE channel_inbound_events ADD COLUMN "
                        "processor_lease_expires_at DATETIME"
                    )
                )

        if "human_handoff_requests" in tables:
            handoff_columns = {
                column["name"] for column in inspector.get_columns("human_handoff_requests")
            }
            if "notify_message_id" not in handoff_columns:
                conn.execute(
                    text("ALTER TABLE human_handoff_requests ADD COLUMN notify_message_id VARCHAR")
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_human_handoff_requests_notify_message_id "
                        "ON human_handoff_requests(notify_message_id)"
                    )
                )

        if "messages" in tables:
            message_columns = {column["name"] for column in inspector.get_columns("messages")}
            if "metadata_json" not in message_columns:
                conn.execute(text("ALTER TABLE messages ADD COLUMN metadata_json JSON"))
                conn.execute(text("UPDATE messages SET metadata_json = '{}' WHERE metadata_json IS NULL"))

        if "tools" in tables:
            tool_columns = {column["name"] for column in inspector.get_columns("tools")}
            if "bucket" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN bucket VARCHAR NOT NULL DEFAULT '未分桶'"))
            if "tool_type" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN tool_type VARCHAR NOT NULL DEFAULT 'http'"))
            if "config_json" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN config_json JSON"))
                conn.execute(text("UPDATE tools SET config_json = '{}' WHERE config_json IS NULL"))
            if "allowed_skills_json" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN allowed_skills_json JSON"))
                if legacy_allowed_column in tool_columns:
                    conn.execute(text(f"UPDATE tools SET allowed_skills_json = {legacy_allowed_column}"))
                else:
                    conn.execute(text("UPDATE tools SET allowed_skills_json = '[]'"))
            if "mcp_server_id" not in tool_columns:
                conn.execute(text("ALTER TABLE tools ADD COLUMN mcp_server_id VARCHAR"))
            if "capability_scope_inherited" not in tool_columns:
                conn.execute(
                    text(
                        "ALTER TABLE tools ADD COLUMN capability_scope_inherited "
                        "BOOLEAN NOT NULL DEFAULT 1"
                    )
                )
            conn.execute(
                text(
                    "UPDATE tools SET capability_scope_inherited = 1 "
                    "WHERE capability_scope_inherited IS NULL"
                )
            )

        if "external_business_tasks" in tables:
            task_columns = {
                column["name"]
                for column in inspector.get_columns("external_business_tasks")
            }
            additions = {
                "status_config_json": (
                    "ALTER TABLE external_business_tasks ADD COLUMN status_config_json JSON"
                ),
                "idempotency_key": (
                    "ALTER TABLE external_business_tasks ADD COLUMN idempotency_key VARCHAR"
                ),
                "lease_owner": (
                    "ALTER TABLE external_business_tasks ADD COLUMN lease_owner VARCHAR"
                ),
                "lease_expires_at": (
                    "ALTER TABLE external_business_tasks ADD COLUMN lease_expires_at DATETIME"
                ),
                "expires_at": (
                    "ALTER TABLE external_business_tasks ADD COLUMN expires_at DATETIME"
                ),
                "task_frame_id": (
                    "ALTER TABLE external_business_tasks ADD COLUMN task_frame_id VARCHAR"
                ),
                "resume_step_id": (
                    "ALTER TABLE external_business_tasks ADD COLUMN resume_step_id VARCHAR"
                ),
            }
            for column_name, ddl in additions.items():
                if column_name not in task_columns:
                    conn.execute(text(ddl))
            conn.execute(
                text(
                    "UPDATE external_business_tasks SET status_config_json = '{}' "
                    "WHERE status_config_json IS NULL"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_external_business_tasks_idempotency_key "
                    "ON external_business_tasks(idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL"
                )
            )

        if "mcp_servers" in tables:
            mcp_server_columns = {
                column["name"] for column in inspector.get_columns("mcp_servers")
            }
            if "apps_mode" not in mcp_server_columns:
                conn.execute(
                    text(
                        "ALTER TABLE mcp_servers ADD COLUMN apps_mode "
                        "VARCHAR NOT NULL DEFAULT 'disabled'"
                    )
                )
            if "negotiated_capabilities_json" not in mcp_server_columns:
                conn.execute(
                    text(
                        "ALTER TABLE mcp_servers ADD COLUMN negotiated_capabilities_json JSON"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE mcp_servers SET negotiated_capabilities_json = '{}' "
                        "WHERE negotiated_capabilities_json IS NULL"
                    )
                )

        if "agent_profiles" in tables:
            agent_columns = {
                column["name"] for column in inspector.get_columns("agent_profiles")
            }
            if "harness_max_actions" not in agent_columns:
                conn.execute(
                    text(
                        "ALTER TABLE agent_profiles ADD COLUMN harness_max_actions "
                        "INTEGER NOT NULL DEFAULT 32"
                    )
                )

        if "ui_configs" in tables:
            ui_columns = {column["name"] for column in inspector.get_columns("ui_configs")}
            if "reflection_max_rounds" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN reflection_max_rounds INTEGER NOT NULL DEFAULT 1")
                )
            if "agent_loop_max_actions" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN agent_loop_max_actions "
                        "INTEGER NOT NULL DEFAULT 32"
                    )
                )
            if "context_token_budget" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN context_token_budget "
                        "INTEGER NOT NULL DEFAULT 32000"
                    )
                )
            if "context_compaction_trigger_ratio" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN context_compaction_trigger_ratio "
                        "FLOAT NOT NULL DEFAULT 0.70"
                    )
                )
            if "context_recent_round_limit" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN context_recent_round_limit "
                        "INTEGER NOT NULL DEFAULT 6"
                    )
                )
            if "context_long_summary_token_budget" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN context_long_summary_token_budget "
                        "INTEGER NOT NULL DEFAULT 4000"
                    )
                )
            if "context_medium_summary_token_budget" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN context_medium_summary_token_budget "
                        "INTEGER NOT NULL DEFAULT 4000"
                    )
                )
            if "context_allowed_roles" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN context_allowed_roles "
                        "JSON NOT NULL DEFAULT '[\"user\", \"assistant\"]'"
                    )
                )
            if "context_long_summary_prefix" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN context_long_summary_prefix "
                        "VARCHAR NOT NULL DEFAULT '历史的信息可以被总结为：'"
                    )
                )
            if "context_medium_summary_prefix" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN context_medium_summary_prefix "
                        "VARCHAR NOT NULL DEFAULT '近期的历史信息总结为：'"
                    )
                )
            if "sandbox_enabled" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN sandbox_enabled "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
            if "sandbox_network_mode" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN sandbox_network_mode "
                        "VARCHAR(32) NOT NULL DEFAULT 'all'"
                    )
                )
            if "sandbox_allowed_domains" not in ui_columns:
                conn.execute(
                    text(
                        "ALTER TABLE ui_configs ADD COLUMN sandbox_allowed_domains "
                        "JSON NOT NULL DEFAULT '[]'"
                    )
                )
            if "harness_storage_path" not in ui_columns:
                conn.execute(
                    text("ALTER TABLE ui_configs ADD COLUMN harness_storage_path VARCHAR")
                )

        if "team_tasks" in tables:
            team_task_columns = {
                column["name"] for column in inspector.get_columns("team_tasks")
            }
            if "team_run_id" not in team_task_columns:
                conn.execute(text("ALTER TABLE team_tasks ADD COLUMN team_run_id VARCHAR"))
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_team_tasks_team_run_id ON team_tasks (team_run_id)")
                )
            if "source_turn_id" not in team_task_columns:
                conn.execute(text("ALTER TABLE team_tasks ADD COLUMN source_turn_id VARCHAR"))
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_team_tasks_source_turn_id "
                        "ON team_tasks (source_turn_id)"
                    )
                )
            if "depends_on_task_ids_json" not in team_task_columns:
                conn.execute(text("ALTER TABLE team_tasks ADD COLUMN depends_on_task_ids_json JSON"))
                conn.execute(
                    text(
                        "UPDATE team_tasks SET depends_on_task_ids_json = '[]' "
                        "WHERE depends_on_task_ids_json IS NULL"
                    )
                )
            if "activation_condition_json" not in team_task_columns:
                conn.execute(text("ALTER TABLE team_tasks ADD COLUMN activation_condition_json JSON"))
                conn.execute(
                    text(
                        "UPDATE team_tasks SET activation_condition_json = '{}' "
                        "WHERE activation_condition_json IS NULL"
                    )
                )

        if "skill_feedback" in tables:
            feedback_columns = {column["name"] for column in inspector.get_columns("skill_feedback")}
            if "skill_version" not in feedback_columns:
                conn.execute(text("ALTER TABLE skill_feedback ADD COLUMN skill_version VARCHAR"))
            if "step_id" not in feedback_columns:
                conn.execute(text("ALTER TABLE skill_feedback ADD COLUMN step_id VARCHAR"))

        if "message_feedback" in tables:
            message_feedback_columns = {column["name"] for column in inspector.get_columns("message_feedback")}
            feedback_column_sql = {
                "analysis_status": "ALTER TABLE message_feedback ADD COLUMN analysis_status VARCHAR NOT NULL DEFAULT 'pending'",
                "analysis_bucket": "ALTER TABLE message_feedback ADD COLUMN analysis_bucket VARCHAR",
                "analysis_reason": "ALTER TABLE message_feedback ADD COLUMN analysis_reason VARCHAR",
                "analysis_summary": "ALTER TABLE message_feedback ADD COLUMN analysis_summary VARCHAR",
                "analysis_confidence": "ALTER TABLE message_feedback ADD COLUMN analysis_confidence FLOAT",
                "analysis_json": "ALTER TABLE message_feedback ADD COLUMN analysis_json JSON",
                "analyzed_at": "ALTER TABLE message_feedback ADD COLUMN analyzed_at DATETIME",
            }
            for column_name, ddl in feedback_column_sql.items():
                if column_name not in message_feedback_columns:
                    conn.execute(text(ddl))
            if "analysis_json" not in message_feedback_columns:
                conn.execute(text("UPDATE message_feedback SET analysis_json = '{}' WHERE analysis_json IS NULL"))

        if "general_skills" in tables:
            general_skill_columns = {column["name"] for column in inspector.get_columns("general_skills")}
            if "skill_files_json" not in general_skill_columns:
                conn.execute(text("ALTER TABLE general_skills ADD COLUMN skill_files_json JSON"))
                conn.execute(text("UPDATE general_skills SET skill_files_json = '[]' WHERE skill_files_json IS NULL"))
            if "metadata_json" not in general_skill_columns:
                conn.execute(text("ALTER TABLE general_skills ADD COLUMN metadata_json JSON"))
                conn.execute(text("UPDATE general_skills SET metadata_json = '{}' WHERE metadata_json IS NULL"))

        _migrate_knowledge_base_schema(conn, inspector, tables)
        # 归属化迁移必须排在所有「加列型」迁移之后：表重建要把最终列全集一次性搬过去。
        _migrate_resource_ownership(conn, inspector, tables)
        # 紧跟在归属化之后：它要读归属化刚用过的 `is_overall` 列，读完就把列删掉。
        _migrate_drop_overall_agent(conn, tables)
        _purge_legacy_agent_model_bindings(conn, tables)
        _seed_default_agents(conn, tables)

        if legacy_table in tables and "skills" in tables:
            rows = conn.execute(text(f"SELECT * FROM {legacy_table}")).mappings().all()
            for row in rows:
                skill_id = _normalize_skill_identifier(
                    row.get("skill_id") or row.get(legacy_id_column),
                    legacy_id_prefix,
                )
                if not skill_id:
                    continue
                target_id = str(row["id"]).replace(legacy_id_prefix, "skill_", 1)
                existing = conn.execute(
                    text("SELECT id FROM skills WHERE tenant_id = :tenant_id AND skill_id = :skill_id"),
                    {"tenant_id": row["tenant_id"], "skill_id": skill_id},
                ).first()
                if existing:
                    continue
                content = _migrate_skill_content(row.get("content_json"), skill_id)
                existing_id = conn.execute(
                    text("SELECT id FROM skills WHERE id = :id"),
                    {"id": target_id},
                ).first()
                if existing_id:
                    conn.execute(
                        text(
                            """
                            UPDATE skills
                            SET skill_id = :skill_id, content_json = :content_json, updated_at = :updated_at
                            WHERE id = :id
                            """
                        ),
                        {
                            "id": target_id,
                            "skill_id": skill_id,
                            "content_json": json.dumps(content, ensure_ascii=False),
                            "updated_at": row.get("updated_at"),
                        },
                    )
                    continue
                extra_columns, extra_values = _gallery_scope_insert_columns(conn, "skills")
                conn.execute(
                    text(
                        """
                        INSERT INTO skills (
                            id, tenant_id, skill_id, version, name, business_domain,
                            description, content_json, status, created_at, updated_at"""
                        + extra_columns
                        + """
                        )
                        VALUES (
                            :id, :tenant_id, :skill_id, :version, :name, :business_domain,
                            :description, :content_json, :status, :created_at, :updated_at"""
                        + extra_values
                        + """
                        )
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant_id": row["tenant_id"],
                        "skill_id": skill_id,
                        "version": row.get("version") or "1.0.0",
                        "name": row["name"],
                        "business_domain": row.get("business_domain"),
                        "description": row.get("description"),
                        "content_json": json.dumps(content, ensure_ascii=False),
                        "status": row.get("status") or "draft",
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                    },
                )
        if "skills" in tables:
            _normalize_existing_skill_rows(conn, legacy_id_prefix)
            if "skill_versions" in tables:
                _normalize_existing_skill_version_rows(conn, legacy_id_prefix)
                _seed_skill_versions(conn)
            _sync_explicit_skill_tool_bindings(conn, tables)


@contextmanager
def _sqlite_immediate_connection():
    conn = engine.connect()
    try:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_default_model_output_limit(conn, tables: set[str]) -> None:
    if "model_configs" not in tables:
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
        {"id": _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID},
    ).first()
    if applied:
        return

    conn.execute(
        text(
            """
            UPDATE model_configs
            SET max_output_tokens = :new_limit,
                updated_at = CURRENT_TIMESTAMP
            WHERE is_default = 1
              AND max_output_tokens = :legacy_limit
            """
        ),
        {
            "new_limit": _DEFAULT_MODEL_OUTPUT_TOKENS,
            "legacy_limit": _LEGACY_DEFAULT_MODEL_OUTPUT_TOKENS,
        },
    )
    conn.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": _DEFAULT_MODEL_OUTPUT_LIMIT_MIGRATION_ID},
    )


def _migrate_channel_binding_agents_backfill(conn, tables: set[str]) -> None:
    """为存量渠道绑定补挂载行(binding_id, agent_id, is_default=1),只跑一次。"""
    if "channel_bindings" not in tables or "channel_binding_agents" not in tables:
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
        {"id": _CHANNEL_BINDING_AGENTS_BACKFILL_MIGRATION_ID},
    ).first()
    if applied:
        return

    rows = conn.execute(
        text("SELECT id, tenant_id, agent_id FROM channel_bindings")
    ).mappings().all()
    for row in rows:
        existing = conn.execute(
            text(
                "SELECT id FROM channel_binding_agents "
                "WHERE binding_id = :binding_id AND agent_id = :agent_id"
            ),
            {"binding_id": row["id"], "agent_id": row["agent_id"]},
        ).first()
        if existing:
            continue
        conn.execute(
            text(
                """
                INSERT INTO channel_binding_agents (
                    id, tenant_id, binding_id, agent_id, is_default, sort_order, created_at
                )
                VALUES (:id, :tenant_id, :binding_id, :agent_id, 1, 0, CURRENT_TIMESTAMP)
                """
            ),
            {
                "id": f"chba_{row['id']}",
                "tenant_id": row["tenant_id"],
                "binding_id": row["id"],
                "agent_id": row["agent_id"],
            },
        )
    conn.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": _CHANNEL_BINDING_AGENTS_BACKFILL_MIGRATION_ID},
    )


def _migrate_user_source_backfill(conn) -> None:
    """存量渠道懒建账号(username 以 wechat_ 开头,含群账号)source 置 'wechat',只跑一次。"""
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
        {"id": _USER_SOURCE_BACKFILL_MIGRATION_ID},
    ).first()
    if applied:
        return
    conn.execute(
        text("UPDATE users SET source = 'wechat' WHERE substr(username, 1, 7) = 'wechat_' AND source = 'web'")
    )
    conn.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": _USER_SOURCE_BACKFILL_MIGRATION_ID},
    )


def _wecom_scope_from_config(config: object, binding_id: str) -> str:
    if isinstance(config, dict):
        return str(config.get("corp_id") or config.get("bot_id") or "").strip() or binding_id
    return binding_id


def _migrate_channel_scope_rebuild(conn, inspector, tables: set[str]) -> None:
    """身份作用域重构(一次性,app_data_migrations 守卫):

    1) channel_identities 重建:加 external_account_scope 列,唯一约束改
       (tenant_id, channel, external_account_scope, external_user_id)。存量行 scope
       回填优先级:①被会话引用的 identity 按其会话所在 binding 的 scope(多 scope
       歧义则留 legacy 并隔离相关会话);②无会话引用且该 tenant+channel 现存 scope
       唯一时用之(多 scope 不猜);③取不到归 'legacy'。
    2) channel_inbound_events 重建:唯一约束 (channel, event_id) 改 (binding_id, event_id)。
    3) sessions 的 wecom external_conv_id 改写为 wecom_{scope}_p2p_/group_ 格式(孤儿归 legacy)。
    """
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
        {"id": _CHANNEL_SCOPE_REBUILD_MIGRATION_ID},
    ).first()
    if applied:
        return

    scope_by_binding_id: dict[str, str] = {}
    scopes_by_tenant_channel: dict[tuple[str, str], set[str]] = {}
    if "channel_bindings" in tables:
        for row in conn.execute(
            text(
                "SELECT id, tenant_id, channel, status, config_json "
                "FROM channel_bindings ORDER BY created_at, id"
            )
        ).mappings().all():
            if row["channel"] != "wecom":
                scope = ""
            else:
                scope = _wecom_scope_from_config(
                    _json_object(row.get("config_json")), str(row["id"])
                )
            scope_by_binding_id[str(row["id"])] = scope
            scopes_by_tenant_channel.setdefault(
                (str(row["tenant_id"]), str(row["channel"])), set()
            ).add(scope)

    # 旧全局 identity bug 可能让 tenantB session 指向 tenantA User。迁移时立即
    # 解除错误 User 关联并隔离会话，避免升级后被正常 external_conv_id 再次命中。
    if "sessions" in tables and "users" in tables:
        session_columns = {column["name"] for column in inspector.get_columns("sessions")}
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        if {"id", "tenant_id", "user_id", "channel", "external_conv_id"} <= session_columns and {
            "id",
            "tenant_id",
        } <= user_columns:
            polluted_rows = conn.execute(
                text(
                    "SELECT s.id, s.external_conv_id FROM sessions s JOIN users u ON u.id = s.user_id "
                    "WHERE s.channel IN ('wechat', 'wecom') AND s.tenant_id != u.tenant_id"
                )
            ).mappings().all()
            for polluted in polluted_rows:
                conn.execute(
                    text(
                        "UPDATE sessions SET user_id = NULL, external_conv_id = :conv "
                        "WHERE id = :id"
                    ),
                    {
                        "id": polluted["id"],
                        "conv": (
                            f"legacy_cross_tenant:{polluted['id']}:"
                            f"{polluted.get('external_conv_id') or ''}"
                        ),
                    },
                )

    identity_session_scopes: dict[tuple[str, str, str], set[str]] = {}
    session_ids_by_identity: dict[tuple[str, str, str], set[str]] = {}
    user_tenant_by_id: dict[str, str] | None = None
    if "users" in tables:
        user_columns = {column["name"] for column in inspector.get_columns("users")}
        if {"id", "tenant_id"} <= user_columns:
            user_tenant_by_id = {
                str(row["id"]): str(row["tenant_id"])
                for row in conn.execute(text("SELECT id, tenant_id FROM users")).mappings().all()
            }
    if "sessions" in tables:
        session_columns = {column["name"] for column in inspector.get_columns("sessions")}
        if {
            "tenant_id",
            "user_id",
            "channel",
            "channel_binding_id",
            "external_conv_id",
        } <= session_columns:
            session_rows = conn.execute(
                text(
                    "SELECT id, tenant_id, user_id, channel_binding_id, external_conv_id "
                    "FROM sessions WHERE channel = 'wecom' "
                    "AND user_id IS NOT NULL AND channel_binding_id IS NOT NULL"
                )
            ).mappings().all()
            for session_row in session_rows:
                scope = scope_by_binding_id.get(str(session_row["channel_binding_id"]))
                conv = str(session_row.get("external_conv_id") or "")
                if not scope:
                    continue
                if conv.startswith("wecom_p2p_"):
                    external_id = conv.removeprefix("wecom_p2p_")
                elif conv.startswith("wecom_group_"):
                    external_id = f"group:{conv.removeprefix('wecom_group_')}"
                else:
                    continue
                identity_key = (
                    str(session_row["tenant_id"]),
                    str(session_row["user_id"]),
                    external_id,
                )
                identity_session_scopes.setdefault(identity_key, set()).add(scope)
                session_ids_by_identity.setdefault(identity_key, set()).add(str(session_row["id"]))

    if "channel_identities" in tables:
        columns = {column["name"] for column in inspector.get_columns("channel_identities")}
        if "external_account_scope" not in columns:
            conn.execute(
                text(
                    """
                    CREATE TABLE channel_identities_new (
                        id VARCHAR PRIMARY KEY,
                        tenant_id VARCHAR,
                        channel VARCHAR,
                        external_account_scope VARCHAR NOT NULL DEFAULT '',
                        external_user_id VARCHAR NOT NULL,
                        staffdeck_user_id VARCHAR,
                        display_name VARCHAR,
                        created_at DATETIME,
                        updated_at DATETIME,
                        CONSTRAINT uq_channel_identity_scope_external UNIQUE (
                            tenant_id, channel, external_account_scope, external_user_id
                        )
                    )
                    """
                )
            )
            identity_owner_by_key: dict[tuple[str, str, str, str], tuple[str, str]] = {}
            for row in conn.execute(
                text("SELECT * FROM channel_identities ORDER BY id")
            ).mappings().all():
                external_user_id = str(row["external_user_id"])
                # wechat 是全局 wxid,scope 恒空。企微存量 identity 的 scope 回填优先级:
                # ①被会话引用(单 scope)按其会话所在 binding;多 scope 歧义留 legacy 并
                # 隔离相关会话;②无会话引用且该 tenant+channel 现存 scope 唯一时用之
                # (创建时间不能证明归属,多 scope 不猜);③取不到归 legacy。
                if str(row["channel"]) == "wecom":
                    if external_user_id.startswith("group_"):
                        external_user_id = f"group:{external_user_id.removeprefix('group_')}"
                    identity_key = (
                        str(row["tenant_id"]),
                        str(row.get("staffdeck_user_id") or ""),
                        external_user_id,
                    )
                    session_scopes = identity_session_scopes.get(identity_key, set())
                    if len(session_scopes) == 1:
                        scope = next(iter(session_scopes))
                    elif len(session_scopes) > 1:
                        scope = "legacy"
                        for session_id in session_ids_by_identity.get(identity_key, set()):
                            conn.execute(
                                text(
                                    "UPDATE sessions SET external_conv_id = :conv "
                                    "WHERE id = :id"
                                ),
                                {
                                    "id": session_id,
                                    "conv": (
                                        f"legacy_ambiguous_identity:{session_id}:"
                                        + str(
                                            next(
                                                (
                                                    session_row.get("external_conv_id")
                                                    for session_row in session_rows
                                                    if str(session_row["id"]) == session_id
                                                ),
                                                "",
                                            )
                                            or ""
                                        )
                                    ),
                                },
                            )
                    else:
                        tenant_scopes = scopes_by_tenant_channel.get(
                            (str(row["tenant_id"]), str(row["channel"])), set()
                        )
                        scope = next(iter(tenant_scopes)) if len(tenant_scopes) == 1 else "legacy"
                else:
                    scope = ""
                current_user_id = str(row.get("staffdeck_user_id") or "")
                if user_tenant_by_id is not None and user_tenant_by_id.get(
                    current_user_id
                ) != str(row["tenant_id"]):
                    # 丢失 User 或跨租户 User 指针不可进入正常 scope；保留记录供审计，
                    # 正常入站会在当前 tenant 重新建立 User 与 identity。
                    scope = "legacy_cross_tenant"
                unique_key = (
                    str(row["tenant_id"]),
                    str(row["channel"]),
                    scope,
                    external_user_id,
                )
                prior = identity_owner_by_key.get(unique_key)
                if prior:
                    if prior[0] == current_user_id:
                        continue
                    raise RuntimeError(
                        "渠道身份迁移冲突:规范化后同一身份指向不同 User "
                        f"identities={prior[1]},{row['id']}"
                    )
                identity_owner_by_key[unique_key] = (current_user_id, str(row["id"]))
                conn.execute(
                    text(
                        """
                        INSERT INTO channel_identities_new (
                            id, tenant_id, channel, external_account_scope, external_user_id,
                            staffdeck_user_id, display_name, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :channel, :scope, :external_user_id,
                            :staffdeck_user_id, :display_name, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "id": row["id"],
                        "tenant_id": row["tenant_id"],
                        "channel": row["channel"],
                        "scope": scope,
                        "external_user_id": external_user_id,
                        "staffdeck_user_id": row["staffdeck_user_id"],
                        "display_name": row.get("display_name"),
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("updated_at"),
                    },
                )
            conn.execute(text("DROP TABLE channel_identities"))
            conn.execute(text("ALTER TABLE channel_identities_new RENAME TO channel_identities"))
            for index_sql in (
                "CREATE INDEX IF NOT EXISTS ix_channel_identities_tenant_id ON channel_identities (tenant_id)",
                "CREATE INDEX IF NOT EXISTS ix_channel_identities_channel ON channel_identities (channel)",
                "CREATE INDEX IF NOT EXISTS ix_channel_identities_external_account_scope ON channel_identities (external_account_scope)",
                "CREATE INDEX IF NOT EXISTS ix_channel_identities_staffdeck_user_id ON channel_identities (staffdeck_user_id)",
            ):
                conn.execute(text(index_sql))

    if "channel_inbound_events" in tables:
        # 新形态判定:SQLite 内联唯一约束在 get_indexes 中不可见,按 sqlite_master 的 DDL 文本判定
        compact = _table_sql_compact(conn, "channel_inbound_events")
        if "UNIQUE(CHANNEL,EVENT_ID)" in compact:
            conn.execute(
                text(
                    """
                    CREATE TABLE channel_inbound_events_new (
                        id VARCHAR PRIMARY KEY,
                        tenant_id VARCHAR,
                        binding_id VARCHAR,
                        channel VARCHAR,
                        event_id VARCHAR NOT NULL,
                        payload_json JSON,
                        status VARCHAR,
                        processor_run_id VARCHAR,
                        error VARCHAR,
                        processed_at DATETIME,
                        created_at DATETIME,
                        updated_at DATETIME,
                        CONSTRAINT uq_channel_inbound_event_binding UNIQUE (binding_id, event_id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "INSERT INTO channel_inbound_events_new ("
                    "id, tenant_id, binding_id, channel, event_id, payload_json, status, "
                    "error, processed_at, created_at, updated_at) "
                    "SELECT id, tenant_id, binding_id, channel, event_id, payload_json, status, "
                    "error, processed_at, created_at, updated_at FROM channel_inbound_events"
                )
            )
            conn.execute(text("DROP TABLE channel_inbound_events"))
            conn.execute(
                text("ALTER TABLE channel_inbound_events_new RENAME TO channel_inbound_events")
            )
            for index_sql in (
                "CREATE INDEX IF NOT EXISTS ix_channel_inbound_events_tenant_id ON channel_inbound_events (tenant_id)",
                "CREATE INDEX IF NOT EXISTS ix_channel_inbound_events_binding_id ON channel_inbound_events (binding_id)",
                "CREATE INDEX IF NOT EXISTS ix_channel_inbound_events_channel ON channel_inbound_events (channel)",
                "CREATE INDEX IF NOT EXISTS ix_channel_inbound_events_status ON channel_inbound_events (status)",
                "CREATE INDEX IF NOT EXISTS ix_channel_inbound_events_processor_run_id ON channel_inbound_events (processor_run_id)",
            ):
                conn.execute(text(index_sql))

    if "sessions" in tables:
        # 列级守卫:老库 sessions 可能尚无渠道列(本函数内的 ALTER 在其后执行),
        # 没有这些列就不可能有 wecom 会话数据,直接跳过
        session_columns = {column["name"] for column in inspector.get_columns("sessions")}
        if {"channel_binding_id", "external_conv_id"} <= session_columns:
            rows = conn.execute(
                text(
                    "SELECT id, channel_binding_id, external_conv_id FROM sessions "
                    "WHERE external_conv_id LIKE 'wecom\\_p2p\\_%' ESCAPE '\\' "
                    "OR external_conv_id LIKE 'wecom\\_group\\_%' ESCAPE '\\'"
                )
            ).mappings().all()
            for row in rows:
                conv = str(row["external_conv_id"])
                scope = scope_by_binding_id.get(str(row["channel_binding_id"] or ""), "legacy")
                if conv.startswith("wecom_p2p_"):
                    new_conv = f"wecom_{scope}_p2p_{conv.removeprefix('wecom_p2p_')}"
                else:
                    new_conv = f"wecom_{scope}_group_{conv.removeprefix('wecom_group_')}"
                conn.execute(
                    text("UPDATE sessions SET external_conv_id = :conv WHERE id = :id"),
                    {"conv": new_conv, "id": row["id"]},
                )

    conn.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": _CHANNEL_SCOPE_REBUILD_MIGRATION_ID},
    )


def _table_sql_compact(conn, table_name: str) -> str:
    """sqlite_master 中表 DDL 的去空白大写文本(SQLite 内联约束在 get_indexes 不可见时用它判定)。"""
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table_name},
    ).first()
    if not row or not row[0]:
        return ""
    return "".join(str(row[0]).split()).upper()


def _migrate_channel_bindings_multi(conn, inspector, tables: set[str]) -> None:
    """channel_bindings 重建:移除 (agent_id, channel) 表级唯一约束,支持同 Agent 多渠道实例。"""
    if "channel_bindings" not in tables:
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
        {"id": _CHANNEL_BINDINGS_MULTI_MIGRATION_ID},
    ).first()
    if applied:
        return
    if "UNIQUE(AGENT_ID,CHANNEL)" in _table_sql_compact(conn, "channel_bindings"):
        conn.execute(
            text(
                """
                CREATE TABLE channel_bindings_new (
                    id VARCHAR PRIMARY KEY,
                    tenant_id VARCHAR,
                    agent_id VARCHAR,
                    channel VARCHAR,
                    status VARCHAR,
                    credentials_enc VARCHAR,
                    config_json JSON,
                    connected BOOLEAN,
                    created_by_user_id VARCHAR,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(text("INSERT INTO channel_bindings_new SELECT * FROM channel_bindings"))
        conn.execute(text("DROP TABLE channel_bindings"))
        conn.execute(text("ALTER TABLE channel_bindings_new RENAME TO channel_bindings"))
        for column in ("tenant_id", "agent_id", "channel", "status", "created_by_user_id"):
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_channel_bindings_{column} "
                    f"ON channel_bindings ({column})"
                )
            )
    conn.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": _CHANNEL_BINDINGS_MULTI_MIGRATION_ID},
    )


def _migrate_wechat_kf_accounts(conn, tables: set[str]) -> None:
    """Add the per-客服账号 routing table for multi-account WeChat客服 bindings."""
    if "wechat_kf_accounts" in tables:
        return
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS wechat_kf_accounts (
                id VARCHAR PRIMARY KEY,
                tenant_id VARCHAR NOT NULL,
                binding_id VARCHAR NOT NULL,
                open_kfid VARCHAR NOT NULL,
                name VARCHAR NOT NULL DEFAULT '',
                agent_id VARCHAR,
                team_id VARCHAR,
                status VARCHAR NOT NULL DEFAULT 'active',
                sync_cursor VARCHAR NOT NULL DEFAULT '',
                last_error VARCHAR,
                last_sync_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_wechat_kf_account_binding_kfid UNIQUE (binding_id, open_kfid),
                CONSTRAINT uq_wechat_kf_account_tenant_kfid UNIQUE (tenant_id, open_kfid)
            )
            """
        )
    )
    if "channel_bindings" in tables:
        rows = conn.execute(
            text(
                "SELECT id, tenant_id, agent_id, team_id, config_json "
                "FROM channel_bindings WHERE channel = 'wechat_kf'"
            )
        ).mappings().all()
        for row in rows:
            config = _json_object(row["config_json"])
            open_kfid = str(config.get("open_kfid") or "").strip()
            if not open_kfid:
                continue
            now = utc_now()
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO wechat_kf_accounts "
                    "(id, tenant_id, binding_id, open_kfid, agent_id, team_id, "
                    "status, sync_cursor, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :binding_id, :open_kfid, :agent_id, "
                    ":team_id, 'active', '', :created_at, :updated_at)"
                ),
                {
                    "id": new_id("wka"),
                    "tenant_id": row["tenant_id"],
                    "binding_id": row["id"],
                    "open_kfid": open_kfid,
                    "agent_id": row["agent_id"] if not row["team_id"] else None,
                    "team_id": row["team_id"],
                    "created_at": now,
                    "updated_at": now,
                },
            )
    for column in ("tenant_id", "binding_id", "open_kfid", "agent_id", "team_id", "status"):
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_wechat_kf_accounts_{column} "
                f"ON wechat_kf_accounts ({column})"
            )
        )


def _channel_account_key_from_row(channel: str, config: object) -> str | None:
    parsed = _json_object(config)
    if channel == "wecom":
        corp_id = str(parsed.get("corp_id") or "").strip()
        bot_id = str(parsed.get("bot_id") or "").strip()
        if corp_id and bot_id:
            return f"wecom:corp:{len(corp_id)}:{corp_id}:bot:{len(bot_id)}:{bot_id}"
        return f"wecom:bot:{bot_id}" if bot_id else None
    if channel == "wechat":
        bot_id = str(parsed.get("ilink_bot_id") or "").strip()
        return f"wechat:ilink_bot:{bot_id}" if bot_id else None
    return None


def _migrate_channel_inbound_run_schema(conn, tables: set[str]) -> None:
    if "channel_inbound_events" not in tables:
        return
    columns = {
        str(row[1])
        for row in conn.execute(text("PRAGMA table_info(channel_inbound_events)")).all()
    }
    if "processor_run_id" not in columns:
        conn.execute(text("ALTER TABLE channel_inbound_events ADD COLUMN processor_run_id VARCHAR"))
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_channel_inbound_events_processor_run_id "
            "ON channel_inbound_events(processor_run_id)"
        )
    )


def _migrate_feishu_channel_schema(conn, tables: set[str]) -> None:
    """Add the durable-inbox and provider-scope columns required by Feishu.

    Column presence is authoritative rather than the marker alone so an interrupted
    or manually modified database is repaired on the next startup.
    """
    required_tables = {
        "channel_bindings",
        "channel_inbound_events",
        "channel_deliveries",
    }
    if not required_tables <= tables:
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
    binding_columns = {
        str(row[1]) for row in conn.execute(text("PRAGMA table_info(channel_bindings)"))
    }
    if "provider_tenant_key" not in binding_columns:
        conn.execute(
            text("ALTER TABLE channel_bindings ADD COLUMN provider_tenant_key VARCHAR")
        )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_channel_bindings_provider_tenant_key "
            "ON channel_bindings(provider_tenant_key)"
        )
    )

    inbound_columns = {
        str(row[1])
        for row in conn.execute(text("PRAGMA table_info(channel_inbound_events)"))
    }
    if "config_revision" not in inbound_columns:
        conn.execute(
            text(
                "ALTER TABLE channel_inbound_events ADD COLUMN config_revision "
                "INTEGER NOT NULL DEFAULT 0"
            )
        )
    if "target_json" not in inbound_columns:
        conn.execute(
            text(
                "ALTER TABLE channel_inbound_events ADD COLUMN target_json "
                "JSON NOT NULL DEFAULT '{}'"
            )
        )
    if "reaction_id" not in inbound_columns:
        conn.execute(
            text("ALTER TABLE channel_inbound_events ADD COLUMN reaction_id VARCHAR")
        )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_channel_inbound_events_reaction_id "
            "ON channel_inbound_events(reaction_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_channel_inbound_events_binding_status_created "
            "ON channel_inbound_events(binding_id, status, created_at)"
        )
    )

    delivery_columns = {
        str(row[1]) for row in conn.execute(text("PRAGMA table_info(channel_deliveries)"))
    }
    if "first_attempt_at" not in delivery_columns:
        conn.execute(
            text("ALTER TABLE channel_deliveries ADD COLUMN first_attempt_at DATETIME")
        )

    conn.execute(
        text(
            "INSERT OR IGNORE INTO app_data_migrations (id) VALUES (:id)"
        ),
        {"id": _FEISHU_CHANNEL_SCHEMA_MIGRATION_ID},
    )


def _migrate_channel_bind_code_constraints(conn, tables: set[str]) -> None:
    if "channel_bind_codes" not in tables:
        return
    # 已使用/过期码不再参与当前码模型，先清理以释放六位码空间。
    conn.execute(
        text(
            "DELETE FROM channel_bind_codes "
            "WHERE used_at IS NOT NULL OR expires_at <= CURRENT_TIMESTAMP"
        )
    )
    # 同一码存在多个 owner 时归属已不可信，整组作废，要求用户重新生成。
    conn.execute(
        text(
            "DELETE FROM channel_bind_codes WHERE (tenant_id, code) IN ("
            "SELECT tenant_id, code FROM channel_bind_codes "
            "GROUP BY tenant_id, code HAVING COUNT(*) > 1)"
        )
    )
    # 同一用户多个有效码只保留最新一条。
    conn.execute(
        text(
            "DELETE FROM channel_bind_codes AS older WHERE EXISTS ("
            "SELECT 1 FROM channel_bind_codes AS newer "
            "WHERE newer.tenant_id = older.tenant_id "
            "AND newer.user_id = older.user_id "
            "AND (COALESCE(newer.created_at, '') > COALESCE(older.created_at, '') "
            "OR (COALESCE(newer.created_at, '') = COALESCE(older.created_at, '') "
            "AND newer.id > older.id)))"
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_bind_code_tenant_code "
            "ON channel_bind_codes(tenant_id, code)"
        )
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_bind_code_tenant_user "
            "ON channel_bind_codes(tenant_id, user_id)"
        )
    )


def _migrate_channel_account_key_schema(conn, tables: set[str]) -> None:
    """为渠道绑定补稳定账号键、身份 scope 与 revision，并回填会话账号锚点。"""
    if "channel_bindings" not in tables:
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
    columns = {
        str(row[1]) for row in conn.execute(text("PRAGMA table_info(channel_bindings)")).all()
    }
    if "external_account_key" not in columns:
        conn.execute(text("ALTER TABLE channel_bindings ADD COLUMN external_account_key VARCHAR"))
    if "identity_scope_key" not in columns:
        conn.execute(text("ALTER TABLE channel_bindings ADD COLUMN identity_scope_key VARCHAR"))
    if "config_revision" not in columns:
        conn.execute(
            text(
                "ALTER TABLE channel_bindings ADD COLUMN config_revision "
                "INTEGER NOT NULL DEFAULT 0"
            )
        )

    rows = conn.execute(
        text("SELECT id, channel, config_json, external_account_key FROM channel_bindings")
    ).mappings().all()
    seen: dict[str, str] = {}
    legacy_wecom_bots: dict[str, str] = {}
    scoped_wecom_bots: dict[str, str] = {}
    for row in rows:
        config = _json_object(row.get("config_json"))
        if str(row["channel"]) == "wecom":
            bot_id = str(config.get("bot_id") or "").strip()
            corp_id = str(config.get("corp_id") or "").strip()
            if bot_id and corp_id:
                legacy_owner = legacy_wecom_bots.get(bot_id)
                if legacy_owner:
                    raise RuntimeError(
                        "检测到缺少 corp_id 的存量 Bot 与企业 Bot 冲突: "
                        f"bot_id={bot_id} bindings={legacy_owner},{row['id']}"
                    )
                scoped_wecom_bots.setdefault(bot_id, str(row["id"]))
            elif bot_id:
                scoped_owner = scoped_wecom_bots.get(bot_id)
                if scoped_owner:
                    raise RuntimeError(
                        "检测到缺少 corp_id 的存量 Bot 与企业 Bot 冲突: "
                        f"bot_id={bot_id} bindings={row['id']},{scoped_owner}"
                    )
                legacy_wecom_bots.setdefault(bot_id, str(row["id"]))
        derived_account_key = _channel_account_key_from_row(
            str(row["channel"]), row.get("config_json")
        )
        account_key = (
            derived_account_key
            or str(row.get("external_account_key") or "").strip()
            or None
        )
        if account_key:
            owner = seen.get(account_key)
            if owner and owner != str(row["id"]):
                raise RuntimeError(
                    "检测到同一外部 Bot 被多个 binding 使用: "
                    f"account_key={account_key} bindings={owner},{row['id']}"
                )
            seen[account_key] = str(row["id"])
        if str(row["channel"]) == "wecom":
            scope_key = str(config.get("corp_id") or config.get("bot_id") or row["id"]).strip()
        else:
            scope_key = ""
        conn.execute(
            text(
                "UPDATE channel_bindings SET external_account_key = :account_key, "
                "identity_scope_key = :scope_key WHERE id = :id"
            ),
            {"id": row["id"], "account_key": account_key, "scope_key": scope_key},
        )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_bindings_external_account_key "
            "ON channel_bindings(external_account_key) WHERE external_account_key IS NOT NULL"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_channel_bindings_identity_scope_key "
            "ON channel_bindings(identity_scope_key)"
        )
    )
    applied = conn.execute(
        text("SELECT id FROM app_data_migrations WHERE id = :id"),
        {"id": _CHANNEL_ACCOUNT_KEY_MIGRATION_ID},
    ).first()
    if not applied:
        conn.execute(
            text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
            {"id": _CHANNEL_ACCOUNT_KEY_MIGRATION_ID},
        )


def _migrate_model_api_protocols(conn, tables: set[str]) -> None:
    if "model_configs" not in tables:
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
        {"id": _MODEL_API_PROTOCOLS_MIGRATION_ID},
    ).first()
    columns = {
        str(row[1]) for row in conn.execute(text("PRAGMA table_info(model_configs)")).all()
    }
    if applied and _model_api_protocol_schema_complete(conn, columns):
        return
    repairing_applied_migration = bool(applied)
    if "extra_body_json" not in columns:
        conn.execute(text("ALTER TABLE model_configs ADD COLUMN extra_body_json JSON"))
        conn.execute(text("UPDATE model_configs SET extra_body_json = '{}'"))
        columns.add("extra_body_json")
    column_ddl = {
        "api_protocol": (
            "ALTER TABLE model_configs ADD COLUMN api_protocol VARCHAR "
            "NOT NULL DEFAULT 'openai_chat_completions'"
        ),
        "protocol_options_json": "ALTER TABLE model_configs ADD COLUMN protocol_options_json JSON",
        "legacy_unmapped_options_json": (
            "ALTER TABLE model_configs ADD COLUMN legacy_unmapped_options_json JSON"
        ),
        "trust_status": (
            "ALTER TABLE model_configs ADD COLUMN trust_status VARCHAR "
            "NOT NULL DEFAULT 'unverified'"
        ),
        "verified_at": "ALTER TABLE model_configs ADD COLUMN verified_at DATETIME",
        "verified_fingerprint": (
            "ALTER TABLE model_configs ADD COLUMN verified_fingerprint VARCHAR"
        ),
        "verification_attempt_id": (
            "ALTER TABLE model_configs ADD COLUMN verification_attempt_id VARCHAR"
        ),
        "verification_started_at": (
            "ALTER TABLE model_configs ADD COLUMN verification_started_at DATETIME"
        ),
        "verification_attempt_status": (
            "ALTER TABLE model_configs ADD COLUMN verification_attempt_status VARCHAR "
            "NOT NULL DEFAULT 'idle'"
        ),
        "verification_attempt_error_code": (
            "ALTER TABLE model_configs ADD COLUMN verification_attempt_error_code VARCHAR"
        ),
        "config_revision": (
            "ALTER TABLE model_configs ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 1"
        ),
        "security_revision": (
            "ALTER TABLE model_configs ADD COLUMN security_revision INTEGER NOT NULL DEFAULT 1"
        ),
        "key_revision": (
            "ALTER TABLE model_configs ADD COLUMN key_revision INTEGER NOT NULL DEFAULT 1"
        ),
    }
    for column_name, ddl in column_ddl.items():
        if column_name not in columns:
            conn.execute(text(ddl))

    if not repairing_applied_migration:
        rows = conn.execute(
            text("SELECT id, enabled, extra_body_json FROM model_configs")
        ).mappings().all()
        for row in rows:
            extra_body = _json_object(row.get("extra_body_json"))
            thinking = extra_body.get("thinking")
            protocol_options: dict[str, object] = {"openai_chat_completions": {}}
            legacy_unmapped: dict[str, object] = {}
            if _valid_chat_thinking_options(thinking):
                protocol_options["openai_chat_completions"] = {"thinking": thinking}
                legacy_unmapped = {
                    key: value for key, value in extra_body.items() if key != "thinking"
                }
            elif extra_body:
                legacy_unmapped = extra_body
            conn.execute(
                text(
                    """
                    UPDATE model_configs
                    SET api_protocol = 'openai_chat_completions',
                        protocol_options_json = :protocol_options,
                        legacy_unmapped_options_json = :legacy_unmapped,
                        trust_status = CASE WHEN enabled = 1 THEN 'legacy_trusted' ELSE 'unverified' END,
                        verification_attempt_status = 'idle',
                        config_revision = 1,
                        security_revision = 1,
                        key_revision = 1
                    WHERE id = :id
                    """
                ),
                {
                    "id": row["id"],
                    "protocol_options": json.dumps(protocol_options, ensure_ascii=False),
                    "legacy_unmapped": json.dumps(legacy_unmapped, ensure_ascii=False),
                },
            )

    _normalize_model_default_rows(conn)
    if repairing_applied_migration:
        return


def _normalize_model_default_rows(conn) -> None:
    duplicate_defaults = conn.execute(
        text(
            """
            SELECT tenant_id
            FROM model_configs
            WHERE is_default = 1 AND enabled = 1
            GROUP BY tenant_id
            HAVING COUNT(*) > 1
            """
        )
    ).scalars().all()
    for tenant_id in duplicate_defaults:
        keep_id = conn.execute(
            text(
                """
                SELECT id FROM model_configs
                WHERE tenant_id = :tenant_id AND is_default = 1 AND enabled = 1
                ORDER BY updated_at DESC, id ASC
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id},
        ).scalar_one()
        conn.execute(
            text(
                """
                UPDATE model_configs SET is_default = 0
                WHERE tenant_id = :tenant_id AND is_default = 1 AND id != :keep_id
                """
            ),
            {"tenant_id": tenant_id, "keep_id": keep_id},
        )
    conn.execute(text("UPDATE model_configs SET is_default = 0 WHERE enabled = 0"))
    conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_model_configs_tenant_default
            ON model_configs(tenant_id) WHERE is_default = 1
            """
        )
    )
    conn.execute(
        text(
            "INSERT OR IGNORE INTO app_data_migrations (id) VALUES (:id)"
        ),
        {"id": _MODEL_API_PROTOCOLS_MIGRATION_ID},
    )


def _model_api_protocol_schema_complete(conn, columns: set[str]) -> bool:
    if not _MODEL_API_PROTOCOL_COLUMNS.issubset(columns):
        return False
    index = conn.execute(
        text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'uq_model_configs_tenant_default'"
        )
    ).scalar_one_or_none()
    return bool(index and "WHERE is_default = 1" in index)


def _valid_chat_thinking_options(value: object) -> bool:
    if not isinstance(value, dict) or set(value) - {"type", "clear_thinking"}:
        return False
    if value.get("type") not in {"enabled", "disabled"}:
        return False
    return "clear_thinking" not in value or isinstance(value["clear_thinking"], bool)


def _migrate_skill_content(value: object, skill_id: str) -> dict[str, object]:
    if isinstance(value, str):
        try:
            content = json.loads(value)
        except json.JSONDecodeError:
            content = {}
    elif isinstance(value, dict):
        content = dict(value)
    else:
        content = {}
    if "skill_id" not in content:
        content["skill_id"] = content.pop("so" + "p_id", skill_id)
    else:
        content["skill_id"] = skill_id
    return _ensure_skill_graph(content)


def _normalize_existing_skill_rows(conn, legacy_id_prefix: str) -> None:
    rows = conn.execute(text("SELECT id, skill_id, content_json FROM skills")).mappings().all()
    for row in rows:
        skill_id = _normalize_skill_identifier(row.get("skill_id"), legacy_id_prefix)
        if not skill_id:
            continue
        content = _migrate_skill_content(row.get("content_json"), skill_id)
        if skill_id == row.get("skill_id"):
            conn.execute(
                text("UPDATE skills SET content_json = :content_json WHERE id = :id"),
                {"id": row["id"], "content_json": json.dumps(content, ensure_ascii=False)},
            )
            continue
        existing = conn.execute(
            text("SELECT id FROM skills WHERE skill_id = :skill_id AND id != :id"),
            {"skill_id": skill_id, "id": row["id"]},
        ).first()
        if existing:
            continue
        conn.execute(
            text("UPDATE skills SET skill_id = :skill_id, content_json = :content_json WHERE id = :id"),
            {
                "id": row["id"],
                "skill_id": skill_id,
                "content_json": json.dumps(content, ensure_ascii=False),
            },
        )


def _normalize_existing_skill_version_rows(conn, legacy_id_prefix: str) -> None:
    rows = conn.execute(text("SELECT id, skill_id, content_json FROM skill_versions")).mappings().all()
    for row in rows:
        skill_id = _normalize_skill_identifier(row.get("skill_id"), legacy_id_prefix)
        if not skill_id:
            continue
        content = _migrate_skill_content(row.get("content_json"), skill_id)
        conn.execute(
            text("UPDATE skill_versions SET skill_id = :skill_id, content_json = :content_json WHERE id = :id"),
            {
                "id": row["id"],
                "skill_id": skill_id,
                "content_json": json.dumps(content, ensure_ascii=False),
            },
        )


def _sync_explicit_skill_tool_bindings(conn, tables: set[str]) -> None:
    if "skills" not in tables or "tools" not in tables:
        return
    skill_rows = conn.execute(
        text(
            "SELECT tenant_id, skill_id, content_json FROM skills "
            "WHERE status IS NULL OR status != 'deleted'"
        )
    ).mappings().all()
    for skill_row in skill_rows:
        content = _json_object(skill_row.get("content_json"))
        tool_names = _explicit_skill_tool_names(content)
        if not tool_names:
            continue
        tool_rows = conn.execute(
            text("SELECT id, name, allowed_skills_json FROM tools WHERE tenant_id = :tenant_id"),
            {"tenant_id": skill_row["tenant_id"]},
        ).mappings().all()
        for tool_row in tool_rows:
            if str(tool_row.get("name") or "") not in tool_names:
                continue
            allowed_skills = _json_string_list(tool_row.get("allowed_skills_json"))
            skill_id = str(skill_row.get("skill_id") or "").strip()
            if not skill_id or skill_id in allowed_skills:
                continue
            allowed_skills.append(skill_id)
            conn.execute(
                text(
                    "UPDATE tools SET allowed_skills_json = :allowed_skills, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = :id"
                ),
                {
                    "id": tool_row["id"],
                    "allowed_skills": json.dumps(allowed_skills, ensure_ascii=False),
                },
            )


def _explicit_skill_tool_names(content: dict[str, object]) -> set[str]:
    names: set[str] = set()
    for key in ("nodes", "steps"):
        items = content.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            actions = item.get("allowed_actions")
            if not isinstance(actions, list):
                continue
            for action in actions:
                value = str(action or "").strip()
                if value.startswith("call_tool:"):
                    name = value.split(":", 1)[1].strip()
                    if name:
                        names.add(name)
    return names


def _json_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _ensure_skill_graph(content: dict[str, object]) -> dict[str, object]:
    nodes = content.get("nodes")
    steps = content.get("steps")
    if isinstance(nodes, list) and nodes:
        _ensure_required_capability_refs(nodes)
        content.pop("steps", None)
        content.setdefault("start_node_id", _first_node_id(nodes))
        content.setdefault("terminal_node_ids", [_last_node_id(nodes)] if _last_node_id(nodes) else [])
        return content
    if not isinstance(steps, list) or not steps:
        content.setdefault("nodes", [])
        content.setdefault("edges", [])
        content.setdefault("terminal_node_ids", [])
        content.pop("steps", None)
        return content
    normalized_steps = [step for step in steps if isinstance(step, dict)]
    content["nodes"] = [_step_to_node_dict(step) for step in normalized_steps]
    content["edges"] = [
        {
            "source_node_id": str(normalized_steps[index].get("step_id") or f"step_{index + 1}"),
            "next_node_id": str(normalized_steps[index + 1].get("step_id") or f"step_{index + 2}"),
            "priority": index,
            "label": "默认推进",
        }
        for index in range(len(normalized_steps) - 1)
    ]
    if normalized_steps:
        content["start_node_id"] = content.get("start_node_id") or str(normalized_steps[0].get("step_id") or "step_1")
        content["terminal_node_ids"] = content.get("terminal_node_ids") or [
            str(normalized_steps[-1].get("step_id") or f"step_{len(normalized_steps)}")
        ]
    content.pop("steps", None)
    return content


def _ensure_required_capability_refs(nodes: list[object]) -> None:
    """Repair legacy nodes where a required capability was not also selected."""

    for node in nodes:
        if not isinstance(node, dict):
            continue
        refs = node.get("capability_refs")
        if not isinstance(refs, dict):
            continue
        for required_field, selected_field in (
            ("required_general_skill_ids", "general_skill_ids"),
            ("required_tool_ids", "tool_ids"),
            ("required_knowledge_base_ids", "knowledge_base_ids"),
        ):
            required = refs.get(required_field)
            if not isinstance(required, list):
                continue
            selected = refs.get(selected_field)
            selected_values = list(selected) if isinstance(selected, list) else []
            for capability_id in required:
                if capability_id not in selected_values:
                    selected_values.append(capability_id)
            refs[selected_field] = selected_values


def _step_to_node_dict(step: dict[str, object]) -> dict[str, object]:
    actions = step.get("allowed_actions") if isinstance(step.get("allowed_actions"), list) else []
    expected = step.get("expected_user_info") if isinstance(step.get("expected_user_info"), list) else []
    node_type = "collect_info" if expected else "response"
    if any(isinstance(action, str) and action.startswith("call_tool:") for action in actions):
        node_type = "tool_call"
    if "handoff_human" in actions:
        node_type = "handoff"
    return {
        "node_id": str(step.get("step_id") or step.get("node_id") or "step"),
        "type": node_type,
        "name": str(step.get("name") or step.get("step_id") or "步骤"),
        "instruction": str(step.get("instruction") or ""),
        "optional": bool(step.get("optional") or False),
        "condition": step.get("condition") if isinstance(step.get("condition"), str) else None,
        "expected_user_info": expected,
        "allowed_actions": actions,
        "knowledge_scope": step.get("knowledge_scope") if isinstance(step.get("knowledge_scope"), dict) else {},
        "retry_policy": step.get("retry_policy") if isinstance(step.get("retry_policy"), dict) else {},
        "metadata": step.get("metadata") if isinstance(step.get("metadata"), dict) else {},
    }


def _first_node_id(nodes: object) -> str | None:
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if isinstance(node, dict) and node.get("node_id"):
            return str(node["node_id"])
    return None


def _last_node_id(nodes: object) -> str | None:
    if not isinstance(nodes, list):
        return None
    for node in reversed(nodes):
        if isinstance(node, dict) and node.get("node_id"):
            return str(node["node_id"])
    return None


def _seed_skill_versions(conn) -> None:
    rows = conn.execute(text("SELECT * FROM skills")).mappings().all()
    for row in rows:
        version = row.get("version") or "1.0.0"
        existing = conn.execute(
            text(
                """
                SELECT id FROM skill_versions
                WHERE tenant_id = :tenant_id AND skill_id = :skill_id AND version = :version
                """
            ),
            {"tenant_id": row["tenant_id"], "skill_id": row["skill_id"], "version": version},
        ).first()
        if existing:
            continue
        conn.execute(
            text(
                """
                INSERT INTO skill_versions (
                    id, tenant_id, skill_id, version, name, business_domain,
                    description, content_json, status, created_at, updated_at
                )
                VALUES (
                    :id, :tenant_id, :skill_id, :version, :name, :business_domain,
                    :description, :content_json, :status, :created_at, :updated_at
                )
                """
            ),
            {
                "id": f"skillver_{row['id']}",
                "tenant_id": row["tenant_id"],
                "skill_id": row["skill_id"],
                "version": version,
                "name": row["name"],
                "business_domain": row.get("business_domain"),
                "description": row.get("description"),
                "content_json": row.get("content_json"),
                "status": row.get("status") or "draft",
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            },
        )


def _normalize_skill_identifier(value: object, legacy_id_prefix: str) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith(legacy_id_prefix):
        return f"skill_{value[len(legacy_id_prefix):]}"
    return value


def _migrate_capability_scope_schema(conn, inspector, tables: set[str]) -> None:
    for table_name in _CAPABILITY_SCOPE_TABLES:
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "capability_scope" not in columns:
            conn.execute(
                text(
                    f"ALTER TABLE {table_name} ADD COLUMN capability_scope "
                    "VARCHAR NOT NULL DEFAULT 'general'"
                )
            )
        conn.execute(
            text(
                f"UPDATE {table_name} SET capability_scope = 'general' "
                "WHERE capability_scope IS NULL "
                "OR capability_scope NOT IN ('general', 'sop_specific')"
            )
        )
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_{table_name}_capability_scope "
                f"ON {table_name}(capability_scope)"
            )
        )


def _migrate_harness_v2_schema(conn, inspector, tables: set[str]) -> None:
    """Repair Harness v2 tables created by an older application version."""

    if "harness_task_frames" in tables:
        task_frame_columns = {
            column["name"] for column in inspector.get_columns("harness_task_frames")
        }
        task_frame_column_sql = {
            "agent_loop_id": (
                "ALTER TABLE harness_task_frames ADD COLUMN agent_loop_id VARCHAR"
            ),
            "decision": (
                "ALTER TABLE harness_task_frames ADD COLUMN decision "
                "VARCHAR NOT NULL DEFAULT 'answer_only'"
            ),
            "attempt_no": (
                "ALTER TABLE harness_task_frames ADD COLUMN attempt_no "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "lease_owner": (
                "ALTER TABLE harness_task_frames ADD COLUMN lease_owner VARCHAR"
            ),
            "lease_expires_at": (
                "ALTER TABLE harness_task_frames ADD COLUMN lease_expires_at DATETIME"
            ),
        }
        for column_name, ddl in task_frame_column_sql.items():
            if column_name not in task_frame_columns:
                conn.execute(text(ddl))
        conn.execute(
            text(
                "UPDATE harness_task_frames SET decision = 'answer_only' "
                "WHERE decision IS NULL OR decision = ''"
            )
        )
        conn.execute(
            text(
                "UPDATE harness_task_frames SET attempt_no = 0 "
                "WHERE attempt_no IS NULL"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_harness_task_frames_decision "
                "ON harness_task_frames(decision)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_harness_task_frames_lease_owner "
                "ON harness_task_frames(lease_owner)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_harness_task_frames_lease_expires_at "
                "ON harness_task_frames(lease_expires_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_harness_task_frames_agent_loop_id "
                "ON harness_task_frames(agent_loop_id)"
            )
        )

    if "harness_runs" in tables:
        run_columns = {
            column["name"] for column in inspector.get_columns("harness_runs")
        }
        run_column_sql = {
            "agent_loop_id": "ALTER TABLE harness_runs ADD COLUMN agent_loop_id VARCHAR",
            "attempt_no": (
                "ALTER TABLE harness_runs ADD COLUMN attempt_no "
                "INTEGER NOT NULL DEFAULT 1"
            ),
            "lease_owner": "ALTER TABLE harness_runs ADD COLUMN lease_owner VARCHAR",
            "lease_expires_at": (
                "ALTER TABLE harness_runs ADD COLUMN lease_expires_at DATETIME"
            ),
        }
        for column_name, ddl in run_column_sql.items():
            if column_name not in run_columns:
                conn.execute(text(ddl))
        conn.execute(
            text(
                "UPDATE harness_runs SET attempt_no = 1 "
                "WHERE attempt_no IS NULL"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_harness_runs_lease_owner "
                "ON harness_runs(lease_owner)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_harness_runs_lease_expires_at "
                "ON harness_runs(lease_expires_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_harness_runs_agent_loop_id "
                "ON harness_runs(agent_loop_id)"
            )
        )

    if "harness_invocations" not in tables:
        return

    invocation_columns = {
        column["name"] for column in inspector.get_columns("harness_invocations")
    }
    invocation_column_sql = {
        "logical_action_key": (
            "ALTER TABLE harness_invocations ADD COLUMN logical_action_key VARCHAR"
        ),
        "replayed_from_invocation_id": (
            "ALTER TABLE harness_invocations "
            "ADD COLUMN replayed_from_invocation_id VARCHAR"
        ),
        "response_cache_json": (
            "ALTER TABLE harness_invocations ADD COLUMN response_cache_json "
            "JSON NOT NULL DEFAULT '{}'"
        ),
    }
    for column_name, ddl in invocation_column_sql.items():
        if column_name not in invocation_columns:
            conn.execute(text(ddl))
    conn.execute(
        text(
            "UPDATE harness_invocations SET response_cache_json = '{}' "
            "WHERE response_cache_json IS NULL"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS "
            "ix_harness_invocations_replayed_from_invocation_id "
            "ON harness_invocations(replayed_from_invocation_id)"
        )
    )

    logical_action_index = next(
        (
            index
            for index in inspector.get_indexes("harness_invocations")
            if index["name"] == "ix_harness_invocations_logical_action_key"
        ),
        None,
    )
    if logical_action_index and (
        not logical_action_index.get("unique")
        or logical_action_index.get("column_names") != ["logical_action_key"]
    ):
        conn.execute(
            text(
                "DROP INDEX IF EXISTS "
                "ix_harness_invocations_logical_action_key"
            )
        )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ix_harness_invocations_logical_action_key "
            "ON harness_invocations(logical_action_key) "
            "WHERE logical_action_key IS NOT NULL"
        )
    )


def _migrate_knowledge_base_schema(conn, inspector, tables: set[str]) -> None:
    tenant_ids = _tenant_ids(conn, tables)
    if "knowledge_bases" in tables:
        for tenant_id in tenant_ids:
            default_id = _default_knowledge_base_id(tenant_id)
            existing = conn.execute(
                text("SELECT id FROM knowledge_bases WHERE id = :id"),
                {"id": default_id},
            ).first()
            if not existing:
                extra_columns, extra_values = _gallery_scope_insert_columns(
                    conn, "knowledge_bases"
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_bases (
                            id, tenant_id, name, description, status, capability_scope,
                            metadata_json, created_at, updated_at"""
                        + extra_columns
                        + """
                        )
                        VALUES (
                            :id, :tenant_id, :name, :description, 'active', 'general',
                            '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"""
                        + extra_values
                        + """
                        )
                        """
                    ),
                    {
                        "id": default_id,
                        "tenant_id": tenant_id,
                        "name": "默认知识库",
                        "description": "系统默认知识库",
                    },
                )

    table_names = {
        "knowledge_documents": "knowledge_base_id",
        "knowledge_buckets": "knowledge_base_id",
        "knowledge_chunks": "knowledge_base_id",
        "knowledge_concepts": "knowledge_base_id",
        "knowledge_discovery_suggestions": "knowledge_base_id",
        "knowledge_ingest_jobs": "knowledge_base_id",
    }
    for table_name, column_name in table_names.items():
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if column_name not in columns:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} VARCHAR"))
        rows = conn.execute(
            text(f"SELECT DISTINCT tenant_id FROM {table_name} WHERE {column_name} IS NULL OR {column_name} = ''")
        ).mappings().all()
        for row in rows:
            tenant_id = str(row.get("tenant_id") or "")
            if tenant_id:
                conn.execute(
                    text(f"UPDATE {table_name} SET {column_name} = :knowledge_base_id WHERE tenant_id = :tenant_id AND ({column_name} IS NULL OR {column_name} = '')"),
                    {"tenant_id": tenant_id, "knowledge_base_id": _default_knowledge_base_id(tenant_id)},
                )

    resolved_version_ids: dict[str, str] = {}
    if "knowledge_base_versions" in tables and "knowledge_bases" in tables:
        knowledge_bases = conn.execute(text("SELECT * FROM knowledge_bases")).mappings().all()
        for row in knowledge_bases:
            knowledge_base_id = str(row["id"])
            version_id = _knowledge_base_version_id(knowledge_base_id, "1.0.0")
            existing = conn.execute(
                text(
                    """
                    SELECT id FROM knowledge_base_versions
                    WHERE id = :id
                       OR (
                            tenant_id = :tenant_id
                            AND knowledge_base_id = :knowledge_base_id
                            AND version = '1.0.0'
                       )
                    """
                ),
                {
                    "id": version_id,
                    "tenant_id": row["tenant_id"],
                    "knowledge_base_id": knowledge_base_id,
                },
            ).first()
            if existing:
                version_id = str(existing[0])
            else:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_base_versions (
                            id, tenant_id, knowledge_base_id, version, name, description,
                            status, capability_scope, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :knowledge_base_id, '1.0.0', :name, :description,
                            :status, :capability_scope, :metadata_json,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": version_id,
                        "tenant_id": row["tenant_id"],
                        "knowledge_base_id": knowledge_base_id,
                        "name": row["name"],
                        "description": row.get("description"),
                        "status": row.get("status") or "active",
                        "capability_scope": (
                            row.get("capability_scope")
                            if row.get("capability_scope") in {"general", "sop_specific"}
                            else "general"
                        ),
                        "metadata_json": row.get("metadata_json") or "{}",
                    },
                )
            resolved_version_ids[knowledge_base_id] = version_id

    for table_name in table_names:
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "knowledge_base_version_id" not in columns:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN knowledge_base_version_id VARCHAR"))
        rows = conn.execute(
            text(
                f"""
                SELECT DISTINCT knowledge_base_id FROM {table_name}
                WHERE knowledge_base_id IS NOT NULL
                  AND knowledge_base_id != ''
                  AND (knowledge_base_version_id IS NULL OR knowledge_base_version_id = '')
                """
            )
        ).mappings().all()
        for row in rows:
            knowledge_base_id = str(row.get("knowledge_base_id") or "")
            if not knowledge_base_id:
                continue
            conn.execute(
                text(
                    f"""
                    UPDATE {table_name}
                    SET knowledge_base_version_id = :version_id
                    WHERE knowledge_base_id = :knowledge_base_id
                      AND (knowledge_base_version_id IS NULL OR knowledge_base_version_id = '')
                    """
                ),
                {
                    "knowledge_base_id": knowledge_base_id,
                    "version_id": resolved_version_ids.get(
                        knowledge_base_id,
                        _knowledge_base_version_id(knowledge_base_id, "1.0.0"),
                    ),
                },
            )

    _split_document_backed_knowledge_bases(conn, tables)


def _split_document_backed_knowledge_bases(conn, tables: set[str]) -> None:
    required_tables = {"knowledge_bases", "knowledge_base_versions", "knowledge_documents"}
    if not required_tables.issubset(tables):
        return

    document_groups = conn.execute(
        text(
            """
            SELECT knowledge_base_id, COUNT(id) AS document_count
            FROM knowledge_documents
            WHERE knowledge_base_id IS NOT NULL AND knowledge_base_id != ''
            GROUP BY knowledge_base_id
            """
        )
    ).mappings().all()
    multi_document_base_ids = {
        str(row["knowledge_base_id"])
        for row in document_groups
        if int(row.get("document_count") or 0) > 1
    }
    if not multi_document_base_ids:
        return

    for source_knowledge_base_id in sorted(multi_document_base_ids):
        source = conn.execute(
            text("SELECT * FROM knowledge_bases WHERE id = :id"),
            {"id": source_knowledge_base_id},
        ).mappings().first()
        if not source:
            continue
        documents = conn.execute(
            text(
                """
                SELECT *
                FROM knowledge_documents
                WHERE knowledge_base_id = :knowledge_base_id
                ORDER BY created_at, id
                """
            ),
            {"knowledge_base_id": source_knowledge_base_id},
        ).mappings().all()
        if len(documents) <= 1:
            continue
        for document in documents:
            target_id = _document_knowledge_base_id(str(document["id"]))
            target = conn.execute(
                text("SELECT id FROM knowledge_bases WHERE id = :id"),
                {"id": target_id},
            ).first()
            target_name = _unique_migrated_knowledge_base_name(
                conn,
                str(source["tenant_id"]),
                _document_knowledge_base_name(document),
                target_id,
            )
            metadata = _json_object(source.get("metadata_json"))
            metadata.update(
                {
                    "created_from_document_upload": True,
                    "source_document_id": document["id"],
                    "source_filename": document.get("filename"),
                    "split_from_knowledge_base_id": source_knowledge_base_id,
                }
            )
            if not target:
                extra_columns, extra_values = _gallery_scope_insert_columns(
                    conn, "knowledge_bases"
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_bases (
                            id, tenant_id, name, description, status, capability_scope,
                            metadata_json, created_at, updated_at"""
                        + extra_columns
                        + """
                        )
                        VALUES (
                            :id, :tenant_id, :name, :description, :status, :capability_scope,
                            :metadata_json, :created_at, CURRENT_TIMESTAMP"""
                        + extra_values
                        + """
                        )
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant_id": source["tenant_id"],
                        "name": target_name,
                        "description": f"由文档 {document.get('filename') or document['id']} 创建",
                        "status": "active",
                        "capability_scope": (
                            source.get("capability_scope")
                            if source.get("capability_scope") in {"general", "sop_specific"}
                            else "general"
                        ),
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
                        "created_at": document.get("created_at") or source.get("created_at"),
                    },
                )
            version_id = _knowledge_base_version_id(target_id, "1.0.0")
            version_exists = conn.execute(
                text("SELECT id FROM knowledge_base_versions WHERE id = :id"),
                {"id": version_id},
            ).first()
            if not version_exists:
                conn.execute(
                    text(
                        """
                        INSERT INTO knowledge_base_versions (
                            id, tenant_id, knowledge_base_id, version, name, description,
                            status, capability_scope, metadata_json, created_at, updated_at
                        )
                        VALUES (
                            :id, :tenant_id, :knowledge_base_id, '1.0.0', :name, :description,
                            'active', :capability_scope, :metadata_json,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": version_id,
                        "tenant_id": source["tenant_id"],
                        "knowledge_base_id": target_id,
                        "name": target_name,
                        "description": f"由文档 {document.get('filename') or document['id']} 创建",
                        "capability_scope": (
                            source.get("capability_scope")
                            if source.get("capability_scope") in {"general", "sop_specific"}
                            else "general"
                        ),
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
                    },
                )
            _move_document_knowledge_rows(conn, tables, str(document["id"]), target_id, version_id)


def _move_document_knowledge_rows(
    conn,
    tables: set[str],
    document_id: str,
    knowledge_base_id: str,
    version_id: str,
) -> None:
    document_scoped_tables = (
        "knowledge_buckets",
        "knowledge_chunks",
        "knowledge_concepts",
        "knowledge_discovery_suggestions",
    )
    if "knowledge_documents" in tables:
        conn.execute(
            text(
                """
                UPDATE knowledge_documents
                SET knowledge_base_id = :knowledge_base_id,
                    knowledge_base_version_id = :version_id
                WHERE id = :document_id
                """
            ),
            {
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "version_id": version_id,
            },
        )
    for table_name in document_scoped_tables:
        if table_name not in tables:
            continue
        conn.execute(
            text(
                f"""
                UPDATE {table_name}
                SET knowledge_base_id = :knowledge_base_id,
                    knowledge_base_version_id = :version_id
                WHERE document_id = :document_id
                """
            ),
            {
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "version_id": version_id,
            },
        )
    if "knowledge_ingest_jobs" not in tables:
        return
    conn.execute(
        text(
            """
            UPDATE knowledge_ingest_jobs
            SET knowledge_base_id = :knowledge_base_id,
                knowledge_base_version_id = :version_id
            WHERE document_id = :document_id
            """
        ),
        {
            "document_id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "version_id": version_id,
        },
    )


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _document_knowledge_base_id(document_id: str) -> str:
    return f"kb_doc_{document_id}"


def _document_knowledge_base_name(document) -> str:
    title = str(document.get("title") or "").strip()
    if title:
        return title
    filename = str(document.get("filename") or "").strip()
    stem = Path(filename).stem.strip()
    return stem or filename or "未命名知识库"


def _unique_migrated_knowledge_base_name(
    conn,
    tenant_id: str,
    base_name: str,
    target_id: str,
) -> str:
    normalized = base_name.strip() or "未命名知识库"
    existing_names = {
        str(row[0])
        for row in conn.execute(
            text("SELECT name FROM knowledge_bases WHERE tenant_id = :tenant_id AND id != :target_id"),
            {"tenant_id": tenant_id, "target_id": target_id},
        ).all()
        if row[0]
    }
    if normalized not in existing_names:
        return normalized
    index = 2
    while True:
        candidate = f"{normalized} {index}"
        if candidate not in existing_names:
            return candidate
        index += 1


# ---------------------------------------------------------------------------
# 资源归属化迁移（_RESOURCE_OWNERSHIP_MIGRATION_ID）
# ---------------------------------------------------------------------------
# 目标：资源自带 scope/owner_agent_id，广场不再依赖「is_overall 孪生 agent」，
# 「启用广场资源」不再是绑定表上的 status，而是引用行的存在与否。

_RESOURCE_OWNERSHIP_LEGACY_PRIVATE_SCOPE = "agent_private"
_RESOURCE_OWNERSHIP_LEGACY_GALLERY_SCOPE = "open_gallery"

_RESOURCE_OWNERSHIP_SCOPE_CHECK = (
    f"(scope = '{AGENT_SCOPE}' AND owner_agent_id IS NOT NULL) "
    f"OR (scope = '{GALLERY_SCOPE}' AND owner_agent_id IS NULL)"
)

# 表重建 DDL 是**改造完成时的历史快照**，不跟随 models.py 继续漂移。
_RESOURCE_OWNERSHIP_REBUILD: dict[str, tuple[str, tuple[str, ...]]] = {
    "skills": (
        f"""
        CREATE TABLE "__TABLE__" (
            id VARCHAR NOT NULL,
            tenant_id VARCHAR NOT NULL,
            skill_id VARCHAR NOT NULL,
            scope VARCHAR NOT NULL,
            owner_agent_id VARCHAR,
            created_by_user_id VARCHAR,
            version VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            business_domain VARCHAR,
            description VARCHAR,
            content_json JSON NOT NULL,
            status VARCHAR NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT ck_skills_scope_owner CHECK ({_RESOURCE_OWNERSHIP_SCOPE_CHECK})
        )
        """,
        (
            "id",
            "tenant_id",
            "skill_id",
            "scope",
            "owner_agent_id",
            "created_by_user_id",
            "version",
            "name",
            "business_domain",
            "description",
            "content_json",
            "status",
            "created_at",
            "updated_at",
        ),
    ),
    "general_skills": (
        f"""
        CREATE TABLE "__TABLE__" (
            id VARCHAR NOT NULL,
            tenant_id VARCHAR NOT NULL,
            slug VARCHAR NOT NULL,
            scope VARCHAR NOT NULL,
            owner_agent_id VARCHAR,
            created_by_user_id VARCHAR,
            name VARCHAR NOT NULL,
            description VARCHAR,
            homepage VARCHAR,
            skill_markdown VARCHAR NOT NULL,
            skill_files_json JSON NOT NULL,
            metadata_json JSON NOT NULL,
            status VARCHAR NOT NULL,
            capability_scope VARCHAR NOT NULL,
            permissions_json JSON NOT NULL,
            runtime_config_json JSON NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT ck_general_skills_scope_owner CHECK ({_RESOURCE_OWNERSHIP_SCOPE_CHECK})
        )
        """,
        (
            "id",
            "tenant_id",
            "slug",
            "scope",
            "owner_agent_id",
            "created_by_user_id",
            "name",
            "description",
            "homepage",
            "skill_markdown",
            "skill_files_json",
            "metadata_json",
            "status",
            "capability_scope",
            "permissions_json",
            "runtime_config_json",
            "created_at",
            "updated_at",
        ),
    ),
    "knowledge_bases": (
        f"""
        CREATE TABLE "__TABLE__" (
            id VARCHAR NOT NULL,
            tenant_id VARCHAR NOT NULL,
            scope VARCHAR NOT NULL,
            owner_agent_id VARCHAR,
            created_by_user_id VARCHAR,
            name VARCHAR NOT NULL,
            description VARCHAR,
            status VARCHAR NOT NULL,
            capability_scope VARCHAR NOT NULL,
            metadata_json JSON NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT ck_knowledge_bases_scope_owner CHECK ({_RESOURCE_OWNERSHIP_SCOPE_CHECK})
        )
        """,
        (
            "id",
            "tenant_id",
            "scope",
            "owner_agent_id",
            "created_by_user_id",
            "name",
            "description",
            "status",
            "capability_scope",
            "metadata_json",
            "created_at",
            "updated_at",
        ),
    ),
    "tools": (
        f"""
        CREATE TABLE "__TABLE__" (
            id VARCHAR NOT NULL,
            tenant_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            scope VARCHAR NOT NULL,
            owner_agent_id VARCHAR,
            created_by_user_id VARCHAR,
            display_name VARCHAR,
            description VARCHAR,
            bucket VARCHAR NOT NULL,
            tool_type VARCHAR NOT NULL,
            method VARCHAR NOT NULL,
            url VARCHAR NOT NULL,
            headers_json JSON NOT NULL,
            auth_json JSON NOT NULL,
            config_json JSON NOT NULL,
            input_schema JSON NOT NULL,
            output_schema JSON NOT NULL,
            allowed_skills_json JSON NOT NULL,
            mcp_server_id VARCHAR,
            capability_scope VARCHAR NOT NULL,
            capability_scope_inherited BOOLEAN NOT NULL,
            enabled BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT ck_tools_scope_owner CHECK ({_RESOURCE_OWNERSHIP_SCOPE_CHECK})
        )
        """,
        (
            "id",
            "tenant_id",
            "name",
            "scope",
            "owner_agent_id",
            "created_by_user_id",
            "display_name",
            "description",
            "bucket",
            "tool_type",
            "method",
            "url",
            "headers_json",
            "auth_json",
            "config_json",
            "input_schema",
            "output_schema",
            "allowed_skills_json",
            "mcp_server_id",
            "capability_scope",
            "capability_scope_inherited",
            "enabled",
            "created_at",
            "updated_at",
        ),
    ),
    "agent_profiles": (
        """
        CREATE TABLE "__TABLE__" (
            id VARCHAR NOT NULL,
            tenant_id VARCHAR NOT NULL,
            owner_user_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            description VARCHAR,
            persona_prompt VARCHAR,
            is_overall BOOLEAN NOT NULL,
            is_published BOOLEAN NOT NULL,
            published_at DATETIME,
            published_by VARCHAR,
            status VARCHAR NOT NULL,
            harness_max_actions INTEGER NOT NULL,
            metadata_json JSON NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_agent_profile_tenant_owner_name UNIQUE (tenant_id, owner_user_id, name)
        )
        """,
        (
            "id",
            "tenant_id",
            "owner_user_id",
            "name",
            "description",
            "persona_prompt",
            "is_overall",
            "is_published",
            "published_at",
            "published_by",
            "status",
            "harness_max_actions",
            "metadata_json",
            "created_at",
            "updated_at",
        ),
    ),
    "agent_resource_references": (
        """
        CREATE TABLE "__TABLE__" (
            id VARCHAR NOT NULL,
            tenant_id VARCHAR NOT NULL,
            agent_id VARCHAR NOT NULL,
            resource_type VARCHAR NOT NULL,
            resource_id VARCHAR NOT NULL,
            created_by_user_id VARCHAR,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_agent_resource_reference
                UNIQUE (tenant_id, agent_id, resource_type, resource_id)
        )
        """,
        (
            "id",
            "tenant_id",
            "agent_id",
            "resource_type",
            "resource_id",
            "created_by_user_id",
            "created_at",
            "updated_at",
        ),
    ),
}


def _table_columns(conn, table_name: str) -> list[str]:
    """现读表结构：表重建后 inspector 缓存即失效，必须直接问 SQLite。"""
    rows = conn.execute(text(f'PRAGMA table_info("{table_name}")')).all()
    return [str(row[1]) for row in rows]


def _table_column_info(conn, table_name: str) -> dict[str, bool]:
    """列名 → 是否 NOT NULL。判断外键「置空还是删行」要看列自己的可空性。"""
    rows = conn.execute(text(f'PRAGMA table_info("{table_name}")')).all()
    return {str(row[1]): bool(row[3]) for row in rows}


def _gallery_scope_insert_columns(conn, table_name: str) -> tuple[str, str]:
    """归属化改造后，四张资源表多了 `scope` / `owner_agent_id` 两个 NOT NULL/索引列。

    迁移里的「补数据」INSERT 是给老库兜底用的；老库若已重建出这两列，就必须显式写入
    广场归属，否则会撞 `scope` 的 NOT NULL 约束。列还不存在时返回空串，SQL 保持原样。
    """
    existing = set(_table_columns(conn, table_name))
    names: list[str] = []
    values: list[str] = []
    if "scope" in existing:
        names.append("scope")
        values.append("'gallery'")
    if "owner_agent_id" in existing:
        names.append("owner_agent_id")
        values.append("NULL")
    if not names:
        return "", ""
    return ", " + ", ".join(names), ", " + ", ".join(values)


def _add_columns_if_missing(conn, table_name: str, additions: dict[str, str]) -> None:
    existing = set(_table_columns(conn, table_name))
    for column_name, ddl in additions.items():
        if column_name not in existing:
            conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN {column_name} {ddl}'))


def _rebuild_table(
    conn,
    table_name: str,
    create_sql: str,
    columns: tuple[str, ...],
    *,
    source_table: str | None = None,
) -> None:
    """SQLite 表重建核心四步：建新表 → 搬运交集列 → 删旧表 → 改名。

    重建是 SQLite 下**唯一**能改 CHECK 约束与表级唯一约束的手段（`ALTER TABLE`
    既不能改既有约束，也不能加带 CHECK 的列）。调用方必须先把新列 `ADD COLUMN`
    出来并完成回填 —— 否则搬运时 NOT NULL / CHECK 会直接拒绝旧行。
    """
    source = source_table or table_name
    temp_name = f"{table_name}__rebuild"
    existing_columns = set(_table_columns(conn, source))
    conn.execute(text(f'DROP TABLE IF EXISTS "{temp_name}"'))
    conn.execute(text(create_sql.replace("__TABLE__", temp_name, 1)))
    copy_columns = [name for name in columns if name in existing_columns]
    column_list = ", ".join(f'"{name}"' for name in copy_columns)
    conn.execute(
        text(
            f'INSERT INTO "{temp_name}" ({column_list}) '
            f'SELECT {column_list} FROM "{source}"'
        )
    )
    conn.execute(text(f'DROP TABLE "{source}"'))
    conn.execute(text(f'ALTER TABLE "{temp_name}" RENAME TO "{table_name}"'))


def _create_model_indexes(conn, table_name: str) -> None:
    """索引集从 models.py 元数据派生 —— 索引是约束的机械派生物，不该在迁移里手抄一份。"""
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from sqlalchemy.schema import CreateIndex

    import app.db.models  # noqa: F401  确保元数据已注册

    dialect = sqlite_dialect.dialect()
    for index in SQLModel.metadata.tables[table_name].indexes:
        conn.execute(text(str(CreateIndex(index).compile(dialect=dialect))))


def _resource_ownership_migration_needed(conn, tables: set[str]) -> bool:
    """新库不能跑重建：`create_all` 已建出目标结构，重建只会拿空表覆盖自己。"""
    if "agent_resource_bindings" in tables:
        return True
    if any(name in tables for name in _RESOURCE_OWNERSHIP_LEGACY_BRANCH_TABLES):
        return True
    if "agent_profiles" in tables and "is_published" not in _table_columns(conn, "agent_profiles"):
        return True
    return False


def _migration_owner_user_id(conn, tables: set[str], tenant_id: str) -> str:
    """老数据缺归属时落到该租户的管理员账号，保证 owner_user_id 非空（新结构是 NOT NULL）。"""
    if "users" in tables:
        row = conn.execute(
            text(
                "SELECT id FROM users WHERE tenant_id = :tenant_id "
                "ORDER BY (role = 'admin') DESC, created_at ASC LIMIT 1"
            ),
            {"tenant_id": tenant_id},
        ).first()
        if row and row[0]:
            return str(row[0])
    return "admin"


def _backfill_agent_ownership(conn, tables: set[str]) -> None:
    """员工归属：metadata_json.owner_user_id 进列；published_to_gallery 进 is_published。"""
    if "agent_profiles" not in tables:
        return
    _add_columns_if_missing(
        conn,
        "agent_profiles",
        {
            "owner_user_id": "VARCHAR",
            "is_published": "BOOLEAN NOT NULL DEFAULT 0",
            "published_at": "DATETIME",
            "published_by": "VARCHAR",
        },
    )
    fallback_owners: dict[str, str] = {}
    rows = conn.execute(
        text("SELECT id, tenant_id, metadata_json FROM agent_profiles")
    ).mappings().all()
    for row in rows:
        tenant_id = str(row.get("tenant_id") or "")
        metadata = _json_object(row.get("metadata_json"))
        owner_user_id = str(
            metadata.get("owner_user_id") or metadata.get("created_by_user_id") or ""
        ).strip()
        if not owner_user_id:
            if tenant_id not in fallback_owners:
                fallback_owners[tenant_id] = _migration_owner_user_id(conn, tables, tenant_id)
            owner_user_id = fallback_owners[tenant_id]
        published = metadata.get("published_to_gallery") in (True, 1, "1", "true", "True")
        conn.execute(
            text(
                """
                UPDATE agent_profiles
                SET owner_user_id = :owner_user_id,
                    is_published = :is_published,
                    published_at = CASE
                        WHEN :is_published = 1 THEN COALESCE(published_at, updated_at)
                        ELSE published_at
                    END,
                    published_by = CASE
                        WHEN :is_published = 1 THEN COALESCE(published_by, :owner_user_id)
                        ELSE published_by
                    END
                WHERE id = :id
                """
            ),
            {
                "owner_user_id": owner_user_id,
                "is_published": 1 if published else 0,
                "id": row["id"],
            },
        )


def _legacy_branch_owners(conn, tables: set[str]) -> dict[tuple[str, str], str]:
    """分支表记录「某 agent 持有私有副本」这一事实：{("skill", id): agent_id}。"""
    owners: dict[tuple[str, str], str] = {}
    branch_sources = (
        ("agent_skill_branches", "skill", "skill_id"),
        ("agent_knowledge_branches", "knowledge_base", "knowledge_base_id"),
    )
    for table_name, resource_type, column_name in branch_sources:
        if table_name not in tables:
            continue
        rows = conn.execute(
            text(f'SELECT agent_id, "{column_name}" AS resource_id FROM "{table_name}"')
        ).mappings().all()
        for row in rows:
            resource_id = str(row.get("resource_id") or "")
            agent_id = str(row.get("agent_id") or "")
            if resource_id and agent_id:
                owners.setdefault((resource_type, resource_id), agent_id)
    return owners


def _legacy_binding_facts(conn, tables: set[str]) -> tuple[dict[tuple[str, str], str], set[tuple[str, str]]]:
    """老绑定表蕴含两件事：绑到整体智能体 = 广场资源；绑到普通员工 = 私有归属。"""
    if "agent_resource_bindings" not in tables or "agent_profiles" not in tables:
        return {}, set()
    overall_agents = {
        str(row[0])
        for row in conn.execute(
            text("SELECT id FROM agent_profiles WHERE is_overall = 1")
        ).all()
        if row[0]
    }
    private_owners: dict[tuple[str, str], str] = {}
    gallery_keys: set[tuple[str, str]] = set()
    rows = conn.execute(
        text("SELECT agent_id, resource_type, resource_id, status FROM agent_resource_bindings")
    ).mappings().all()
    for row in rows:
        if str(row.get("status") or "") == "deleted":
            continue
        resource_type = str(row.get("resource_type") or "")
        resource_id = str(row.get("resource_id") or "")
        if not resource_type or not resource_id:
            continue
        key = (resource_type, resource_id)
        agent_id = str(row.get("agent_id") or "")
        if agent_id in overall_agents:
            gallery_keys.add(key)
        elif agent_id:
            private_owners.setdefault(key, agent_id)
    return private_owners, gallery_keys


def _legacy_owner_agent_id(
    metadata: dict[str, object],
    key: tuple[str, str],
    branch_owners: dict[tuple[str, str], str],
    binding_owners: dict[tuple[str, str], str],
    gallery_keys: set[tuple[str, str]],
) -> str | None:
    """归属判定优先级：metadata 最显式 → 分支表 → 绑定表 → 兜底归广场。

    兜底选广场而非私有：广场是管理员可写，无主资源留在广场最不容易变成孤儿。
    """
    legacy_scope = str(metadata.get("scope") or metadata.get("visibility") or "").strip()
    if legacy_scope == _RESOURCE_OWNERSHIP_LEGACY_PRIVATE_SCOPE:
        owner_agent_id = str(metadata.get("owner_agent_id") or "").strip()
        if owner_agent_id:
            return owner_agent_id
    elif legacy_scope == _RESOURCE_OWNERSHIP_LEGACY_GALLERY_SCOPE:
        return None
    if key in gallery_keys:
        return None
    return branch_owners.get(key) or binding_owners.get(key)


def _backfill_resource_ownership(conn, tables: set[str]) -> None:
    """四张资源表：把藏在 metadata_json / 分支表 / 绑定表里的归属落到 scope + owner_agent_id。"""
    branch_owners = _legacy_branch_owners(conn, tables)
    binding_owners, gallery_keys = _legacy_binding_facts(conn, tables)
    for resource_type, table_name, _, metadata_column in _RESOURCE_OWNERSHIP_SCOPED_TABLES:
        if table_name not in tables:
            continue
        _add_columns_if_missing(
            conn,
            table_name,
            {
                "scope": f"VARCHAR NOT NULL DEFAULT '{AGENT_SCOPE}'",
                "owner_agent_id": "VARCHAR",
                "created_by_user_id": "VARCHAR",
            },
        )
        columns = ["id"] + ([metadata_column] if metadata_column else [])
        rows = conn.execute(
            text(f'SELECT {", ".join(columns)} FROM "{table_name}"')
        ).mappings().all()
        for row in rows:
            metadata = _json_object(row.get(metadata_column)) if metadata_column else {}
            key = (resource_type, str(row["id"]))
            owner_agent_id = _legacy_owner_agent_id(
                metadata, key, branch_owners, binding_owners, gallery_keys
            )
            created_by_user_id = str(
                metadata.get("owner_user_id") or metadata.get("created_by_user_id") or ""
            ).strip()
            conn.execute(
                text(
                    f'UPDATE "{table_name}" '
                    "SET scope = :scope, owner_agent_id = :owner_agent_id, "
                    "created_by_user_id = :created_by_user_id "
                    "WHERE id = :id"
                ),
                {
                    "scope": AGENT_SCOPE if owner_agent_id else GALLERY_SCOPE,
                    "owner_agent_id": owner_agent_id,
                    "created_by_user_id": created_by_user_id or None,
                    "id": row["id"],
                },
            )


def _dedupe_owned_keys(
    conn,
    table_name: str,
    key_column: str,
    *,
    owner_column: str,
    scope: str | None,
) -> None:
    """同 (tenant_id, owner, 业务键) 重名才消歧 —— 保留最早，其余加序号后缀。"""
    scope_filter = " AND scope = :scope" if scope else ""
    parameters: dict[str, object] = {"scope": scope} if scope else {}
    groups = conn.execute(
        text(
            f'SELECT tenant_id, {owner_column} AS owner_id, "{key_column}" AS key_value '
            f'FROM "{table_name}" WHERE {owner_column} IS NOT NULL{scope_filter} '
            f'GROUP BY tenant_id, {owner_column}, "{key_column}" HAVING COUNT(*) > 1'
        ),
        parameters,
    ).mappings().all()
    for group in groups:
        rows = conn.execute(
            text(
                f'SELECT id, "{key_column}" AS key_value FROM "{table_name}" '
                f"WHERE tenant_id = :tenant_id AND {owner_column} = :owner_id "
                f'AND "{key_column}" = :key_value{scope_filter} '
                "ORDER BY created_at ASC, id ASC"
            ),
            {
                "tenant_id": group["tenant_id"],
                "owner_id": group["owner_id"],
                "key_value": group["key_value"],
                **parameters,
            },
        ).mappings().all()
        for position, row in enumerate(rows[1:], start=2):
            conn.execute(
                text(f'UPDATE "{table_name}" SET "{key_column}" = :key_value WHERE id = :id'),
                {"key_value": f'{row["key_value"]}-{position}', "id": row["id"]},
            )


def _resolve_ownership_name_collisions(conn, tables: set[str]) -> None:
    """老约束是租户级（比新约束更严），同 owner 重名理论上不存在；留着兜手工数据。"""
    for _, table_name, key_column, _ in _RESOURCE_OWNERSHIP_SCOPED_TABLES:
        if table_name not in tables:
            continue
        _dedupe_owned_keys(
            conn, table_name, key_column, owner_column="owner_agent_id", scope=AGENT_SCOPE
        )
    if "agent_profiles" in tables:
        _dedupe_owned_keys(
            conn, "agent_profiles", "name", owner_column="owner_user_id", scope=None
        )


def _rebuild_ownership_tables(conn, tables: set[str]) -> None:
    """装 CHECK 与新的唯一约束 —— 只能靠表重建，且必须在回填完成之后。"""
    for table_name in (
        "skills",
        "general_skills",
        "knowledge_bases",
        "tools",
        "agent_profiles",
    ):
        if table_name not in tables:
            continue
        create_sql, columns = _RESOURCE_OWNERSHIP_REBUILD[table_name]
        _rebuild_table(conn, table_name, create_sql, columns)
        _create_model_indexes(conn, table_name)


def _migrate_agent_resource_references(conn, tables: set[str]) -> None:
    """绑定表 → 纯引用表：只留「指向广场资源、且当时处于启用状态、且不是整体智能体自己」的行。

    私有资源「归属即生效」，引用行对它是噪音；反向保留还会把别人的私有资源暴露给引用者。
    status='inactive' 表示迁移前就没启用；新模型用「行是否存在」表达开关，这类行直接丢弃。
    """
    create_sql, _ = _RESOURCE_OWNERSHIP_REBUILD["agent_resource_references"]
    if "agent_resource_bindings" in tables:
        conn.execute(text('DROP TABLE IF EXISTS "agent_resource_references"'))
        conn.execute(text(create_sql.replace("__TABLE__", "agent_resource_references", 1)))
        conn.execute(
            text(
                """
                INSERT INTO agent_resource_references (
                    id, tenant_id, agent_id, resource_type, resource_id, created_at, updated_at
                )
                SELECT b.id, b.tenant_id, b.agent_id, b.resource_type, b.resource_id,
                       b.created_at, b.updated_at
                FROM agent_resource_bindings AS b
                WHERE b.status != 'deleted'
                  AND EXISTS (
                      SELECT 1 FROM agent_resource_bindings AS owner_binding
                      JOIN agent_profiles AS overall_agent
                        ON overall_agent.id = owner_binding.agent_id
                       AND overall_agent.is_overall = 1
                      WHERE owner_binding.resource_type = b.resource_type
                        AND owner_binding.resource_id = b.resource_id
                        AND owner_binding.status != 'deleted'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_profiles AS binding_agent
                      WHERE binding_agent.id = b.agent_id
                        AND binding_agent.is_overall = 1
                  )
                """
            )
        )
        conn.execute(text('DROP TABLE "agent_resource_bindings"'))
    _create_model_indexes(conn, "agent_resource_references")


def _drop_legacy_branch_tables(conn) -> None:
    for table_name in _RESOURCE_OWNERSHIP_LEGACY_BRANCH_TABLES:
        conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))


def _strip_ownership_metadata(conn, tables: set[str]) -> None:
    """清掉已由列承载的状态键，避免「列 + metadata」两处真相。

    只清**状态**键：`owner_user_id` / `owner_username` / `owner_display_name` 是创建者
    署名（UI 用它做同名消歧），列里没有对应字段，必须留下。
    """
    targets = [
        (table_name, _RESOURCE_OWNERSHIP_STATE_METADATA_KEYS)
        for _, table_name, _, metadata_column in _RESOURCE_OWNERSHIP_SCOPED_TABLES
        if metadata_column
    ]
    targets.append(("agent_profiles", _RESOURCE_OWNERSHIP_AGENT_STATE_METADATA_KEYS))
    for table_name, keys in targets:
        if table_name not in tables:
            continue
        rows = conn.execute(
            text(f'SELECT id, metadata_json FROM "{table_name}"')
        ).mappings().all()
        for row in rows:
            metadata = _json_object(row.get("metadata_json"))
            if not any(key in metadata for key in keys):
                continue
            for key in keys:
                metadata.pop(key, None)
            conn.execute(
                text(f'UPDATE "{table_name}" SET metadata_json = :metadata_json WHERE id = :id'),
                {
                    "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    "id": row["id"],
                },
            )


def _migration_already_applied(conn, migration_id: str) -> bool:
    """迁移登记表是「跑过没有」的唯一判据；建表与查询都在这里收口。"""
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
        {"id": migration_id},
    ).first()
    return bool(applied)


def _record_migration(conn, migration_id: str) -> None:
    conn.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": migration_id},
    )


def _migrate_resource_ownership(conn, inspector, tables: set[str]) -> None:
    """把「广场 = 一条 is_overall 员工记录」换成「资源自带 scope/owner」。

    顺序本身即正确性，不可调换：
      1. ADD COLUMN 出新列（不带 CHECK —— SQLite 不允许）
      2. 回填归属
      3. 同归属重名消歧
      4. 表重建，装上 CHECK 与新的唯一约束
      5. 绑定表重建为纯引用表
      6. 删三张分支表
      7. 清掉失效的 metadata 状态键

    本迁移**必须**保留 `is_overall` 列：第 5 步要靠它区分「老绑定表里哪些行是广场
    绑定」。列本身的删除交给随后的一次性迁移 `_migrate_drop_overall_agent`。
    """
    if not _resource_ownership_migration_needed(conn, tables):
        return
    if _migration_already_applied(conn, _RESOURCE_OWNERSHIP_MIGRATION_ID):
        return

    _backfill_agent_ownership(conn, tables)
    _backfill_resource_ownership(conn, tables)
    _resolve_ownership_name_collisions(conn, tables)
    _rebuild_ownership_tables(conn, tables)
    _migrate_agent_resource_references(conn, tables)
    _drop_legacy_branch_tables(conn)
    _strip_ownership_metadata(conn, tables)
    _record_migration(conn, _RESOURCE_OWNERSHIP_MIGRATION_ID)


# 「整体智能体」删除后的 agent_profiles 快照。`_rebuild_table` 只搬运新旧列的交集，
# 所以列元组里没有 `is_overall` 就等于把它砍掉。
_AGENT_PROFILES_TABLE_DDL = (
    """
    CREATE TABLE "__TABLE__" (
        id VARCHAR NOT NULL,
        tenant_id VARCHAR NOT NULL,
        owner_user_id VARCHAR NOT NULL,
        name VARCHAR NOT NULL,
        description VARCHAR,
        persona_prompt VARCHAR,
        is_published BOOLEAN NOT NULL,
        published_at DATETIME,
        published_by VARCHAR,
        status VARCHAR NOT NULL,
        harness_max_actions INTEGER NOT NULL,
        metadata_json JSON NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_agent_profile_tenant_owner_name UNIQUE (tenant_id, owner_user_id, name)
    )
    """,
    (
        "id",
        "tenant_id",
        "owner_user_id",
        "name",
        "description",
        "persona_prompt",
        "is_published",
        "published_at",
        "published_by",
        "status",
        "harness_max_actions",
        "metadata_json",
        "created_at",
        "updated_at",
    ),
)


def _purge_agent_dependents(conn, tables: set[str], agent_ids: list[str]) -> None:
    """清掉指向这批员工的其它表数据：可空外键置空，非空外键删行。

    判定依据是**列自己的可空性**，而不是手抄一张「哪些表指向员工」的清单：
    `agent_id` 可空说明这行不依赖该员工，断开链接即可；非空说明这行的存在意义
    就是它，只能删。新增表也不会漏。
    """
    if not agent_ids:
        return
    placeholders = ", ".join(f":agent_{index}" for index in range(len(agent_ids)))
    params = {f"agent_{index}": value for index, value in enumerate(agent_ids)}
    for table_name in sorted(tables):
        if table_name == "agent_profiles":
            continue
        columns = _table_column_info(conn, table_name)
        if "agent_id" not in columns:
            continue
        if columns["agent_id"]:
            sql = f'DELETE FROM "{table_name}" WHERE agent_id IN ({placeholders})'
        else:
            sql = f'UPDATE "{table_name}" SET agent_id = NULL WHERE agent_id IN ({placeholders})'
        conn.execute(text(sql), params)


def _migrate_drop_overall_agent(conn, tables: set[str]) -> None:
    """删掉「整体智能体」孪生记录，以及它赖以存在的 `is_overall` 列。

    广场资源早已由资源表自己的 `scope='gallery'` 表达，那条孪生记录唯一剩下的作用
    是把「广场视角」伪装成一个员工 —— 于是它同时占着一个员工名、又要被每个权限判定
    特判。清掉它之后，`agent_profiles` 里每一行都是某个人的真实员工，系统里不再有
    「不是员工的员工」。

    先按外键清理依赖，再删行，最后重建表去掉列。列不存在时整个迁移是空操作 ——
    新库由 `create_all` 直接从模型建表，本就没有这一列。
    """
    if "agent_profiles" not in tables:
        return
    if "is_overall" not in _table_columns(conn, "agent_profiles"):
        return
    if _migration_already_applied(conn, _DROP_OVERALL_AGENT_MIGRATION_ID):
        return

    overall_ids = [
        str(row[0])
        for row in conn.execute(
            text("SELECT id FROM agent_profiles WHERE is_overall = 1")
        ).all()
        if row[0]
    ]
    _purge_agent_dependents(conn, tables, overall_ids)
    for agent_id in overall_ids:
        conn.execute(text("DELETE FROM agent_profiles WHERE id = :id"), {"id": agent_id})

    create_sql, columns = _AGENT_PROFILES_TABLE_DDL
    _rebuild_table(conn, "agent_profiles", create_sql, columns)
    _create_model_indexes(conn, "agent_profiles")
    _record_migration(conn, _DROP_OVERALL_AGENT_MIGRATION_ID)


def _purge_legacy_agent_model_bindings(conn, tables: set[str]) -> None:
    """员工级模型选择已退役。启动时清掉历史行，避免老库/老客户端把员工钉在过期模型上。"""
    if "agent_model_bindings" in tables:
        conn.execute(text("DELETE FROM agent_model_bindings"))


def _seed_default_agents(conn, tables: set[str]) -> None:
    if "agent_profiles" not in tables:
        return
    for tenant_id in _tenant_ids(conn, tables):
        _archive_default_agent(conn, tenant_id)
        if "agent_resource_references" in tables:
            _seed_default_agent_bindings(conn, tenant_id)


def _seed_default_agent_bindings(conn, tenant_id: str) -> None:
    """默认员工引用本租户的全部广场资源（私有资源「归属即生效」，不需要引用行）。"""
    default_agent = _default_agent_id(tenant_id)
    active_default = conn.execute(
        text(
            """
            SELECT id FROM agent_profiles
            WHERE id = :id AND tenant_id = :tenant_id AND status != 'archived'
            """
        ),
        {"id": default_agent, "tenant_id": tenant_id},
    ).first()
    if not active_default:
        return
    resource_queries = (
        ("skill", f"SELECT id FROM skills WHERE tenant_id = :tenant_id AND status != 'deleted' AND scope = '{GALLERY_SCOPE}'"),
        ("general_skill", f"SELECT id FROM general_skills WHERE tenant_id = :tenant_id AND status != 'deleted' AND scope = '{GALLERY_SCOPE}'"),
        ("knowledge_base", f"SELECT id FROM knowledge_bases WHERE tenant_id = :tenant_id AND status != 'deleted' AND scope = '{GALLERY_SCOPE}'"),
    )
    for resource_type, sql in resource_queries:
        rows = conn.execute(text(sql), {"tenant_id": tenant_id}).mappings().all()
        for row in rows:
            resource_id = str(row.get("id") or "")
            if not resource_id:
                continue
            existing = conn.execute(
                text(
                    """
                    SELECT id FROM agent_resource_references
                    WHERE tenant_id = :tenant_id AND agent_id = :agent_id
                      AND resource_type = :resource_type AND resource_id = :resource_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "agent_id": default_agent,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            ).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO agent_resource_references (
                        id, tenant_id, agent_id, resource_type, resource_id,
                        created_at, updated_at
                    )
                    VALUES (
                        :id, :tenant_id, :agent_id, :resource_type, :resource_id,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {
                    "id": _agent_resource_binding_id(tenant_id, default_agent, resource_type, resource_id),
                    "tenant_id": tenant_id,
                    "agent_id": default_agent,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            )


def _archive_default_agent(conn, tenant_id: str) -> None:
    default_agent = _default_agent_id(tenant_id)
    row = conn.execute(
        text(
            """
            SELECT metadata_json FROM agent_profiles
            WHERE id = :id AND tenant_id = :tenant_id
            """
        ),
        {"id": default_agent, "tenant_id": tenant_id},
    ).first()
    if not row:
        return
    try:
        metadata = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if metadata and not (
        metadata.get("is_default_employee") is True
        or metadata.get("created_by") == "admin"
        or metadata.get("owner_user_id") == "admin"
    ):
        return
    metadata.update(
        {
            "is_default_employee": True,
            "hidden_from_staffdeck": True,
            "archived_by_seed": True,
            "owner_user_id": "admin",
            "owner_username": "admin",
            "owner_display_name": "Administrator",
            "created_by_user_id": "admin",
            "created_by_username": "admin",
            "created_by": "admin",
            "created_by_display_name": "Administrator",
            "creator_name": "admin",
        }
    )
    conn.execute(
        text(
            """
            UPDATE agent_profiles
            SET status = 'archived',
                metadata_json = :metadata_json,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND tenant_id = :tenant_id
            """
        ),
        {
            "id": default_agent,
            "tenant_id": tenant_id,
            "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        },
    )


def _normalize_canonical_ids(
    conn,
    *,
    table: str,
    select_columns: tuple[str, ...],
    key_columns: tuple[str, ...],
    id_factory: Callable[[dict[str, object]], str],
) -> None:
    columns_sql = ", ".join(select_columns)
    rows = conn.execute(text(f"SELECT {columns_sql} FROM {table}")).mappings().all()
    kept_keys: set[tuple[object, ...]] = set()
    for row in rows:
        row_dict = dict(row)
        row_id = str(row_dict["id"])
        key = tuple(row_dict[column] for column in key_columns)
        target_id = id_factory(row_dict)
        if key in kept_keys:
            conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
            continue
        kept_keys.add(key)
        if row_id == target_id:
            continue
        target_exists = conn.execute(text(f"SELECT id FROM {table} WHERE id = :id"), {"id": target_id}).first()
        if target_exists:
            conn.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
            continue
        conn.execute(text(f"UPDATE {table} SET id = :target_id WHERE id = :id"), {"target_id": target_id, "id": row_id})


def _tenant_ids(conn, tables: set[str]) -> list[str]:
    ids: set[str] = set()
    if "tenants" in tables:
        ids.update(str(row[0]) for row in conn.execute(text("SELECT id FROM tenants")).all() if row[0])
    for table_name in ("skills", "general_skills", "knowledge_documents", "sessions"):
        if table_name not in tables:
            continue
        ids.update(str(row[0]) for row in conn.execute(text(f"SELECT DISTINCT tenant_id FROM {table_name}")).all() if row[0])
    return sorted(ids)


def _default_knowledge_base_id(tenant_id: str) -> str:
    return f"kb_{tenant_id}_default"


def _default_agent_id(tenant_id: str) -> str:
    return f"agent_{tenant_id}_default"


def _knowledge_base_version_id(knowledge_base_id: str, version: str) -> str:
    return f"kbver_{knowledge_base_id}_{version.replace('.', '_').replace('-', '_')}"


def _agent_resource_binding_id(tenant_id: str, agent_id: str, resource_type: str, resource_id: str) -> str:
    key = f"{tenant_id}:{agent_id}:{resource_type}:{resource_id}"
    return f"agentres_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
