from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import text
from sqlmodel import Session, SQLModel, select

from app.agents.branching import ensure_open_gallery_binding
from app.capability_scope import normalize_capability_scope
from app.db.models import (
    GALLERY_SCOPE,
    AgentProfile,
    AgentResourceReference,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Skill,
    SkillVersion,
    Tool,
    User,
    utc_now,
)

TENANT_ID = "tenant_demo"
ADMIN_USER_ID = "admin"
ADMIN_USERNAME = "admin"
ADMIN_DISPLAY_NAME = "Administrator"
SEED_SOURCE = "staffdeck_admin_gallery_seed"
# 一次性初始化标记：写在 app_data_migrations 里，存在即不再补齐预置资源。
GALLERY_SEED_MARKER_ID = "20260922_staffdeck_gallery_initialized"
_APP_DATA_MIGRATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS app_data_migrations ("
    "id VARCHAR PRIMARY KEY, "
    "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
)
FIXTURE_PATH = Path(__file__).resolve().parent / "seed_fixtures" / "staffdeck_admin_gallery_seed.json"
EXPANDED_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "seed_fixtures" / "staffdeck_expanded_gallery_seed.json"
)

SELECTED_AGENT_NAMES = {
    "IT",
    "人事",
    "法务",
    "行政",
    "财务",
    "销售",
    "市场",
    "采购",
    "项目管理",
    "数据分析",
}
USER_EDITABLE_AGENT_METADATA_KEYS = {
    "avatar_image",
    "avatar_kind",
    "avatar_preset",
    "avatar_text",
    "avatar_tone",
}
# 归属化改造前的「状态类」metadata 键。新结构里它们分别由
# `resources.scope` / `resources.owner_agent_id` / `agent_profiles.is_published`
# 三处列承载，metadata 里再留一份就是两处真相。
_LEGACY_OWNERSHIP_METADATA_KEYS = (
    "scope",
    "visibility",
    "owner_agent_id",
    "published_to_gallery",
    "created_from_agent",
    "created_from_upload",
)

JsonDict = dict[str, Any]
ModelT = TypeVar("ModelT", bound=SQLModel)


def seed_staffdeck_admin_gallery(session: Session) -> None:
    """Seed the curated StaffDeck gallery package as admin-owned resources.

    预置资源只做一次性初始化：首次播种后写入 GALLERY_SEED_MARKER_ID 标记，之后启动
    直接跳过。否则管理员删掉的预置员工会在下次启动时按固定 ID 重新补齐（重启即复活）。
    """

    if _gallery_seed_marker_present(session):
        return
    if _gallery_seed_rows_present(session):
        # 存量库（标记机制上线前的安装）：预置数据已在，只补标记、不重播，
        # 以免把管理员此前删掉的员工又按固定 ID 建回来。
        _mark_gallery_seed_applied(session)
        return
    data = _load_seed_fixtures((FIXTURE_PATH, EXPANDED_FIXTURE_PATH))
    if not data:
        return
    id_maps: dict[str, dict[str, str]] = {
        "agent": {},
        "skill": {},
        "skill_business": {},
        "general_skill": {},
        "tool": {},
        "knowledge_base": {},
        "knowledge_base_version": {},
        "knowledge_document": {},
        "knowledge_bucket": {},
    }

    agents = [
        row for row in data.get("agent_profiles", []) if row.get("name") in SELECTED_AGENT_NAMES
    ]
    selected_agent_ids = {str(row.get("id") or "") for row in agents}
    # 引用行没有 status —— 行存在即为引用（取消引用就是删行）。
    active_bindings = [
        row
        for row in data.get("agent_resource_references", [])
        if str(row.get("agent_id") or "") in selected_agent_ids
    ]
    resource_ids = _resource_ids_by_type(active_bindings)

    _seed_agents(session, agents, id_maps)
    _seed_skills(session, data.get("skills", []), data.get("skill_versions", []), resource_ids, id_maps)
    _seed_general_skills(session, data.get("general_skills", []), resource_ids, id_maps)
    _seed_tools(session, data.get("tools", []), resource_ids, id_maps)
    _seed_knowledge(session, data, resource_ids, id_maps)
    session.flush()
    _seed_agent_resource_references(session, active_bindings, id_maps)
    session.flush()
    _publish_gallery_resources(session, id_maps)
    _sync_seed_agents_to_current_admin(session, id_maps)
    _mark_gallery_seed_applied(session)


def _gallery_seed_marker_present(session: Session) -> bool:
    session.execute(text(_APP_DATA_MIGRATIONS_DDL))
    row = session.execute(
        text("SELECT 1 FROM app_data_migrations WHERE id = :id"),
        {"id": GALLERY_SEED_MARKER_ID},
    ).first()
    return row is not None


def _mark_gallery_seed_applied(session: Session) -> None:
    if _gallery_seed_marker_present(session):
        return
    session.execute(
        text("INSERT INTO app_data_migrations (id) VALUES (:id)"),
        {"id": GALLERY_SEED_MARKER_ID},
    )


def _gallery_seed_rows_present(session: Session) -> bool:
    """Detect a library that already carries gallery rows seeded before the marker existed."""
    for model in (AgentProfile, Skill, GeneralSkill, Tool, KnowledgeBase):
        for row in session.exec(select(model)).all():
            metadata = getattr(row, "metadata_json", None) or {}
            if isinstance(metadata, dict) and metadata.get("seed_source") == SEED_SOURCE:
                return True
    return False


def _load_seed_fixtures(paths: Iterable[Path]) -> JsonDict:
    merged: JsonDict = {}
    for path in paths:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        for key, value in fixture.items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            elif key not in merged:
                merged[key] = value
    return merged


def _seed_admin_user_id(session: Session) -> str:
    """种子资源归当前租户的管理员账号所有；管理员尚未落库时回落到常量 id。"""
    admin = session.exec(
        select(User).where(User.tenant_id == TENANT_ID, User.username == ADMIN_USERNAME)
    ).first()
    return admin.id if admin else ADMIN_USER_ID


def _seed_agents(session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]) -> None:
    admin_user_id = _seed_admin_user_id(session)
    for row in rows:
        source_id = str(row.get("id") or "")
        name = str(row.get("name") or "").strip()
        if not source_id or not name:
            continue
        existing_by_id = session.get(AgentProfile, source_id)
        # 员工名只在同一归属内唯一：查找冲突必须带上归属，否则会把别人同名的员工当成自己人。
        existing_by_name = session.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == TENANT_ID,
                AgentProfile.owner_user_id == admin_user_id,
                AgentProfile.name == name,
            )
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        metadata = _agent_metadata(row.get("metadata_json"))
        if existing:
            existing_metadata = existing.metadata_json or {}
            metadata.update(
                {
                    key: existing_metadata[key]
                    for key in USER_EDITABLE_AGENT_METADATA_KEYS
                    if key in existing_metadata
                }
            )
        payload = {
            "tenant_id": TENANT_ID,
            "name": name,
            "description": row.get("description"),
            "persona_prompt": row.get("persona_prompt"),
            "status": row.get("status") or "active",
            "metadata_json": metadata,
            # 种子员工归管理员账号所有，且视为已发布到广场。
            "owner_user_id": admin_user_id,
            "is_published": True,
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["agent"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            continue
        else:
            agent = AgentProfile(id=source_id, **payload)
            session.add(agent)
            id_maps["agent"][source_id] = source_id


def _seed_skills(
    session: Session,
    skill_rows: Iterable[JsonDict],
    version_rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    selected_ids = resource_ids.get("skill", set())
    selected_skill_ids: set[str] = set()
    for row in skill_rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        skill_id = str(row.get("skill_id") or "").strip()
        if not skill_id:
            continue
        existing_by_id = session.get(Skill, source_id)
        existing_by_skill_id = session.exec(
            select(Skill).where(Skill.tenant_id == TENANT_ID, Skill.skill_id == skill_id)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_skill_id, source_id)
        content = _json_object(row.get("content_json"))
        _apply_curated_skill_runtime_defaults(skill_id, content)
        content.update({"skill_id": skill_id, "name": row.get("name"), "version": row.get("version") or "1.0.0"})
        payload = {
            "tenant_id": TENANT_ID,
            "scope": GALLERY_SCOPE,
            "skill_id": skill_id,
            "version": row.get("version") or "1.0.0",
            "name": row.get("name") or skill_id,
            "business_domain": row.get("business_domain"),
            "description": row.get("description"),
            "content_json": content,
            "status": "published",
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            target_id = existing.id
        elif existing_by_id or existing_by_skill_id:
            continue
        else:
            skill = Skill(id=source_id, **payload)
            session.add(skill)
            target_id = source_id
        id_maps["skill"][source_id] = target_id
        id_maps["skill_business"][skill_id] = skill_id
        selected_skill_ids.add(skill_id)
    session.flush()

    for row in version_rows:
        skill_id = str(row.get("skill_id") or "").strip()
        if skill_id not in selected_skill_ids:
            continue
        version = str(row.get("version") or "1.0.0")
        source_id = str(row.get("id") or "")
        existing_by_id = session.get(SkillVersion, source_id)
        existing_by_version = session.exec(
            select(SkillVersion).where(
                SkillVersion.tenant_id == TENANT_ID,
                SkillVersion.skill_id == skill_id,
                SkillVersion.version == version,
            )
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_version, source_id)
        content = _json_object(row.get("content_json"))
        _apply_curated_skill_runtime_defaults(skill_id, content)
        content.update({"skill_id": skill_id, "name": row.get("name"), "version": version})
        payload = {
            "tenant_id": TENANT_ID,
            "skill_id": skill_id,
            "version": version,
            "name": row.get("name") or skill_id,
            "business_domain": row.get("business_domain"),
            "description": row.get("description"),
            "content_json": content,
            "status": "published",
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        elif existing_by_id or existing_by_version:
            continue
        else:
            session.add(SkillVersion(id=source_id, **payload))


def _seed_general_skills(
    session: Session,
    rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    selected_ids = resource_ids.get("general_skill", set())
    for row in rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        existing_by_id = session.get(GeneralSkill, source_id)
        existing_by_slug = session.exec(
            select(GeneralSkill).where(GeneralSkill.tenant_id == TENANT_ID, GeneralSkill.slug == slug)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_slug, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "scope": GALLERY_SCOPE,
            "slug": slug,
            "name": row.get("name") or slug,
            "description": row.get("description"),
            "homepage": row.get("homepage"),
            "skill_markdown": row.get("skill_markdown") or "",
            "skill_files_json": _json_list(row.get("skill_files_json")),
            "metadata_json": _open_gallery_seed_metadata(row.get("metadata_json")),
            "status": "published",
            "capability_scope": normalize_capability_scope(
                row.get("capability_scope")
                if row.get("capability_scope") is not None
                else getattr(existing, "capability_scope", None)
            ),
            "permissions_json": _json_object(row.get("permissions_json")),
            "runtime_config_json": _json_object(row.get("runtime_config_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["general_skill"][source_id] = existing.id
        elif existing_by_id or existing_by_slug:
            continue
        else:
            session.add(GeneralSkill(id=source_id, **payload))
            id_maps["general_skill"][source_id] = source_id


def _seed_tools(
    session: Session,
    rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    selected_ids = resource_ids.get("tool", set())
    for row in rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing_by_id = session.get(Tool, source_id)
        existing_by_name = session.exec(
            select(Tool).where(Tool.tenant_id == TENANT_ID, Tool.name == name)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        config = _json_object(row.get("config_json"))
        if name == "contract.archive_query":
            config = {**config, "execution": {"timeout_seconds": 20}}
        payload = {
            "tenant_id": TENANT_ID,
            "scope": GALLERY_SCOPE,
            "name": name,
            "display_name": row.get("display_name"),
            "description": row.get("description"),
            "bucket": row.get("bucket") or "未分桶",
            "tool_type": row.get("tool_type") or "http",
            "method": row.get("method") or "POST",
            "url": row.get("url") or "",
            "headers_json": _json_object(row.get("headers_json")),
            "auth_json": _json_object(row.get("auth_json")),
            "config_json": config,
            "input_schema": _json_object(row.get("input_schema")),
            "output_schema": _json_object(row.get("output_schema")),
            "allowed_skills_json": _json_list(row.get("allowed_skills_json")),
            "mcp_server_id": row.get("mcp_server_id"),
            "capability_scope": normalize_capability_scope(
                row.get("capability_scope")
                if row.get("capability_scope") is not None
                else getattr(existing, "capability_scope", None)
            ),
            "capability_scope_inherited": bool(
                row.get("capability_scope_inherited")
                if row.get("capability_scope_inherited") is not None
                else getattr(existing, "capability_scope_inherited", True)
            ),
            "enabled": bool(row.get("enabled", True)),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["tool"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            continue
        else:
            session.add(Tool(id=source_id, **payload))
            id_maps["tool"][source_id] = source_id


def _apply_curated_skill_runtime_defaults(skill_id: str, content: JsonDict) -> None:
    if skill_id != "leave_apply_v1":
        return
    nodes = content.get("nodes")
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict) or node.get("node_id") != "check_policy":
            continue
        scope = _json_object(node.get("knowledge_scope"))
        scope.setdefault("query_fields", ["leave_type"])
        node["knowledge_scope"] = scope


def _seed_knowledge(
    session: Session,
    data: JsonDict,
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    selected_ids = resource_ids.get("knowledge_base", set())
    for row in data.get("knowledge_bases", []):
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing_by_id = session.get(KnowledgeBase, source_id)
        existing_by_name = session.exec(
            select(KnowledgeBase).where(KnowledgeBase.tenant_id == TENANT_ID, KnowledgeBase.name == name)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "scope": GALLERY_SCOPE,
            "name": name,
            "description": row.get("description"),
            "status": row.get("status") or "active",
            "capability_scope": normalize_capability_scope(
                row.get("capability_scope")
                if row.get("capability_scope") is not None
                else getattr(existing, "capability_scope", None)
            ),
            "metadata_json": _open_gallery_seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_base"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            continue
        else:
            session.add(KnowledgeBase(id=source_id, **payload))
            id_maps["knowledge_base"][source_id] = source_id
    session.flush()

    _seed_knowledge_versions(session, data.get("knowledge_base_versions", []), id_maps)
    _seed_knowledge_documents(session, data.get("knowledge_documents", []), id_maps)
    _seed_knowledge_buckets(session, data.get("knowledge_buckets", []), id_maps)
    _seed_knowledge_chunks(session, data.get("knowledge_chunks", []), id_maps)
    _seed_knowledge_concepts(session, data.get("knowledge_concepts", []), id_maps)
    _seed_knowledge_discovery(session, data.get("knowledge_discovery_suggestions", []), id_maps)
    _seed_knowledge_jobs(session, data.get("knowledge_ingest_jobs", []), id_maps)


def _seed_knowledge_versions(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        if not kb_id:
            continue
        knowledge_base = session.get(KnowledgeBase, kb_id)
        version = str(row.get("version") or "1.0.0")
        source_id = str(row.get("id") or "")
        existing_by_id = session.get(KnowledgeBaseVersion, source_id)
        existing_by_version = session.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.tenant_id == TENANT_ID,
                KnowledgeBaseVersion.knowledge_base_id == kb_id,
                KnowledgeBaseVersion.version == version,
            )
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_version, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "version": version,
            "name": row.get("name") or version,
            "description": row.get("description"),
            "status": row.get("status") or "active",
            "capability_scope": normalize_capability_scope(
                row.get("capability_scope")
                if row.get("capability_scope") is not None
                else getattr(
                    existing,
                    "capability_scope",
                    getattr(knowledge_base, "capability_scope", None),
                )
            ),
            "metadata_json": _open_gallery_seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_base_version"][source_id] = existing.id
        elif existing_by_id or existing_by_version:
            continue
        else:
            session.add(KnowledgeBaseVersion(id=source_id, **payload))
            id_maps["knowledge_base_version"][source_id] = source_id
    session.flush()


def _seed_knowledge_documents(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        if not kb_id or not kb_ver_id:
            continue
        source_id = str(row.get("id") or "")
        existing = session.get(KnowledgeDocument, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "filename": row.get("filename") or "document",
            "file_type": row.get("file_type") or "text",
            "title": row.get("title"),
            "status": row.get("status") or "ready",
            "bucket_count": int(row.get("bucket_count") or 0),
            "chunk_count": int(row.get("chunk_count") or 0),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
            "error": row.get("error"),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_document"][source_id] = existing.id
        else:
            session.add(KnowledgeDocument(id=source_id, **payload))
            id_maps["knowledge_document"][source_id] = source_id
    session.flush()


def _seed_knowledge_buckets(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        if not kb_id or not kb_ver_id or not doc_id:
            continue
        source_id = str(row.get("id") or "")
        existing = session.get(KnowledgeBucket, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "bucket_key": row.get("bucket_key") or source_id,
            "title": row.get("title") or "Untitled",
            "summary": row.get("summary") or "",
            "token_estimate": int(row.get("token_estimate") or 0),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_bucket"][source_id] = existing.id
        else:
            session.add(KnowledgeBucket(id=source_id, **payload))
            id_maps["knowledge_bucket"][source_id] = source_id
    session.flush()


def _seed_knowledge_chunks(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        bucket_id = id_maps["knowledge_bucket"].get(str(row.get("bucket_id") or ""))
        if not kb_id or not kb_ver_id or not doc_id or not bucket_id:
            continue
        existing = session.get(KnowledgeChunk, str(row.get("id") or ""))
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "bucket_id": bucket_id,
            "chunk_index": int(row.get("chunk_index") or 0),
            "content": row.get("content") or "",
            "summary": row.get("summary"),
            "source_ref": row.get("source_ref"),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeChunk(id=str(row.get("id") or ""), **payload))


def _seed_knowledge_concepts(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        if not kb_id or not kb_ver_id:
            continue
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        concept_id = row.get("concept_id") or str(row.get("id") or "")
        existing = session.get(KnowledgeConcept, str(row.get("id") or "")) or session.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.tenant_id == TENANT_ID,
                KnowledgeConcept.knowledge_base_version_id == kb_ver_id,
                KnowledgeConcept.concept_id == concept_id,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "concept_id": concept_id,
            "concept_type": row.get("concept_type") or "Concept",
            "title": row.get("title") or concept_id,
            "description": row.get("description"),
            "content_md": row.get("content_md") or "",
            "frontmatter_json": _json_object(row.get("frontmatter_json")),
            "links_json": _json_list(row.get("links_json")),
            "citations_json": _json_list(row.get("citations_json")),
            "source_refs_json": _json_list(row.get("source_refs_json")),
            "status": row.get("status") or "active",
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeConcept(id=str(row.get("id") or ""), **payload))


def _seed_knowledge_discovery(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        bucket_id = id_maps["knowledge_bucket"].get(str(row.get("bucket_id") or ""))
        if not kb_id or not kb_ver_id or not doc_id:
            continue
        existing = session.get(KnowledgeDiscoverySuggestion, str(row.get("id") or ""))
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "bucket_id": bucket_id,
            "suggestion_type": row.get("suggestion_type") or "suggestion",
            "title": row.get("title") or "Suggestion",
            "status": row.get("status") or "pending",
            "payload_json": _json_object(row.get("payload_json")),
            "source_refs_json": _json_list(row.get("source_refs_json")),
            "reason": row.get("reason"),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeDiscoverySuggestion(id=str(row.get("id") or ""), **payload))


def _seed_knowledge_jobs(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        if not kb_id or not kb_ver_id:
            continue
        existing = session.get(KnowledgeIngestJob, str(row.get("id") or ""))
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "filename": row.get("filename") or "document",
            "status": row.get("status") or "completed",
            "stage": row.get("stage") or "completed",
            "progress": float(row.get("progress") or 0),
            "error": row.get("error"),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
            "started_at": _parse_datetime(row.get("started_at")),
            "finished_at": _parse_datetime(row.get("finished_at")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeIngestJob(id=str(row.get("id") or ""), **payload))


def _seed_agent_resource_references(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """种子引用行：引用 = 行存在，没有 status/metadata 可改。"""
    for row in rows:
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        resource_type = str(row.get("resource_type") or "")
        resource_id = _mapped_resource_id(resource_type, str(row.get("resource_id") or ""), id_maps)
        if not agent_id or not resource_id:
            continue
        existing = session.exec(
            select(AgentResourceReference).where(
                AgentResourceReference.tenant_id == TENANT_ID,
                AgentResourceReference.agent_id == agent_id,
                AgentResourceReference.resource_type == resource_type,
                AgentResourceReference.resource_id == resource_id,
            )
        ).first()
        if existing:
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(
                AgentResourceReference(
                    id=str(row.get("id") or ""),
                    tenant_id=TENANT_ID,
                    agent_id=agent_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    created_by_user_id=ADMIN_USER_ID,
                )
            )


def _publish_gallery_resources(session: Session, id_maps: dict[str, dict[str, str]]) -> None:
    metadata = _open_gallery_seed_metadata({})
    for resource_type, model, map_key in (
        ("skill", Skill, "skill"),
        ("general_skill", GeneralSkill, "general_skill"),
        ("tool", Tool, "tool"),
        ("knowledge_base", KnowledgeBase, "knowledge_base"),
    ):
        for resource_id in set(id_maps[map_key].values()):
            resource = session.get(model, resource_id)
            if resource is None:
                continue
            if hasattr(resource, "metadata_json"):
                resource.metadata_json = _open_gallery_seed_metadata(getattr(resource, "metadata_json", None))
                resource.updated_at = utc_now()
                session.add(resource)
            if hasattr(resource, "scope"):
                resource.scope = GALLERY_SCOPE
            if hasattr(resource, "owner_agent_id"):
                resource.owner_agent_id = None
            ensure_open_gallery_binding(
                session,
                TENANT_ID,
                resource_type,
                resource_id,
                "active",
                metadata_json=metadata,
            )


def _sync_seed_agents_to_current_admin(
    session: Session, id_maps: dict[str, dict[str, str]]
) -> None:
    admin = session.exec(
        select(User).where(User.tenant_id == TENANT_ID, User.username == ADMIN_USERNAME)
    ).first()
    if not admin:
        return
    admin_metadata = {
        "owner_user_id": admin.id,
        "owner_username": admin.username,
        "owner_display_name": admin.display_name or ADMIN_DISPLAY_NAME,
        "created_by_user_id": admin.id,
        "created_by_username": admin.username,
        "created_by": admin.username,
        "created_by_display_name": admin.display_name or ADMIN_DISPLAY_NAME,
        "creator_name": admin.username,
    }
    for agent_id in set(id_maps["agent"].values()):
        agent = session.get(AgentProfile, agent_id)
        if agent is None:
            continue
        metadata = dict(agent.metadata_json or {})
        metadata.update(admin_metadata)
        metadata["seed_source"] = SEED_SOURCE
        metadata["managed_by_seed"] = True
        agent.metadata_json = metadata
        # 归属与发布状态提升为列。
        agent.owner_user_id = admin.id
        agent.is_published = True
        agent.published_by = admin.username
        agent.updated_at = utc_now()
        session.add(agent)


def _resource_ids_by_type(rows: Iterable[JsonDict]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in rows:
        resource_type = str(row.get("resource_type") or "")
        resource_id = str(row.get("resource_id") or "")
        if resource_type and resource_id:
            result.setdefault(resource_type, set()).add(resource_id)
    return result


def _mapped_resource_id(
    resource_type: str, source_id: str, id_maps: dict[str, dict[str, str]]
) -> str | None:
    map_key = {
        "skill": "skill",
        "general_skill": "general_skill",
        "tool": "tool",
        "knowledge_base": "knowledge_base",
    }.get(resource_type)
    if map_key is None:
        return None
    return id_maps[map_key].get(source_id)


def _agent_metadata(value: Any) -> JsonDict:
    """员工 metadata 只留署名与种子标记：发布状态由 `is_published` 列表达。"""
    metadata = _seed_metadata(value)
    metadata.update(
        {
            "seed_source": SEED_SOURCE,
            "managed_by_seed": True,
        }
    )
    return metadata


def _open_gallery_seed_metadata(value: Any) -> JsonDict:
    """广场资源的 metadata 不再写 scope/visibility —— 那是 `scope` 列的事。"""
    metadata = _seed_metadata(value)
    metadata["seed_source"] = SEED_SOURCE
    metadata["managed_by_seed"] = True
    return metadata


def _seed_metadata(value: Any) -> JsonDict:
    metadata = _json_object(value)
    # 老 fixture 里带着「归属化改造前」的状态键；新结构下它们由列承载，
    # 留在 metadata 会形成两处真相，所以在唯一的入口处统一剔除。
    for key in _LEGACY_OWNERSHIP_METADATA_KEYS:
        metadata.pop(key, None)
    metadata.update(
        {
            "owner_user_id": ADMIN_USER_ID,
            "owner_username": ADMIN_USERNAME,
            "owner_display_name": ADMIN_DISPLAY_NAME,
            "created_by_user_id": ADMIN_USER_ID,
            "created_by_username": ADMIN_USERNAME,
            "created_by": ADMIN_USERNAME,
            "created_by_display_name": ADMIN_DISPLAY_NAME,
            "creator_name": ADMIN_USERNAME,
        }
    )
    return metadata


def _seed_update_target(
    existing_by_id: ModelT | None,
    existing_by_key: ModelT | None,
    source_id: str,
) -> ModelT | None:
    if (
        existing_by_id is not None
        and existing_by_key is not None
        and getattr(existing_by_id, "id", None) != getattr(existing_by_key, "id", None)
    ):
        return existing_by_key if _is_seed_managed(existing_by_key, source_id) else None
    for candidate in (existing_by_id, existing_by_key):
        if candidate is not None and _is_seed_managed(candidate, source_id):
            return candidate
    return None


def _is_seed_managed(row: object, source_id: str) -> bool:
    if getattr(row, "id", None) == source_id:
        return True
    metadata = getattr(row, "metadata_json", None) or {}
    if not isinstance(metadata, dict):
        return False
    return metadata.get("seed_source") == SEED_SOURCE or metadata.get("managed_by_seed") is True


def _json_object(value: Any) -> JsonDict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _apply_payload(row: Any, payload: JsonDict) -> None:
    for key, value in payload.items():
        setattr(row, key, value)
