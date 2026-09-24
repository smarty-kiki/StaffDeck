from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from app.db.models import (
    AGENT_SCOPE,
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
    MCPServer,
    ModelConfig,
    Skill,
    SkillVersion,
    Tool,
    User,
    utc_now,
)
from app.llm.model_config_resolver import (
    ResolvedModelConfig,
    resolve_model_config_for_runtime,
)

DEFAULT_AGENT_ROLES = ("default", "router", "step", "response", "general_skill")
STANDARD_CREATOR_METADATA_KEYS = (
    "creator_name",
    "created_by",
    "created_by_display_name",
    "created_by_username",
)
CREATOR_SOURCE_METADATA_KEYS = (
    "owner_display_name",
    "owner_username",
    "created_by_user_id",
    "owner_user_id",
)
CREATOR_METADATA_KEYS = STANDARD_CREATOR_METADATA_KEYS + CREATOR_SOURCE_METADATA_KEYS


def _valid_creator_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def user_creator_metadata(
    user: object | None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    metadata = dict(extra or {})
    if user is None:
        return metadata
    user_id = getattr(user, "id", None)
    username = getattr(user, "username", None)
    display_name = getattr(user, "display_name", None) or username
    if _valid_creator_value(user_id):
        metadata["owner_user_id"] = str(user_id).strip()
        metadata["created_by_user_id"] = str(user_id).strip()
    if _valid_creator_value(username):
        normalized_username = str(username).strip()
        metadata["owner_username"] = normalized_username
        metadata["created_by_username"] = normalized_username
        metadata["created_by"] = normalized_username
        metadata["creator_name"] = normalized_username
    if _valid_creator_value(display_name):
        normalized_display_name = str(display_name).strip()
        metadata["owner_display_name"] = normalized_display_name
        metadata["created_by_display_name"] = normalized_display_name
    return metadata


def metadata_preserving_creator(
    existing: dict[str, Any] | None,
    replacement: dict[str, Any] | None,
) -> dict[str, Any]:
    """Replace editable metadata without changing the original creator."""
    metadata = dict(replacement or {})
    current = dict(existing or {})
    for key in CREATOR_METADATA_KEYS:
        value = current.get(key)
        if _valid_creator_value(value):
            metadata[key] = value
    return metadata


def get_agent(db: Session, tenant_id: str, agent_id: str | None) -> AgentProfile | None:
    if not agent_id:
        return None
    return db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.id == agent_id,
            AgentProfile.status != "archived",
        )
    ).first()


RESOURCE_MODELS: dict[str, type] = {
    "skill": Skill,
    "knowledge_base": KnowledgeBase,
    "general_skill": GeneralSkill,
    "tool": Tool,
}


def resource_model_for_type(resource_type: str) -> type | None:
    return RESOURCE_MODELS.get(resource_type)


def get_resource_row(db: Session, tenant_id: str, resource_type: str, resource_id: str):
    model = resource_model_for_type(resource_type)
    if model is None:
        return None
    row = db.get(model, resource_id)
    if row is None or getattr(row, "tenant_id", None) != tenant_id:
        return None
    return row


def resource_is_gallery(resource: object) -> bool:
    return getattr(resource, "scope", None) == GALLERY_SCOPE


def resource_is_available(resource: object) -> bool:
    """下架的广场资源对使用者不可见，但保留引用行以便恢复。"""
    return getattr(resource, "status", None) != "hidden"


def mark_resource_open_gallery(
    resource: object, metadata_json: dict[str, Any] | None = None
) -> None:
    """把资源标为广场共享（scope='gallery'）。"""
    if hasattr(resource, "scope"):
        setattr(resource, "scope", GALLERY_SCOPE)
    if hasattr(resource, "owner_agent_id"):
        setattr(resource, "owner_agent_id", None)


def mark_resource_private_for_agent(
    resource: object,
    agent_id: str,
    metadata_json: dict[str, Any] | None = None,
) -> None:
    """把资源标为某员工私有（scope='agent' + 归属）。"""
    if hasattr(resource, "scope"):
        setattr(resource, "scope", AGENT_SCOPE)
    if hasattr(resource, "owner_agent_id"):
        setattr(resource, "owner_agent_id", agent_id)


def ensure_open_gallery_binding(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    status: str = "active",
    metadata_json: dict[str, Any] | None = None,
    revive: bool = False,
) -> None:
    """把资源发布为广场共享资源（取代旧的「绑定到整体智能体」）。"""
    row = get_resource_row(db, tenant_id, resource_type, resource_id)
    if row is None:
        return
    mark_resource_open_gallery(row)
    if revive and getattr(row, "status", None) == "hidden":
        setattr(row, "status", "active")
    if hasattr(row, "updated_at"):
        setattr(row, "updated_at", utc_now())
    db.add(row)


def hide_open_gallery_binding(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
) -> bool:
    """从广场下架：资源标记 hidden，引用行保留（便于恢复）。"""
    row = get_resource_row(db, tenant_id, resource_type, resource_id)
    if row is None or not resource_is_gallery(row):
        return False
    if hasattr(row, "status"):
        setattr(row, "status", "hidden")
    if hasattr(row, "updated_at"):
        setattr(row, "updated_at", utc_now())
    db.add(row)
    return True


def purge_resource_references(
    db: Session, tenant_id: str, resource_type: str, resource_id: str
) -> int:
    """删除指向某资源的全部引用行（资源被删除或收归私有时调用）。"""
    rows = db.exec(
        select(AgentResourceReference).where(
            AgentResourceReference.tenant_id == tenant_id,
            AgentResourceReference.resource_type == resource_type,
            AgentResourceReference.resource_id == resource_id,
        )
    ).all()
    for row in rows:
        db.delete(row)
    return len(rows)


def _purge_resource_children(
    db: Session, tenant_id: str, resource_type: str, row: object
) -> None:
    """删除资源自身的子行（知识库文档/分块、技能版本历史）。"""
    if resource_type == "knowledge_base":
        for model in (
            KnowledgeDocument,
            KnowledgeBucket,
            KnowledgeChunk,
            KnowledgeConcept,
            KnowledgeDiscoverySuggestion,
            KnowledgeBaseVersion,
        ):
            children = db.exec(
                select(model).where(
                    model.tenant_id == tenant_id, model.knowledge_base_id == row.id
                )
            ).all()
            for child in children:
                db.delete(child)
    elif resource_type == "skill":
        versions = db.exec(
            select(SkillVersion).where(
                SkillVersion.tenant_id == tenant_id,
                SkillVersion.skill_id == row.skill_id,
            )
        ).all()
        for version in versions:
            db.delete(version)


def purge_agent_owned_resources(
    db: Session, tenant_id: str, agent_id: str
) -> dict[str, int]:
    """删除该员工**私有**的全部资源（知识库/技能/SOP/工具）及其子行与引用行。

    只删 `scope='agent' AND owner_agent_id=<该员工>` 的行 —— 广场共享资源不属于
    任何员工，不会被误删。员工删除、账号注销时调用，保证"账号被删除时，其创建的
    数字员工及资源一并删除"。
    """
    counts: dict[str, int] = {}
    for resource_type, model in RESOURCE_MODELS.items():
        rows = db.exec(
            select(model).where(
                model.tenant_id == tenant_id,
                model.scope == AGENT_SCOPE,
                model.owner_agent_id == agent_id,
            )
        ).all()
        for row in rows:
            purge_resource_references(db, tenant_id, resource_type, row.id)
            _purge_resource_children(db, tenant_id, resource_type, row)
            db.delete(row)
        counts[resource_type] = len(rows)
    return counts


def count_resource_references(
    db: Session, tenant_id: str, resource_type: str, resource_id: str
) -> int:
    """被多少个员工引用 —— 删除广场资源前提示用。"""
    agent_ids = db.exec(
        select(AgentResourceReference.agent_id).where(
            AgentResourceReference.tenant_id == tenant_id,
            AgentResourceReference.resource_type == resource_type,
            AgentResourceReference.resource_id == resource_id,
        )
    ).all()
    return len(set(agent_ids))


def reference_resource(
    db: Session,
    tenant_id: str,
    agent_id: str,
    resource_type: str,
    resource_id: str,
    created_by_user_id: str | None = None,
) -> bool:
    """员工引用一个广场资源（幂等）。引用 = 有行。"""
    row = get_resource_row(db, tenant_id, resource_type, resource_id)
    if row is None or not resource_is_gallery(row):
        return False
    ensure_visible_name_unique(
        db,
        tenant_id,
        agent_id,
        resource_type,
        _resource_business_key(resource_type, row),
        # 已引用的这条资源会出现在可见集里，别把自己判成冲突（引用必须幂等）。
        exclude_id=resource_id,
    )
    existing = db.exec(
        select(AgentResourceReference).where(
            AgentResourceReference.tenant_id == tenant_id,
            AgentResourceReference.agent_id == agent_id,
            AgentResourceReference.resource_type == resource_type,
            AgentResourceReference.resource_id == resource_id,
        )
    ).first()
    if existing:
        return True
    db.add(
        AgentResourceReference(
            tenant_id=tenant_id,
            agent_id=agent_id,
            resource_type=resource_type,
            resource_id=resource_id,
            created_by_user_id=created_by_user_id,
        )
    )
    return True


# 各类资源的业务键：同一员工可见集内不允许重名（见设计 4.7）。
_RESOURCE_BUSINESS_KEYS = {
    "tool": "name",
    "knowledge_base": "name",
    "general_skill": "slug",
    "skill": "skill_id",
}


def _resource_business_key(resource_type: str, row: object) -> str:
    field = _RESOURCE_BUSINESS_KEYS.get(resource_type)
    return str(getattr(row, field, "") or "") if field else ""


def visible_resource_names(
    db: Session, tenant_id: str, agent_id: str, resource_type: str
) -> dict[str, str]:
    """员工可见集内的 业务键 → resource_id（私有 ∪ 已引用）。"""
    field = _RESOURCE_BUSINESS_KEYS.get(resource_type)
    model = resource_model_for_type(resource_type)
    if field is None or model is None:
        return {}
    return {
        str(getattr(row, field)): row.id
        for row in _agent_visible_rows(
            db, tenant_id, agent_id, model, resource_type, include_inactive=True
        )
    }


def ensure_visible_name_unique(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    resource_type: str,
    name: str | None,
    exclude_id: str | None = None,
) -> None:
    """同一员工可见集内不能重名 —— 私有资源与引用的广场资源共用一套命名空间。

    表级唯一索引只能表达"表内唯一"，而真正冲突的是跨来源：自己的私有工具 `search`
    与引用的广场工具 `search` 会同时进入同一个 prompt。这里把隐式改名变成显式 409。
    """
    if not agent_id or not name:
        return
    existing_id = visible_resource_names(db, tenant_id, agent_id, resource_type).get(str(name))
    if existing_id and existing_id != exclude_id:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409,
            detail=f"{resource_type} '{name}' is already visible to this staff",
        )


def unreference_resource(
    db: Session, tenant_id: str, agent_id: str, resource_type: str, resource_id: str
) -> bool:
    """取消引用 —— 删行，不写任何状态。"""
    existing = db.exec(
        select(AgentResourceReference).where(
            AgentResourceReference.tenant_id == tenant_id,
            AgentResourceReference.agent_id == agent_id,
            AgentResourceReference.resource_type == resource_type,
            AgentResourceReference.resource_id == resource_id,
        )
    ).first()
    if not existing:
        return False
    db.delete(existing)
    return True


def referenced_resource_ids(
    db: Session, tenant_id: str, agent_id: str, resource_type: str
) -> list[str]:
    rows = db.exec(
        select(AgentResourceReference.resource_id).where(
            AgentResourceReference.tenant_id == tenant_id,
            AgentResourceReference.agent_id == agent_id,
            AgentResourceReference.resource_type == resource_type,
        )
    ).all()
    return [row if isinstance(row, str) else row[0] for row in rows]


def ensure_private_resource_binding(
    db: Session,
    tenant_id: str,
    agent_id: str,
    resource_type: str,
    resource_id: str,
    status: str = "active",
    metadata_json: dict[str, Any] | None = None,
    revive: bool = False,
) -> None:
    """把资源收归某员工私有（取代旧的「建立私有分支/绑定」）。

    私有资源「归属即生效」——写两列即可，不需要引用行。
    """
    row = get_resource_row(db, tenant_id, resource_type, resource_id)
    if row is None:
        return
    mark_resource_private_for_agent(row, agent_id)
    if getattr(row, "status", None) == "hidden":
        setattr(row, "status", "active")
    if hasattr(row, "updated_at"):
        setattr(row, "updated_at", utc_now())
    db.add(row)
    # 不再是广场资源 → 清掉指向它的引用行
    purge_resource_references(db, tenant_id, resource_type, resource_id)


def resource_binding_metadata(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    resource_type: str,
) -> dict[str, dict[str, Any]]:
    """兼容保留：引用行不再携带 metadata，归属由资源表表达。"""
    return {}


def resource_creator_metadata(
    db: Session, tenant_id: str, resource: object
) -> dict[str, Any]:
    """资源的创建人展示信息。

    `skills` / `tools` 没有 metadata 列，创建人只能由 `created_by_user_id` 列派生 ——
    归属是列（能力来源），创建人是视图（展示）。`knowledge_bases` / `general_skills`
    自带 metadata_json，直接以它为准。
    """
    metadata = _resource_metadata(resource)
    if metadata:
        return metadata
    user_id = getattr(resource, "created_by_user_id", None)
    if not user_id:
        return {}
    user = db.get(User, user_id)
    if user is None or getattr(user, "tenant_id", None) != tenant_id:
        return {}
    return user_creator_metadata(user)


def is_open_gallery_resource(
    db: Session, tenant_id: str, resource_type: str, resource: object
) -> bool:
    if getattr(resource, "tenant_id", None) != tenant_id:
        return False
    return resource_is_gallery(resource) and resource_is_available(resource)


def is_bound_resource_visible_for_agent(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource: object,
    binding: AgentResourceReference,
) -> bool:
    """引用行能拿到资源 → 可见；资源下架/收归私有 → 不可见。"""
    if getattr(resource, "tenant_id", None) != tenant_id:
        return False
    if resource_is_gallery(resource):
        return resource_is_available(resource)
    return getattr(resource, "owner_agent_id", None) == binding.agent_id


def _sort_by_updated(rows: list) -> list:
    return sorted(
        rows,
        key=lambda item: getattr(item, "updated_at", None) or utc_now(),
        reverse=True,
    )


def _gallery_rows(db: Session, tenant_id: str, model, include_inactive: bool) -> list:
    """广场视图：全部 scope='gallery' 且未下架的资源。"""
    rows = db.exec(
        select(model).where(model.tenant_id == tenant_id, model.scope == GALLERY_SCOPE)
    ).all()
    out = [row for row in rows if resource_is_available(row)]
    if not include_inactive:
        out = [row for row in out if getattr(row, "status", None) in {"active", "published"}]
    return _sort_by_updated(out)


def _agent_visible_rows(
    db: Session,
    tenant_id: str,
    agent_id: str,
    model,
    resource_type: str,
    include_inactive: bool,
) -> list:
    """员工可见 = 自己拥有的（归属即生效） ∪ 已引用的广场资源。无副本。"""
    owned = db.exec(
        select(model).where(
            model.tenant_id == tenant_id,
            model.scope == AGENT_SCOPE,
            model.owner_agent_id == agent_id,
        )
    ).all()
    out = [row for row in owned if resource_is_available(row)]
    seen_ids = {getattr(row, "id", None) for row in out}
    for resource_id in referenced_resource_ids(db, tenant_id, agent_id, resource_type):
        if resource_id in seen_ids:
            continue
        row = db.get(model, resource_id)
        if row is None or getattr(row, "tenant_id", None) != tenant_id:
            continue
        if not resource_is_gallery(row) or not resource_is_available(row):
            continue
        out.append(row)
    if not include_inactive:
        out = [row for row in out if getattr(row, "status", None) in {"active", "published"}]
    return _sort_by_updated(out)


def visible_skill_rows(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = True,
) -> list[Skill]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent:
        return _gallery_rows(db, tenant_id, Skill, include_inactive)
    return _agent_visible_rows(db, tenant_id, agent.id, Skill, "skill", include_inactive)


def visible_published_skills(
    db: Session, tenant_id: str, agent_id: str | None = None
) -> list[Skill]:
    return [
        skill
        for skill in visible_skill_rows(db, tenant_id, agent_id)
        if skill.status == "published"
    ]


def visible_skill(
    db: Session, tenant_id: str, skill_id: str, agent_id: str | None = None
) -> Skill | None:
    skill = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()
    if not skill or getattr(skill, "status", None) == "deleted":
        return None
    agent = get_agent(db, tenant_id, agent_id)
    if not agent:
        if not is_open_gallery_resource(db, tenant_id, "skill", skill):
            return None
        return skill
    if getattr(skill, "scope", None) == AGENT_SCOPE:
        return skill if getattr(skill, "owner_agent_id", None) == agent.id else None
    if not is_open_gallery_resource(db, tenant_id, "skill", skill):
        return None
    if skill.id not in set(referenced_resource_ids(db, tenant_id, agent.id, "skill")):
        return None
    return skill


def visible_general_skill_rows(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = True,
) -> list[GeneralSkill]:
    """员工可见的通用技能 = 自己拥有的 ∪ 已引用的广场技能。"""
    agent = get_agent(db, tenant_id, agent_id)
    if agent_id and not agent:
        return []
    if not agent:
        return _gallery_rows(db, tenant_id, GeneralSkill, include_inactive)
    return _agent_visible_rows(
        db, tenant_id, agent.id, GeneralSkill, "general_skill", include_inactive
    )


def visible_tool_rows(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = True,
) -> list[Tool]:
    agent = get_agent(db, tenant_id, agent_id)
    if agent_id and not agent:
        return []
    if not agent:
        rows = _gallery_rows(db, tenant_id, Tool, True)
    else:
        rows = _agent_visible_rows(db, tenant_id, agent.id, Tool, "tool", True)
    visible = [row for row in rows if include_inactive or _tool_runtime_enabled(db, row)]
    return sorted(visible, key=lambda row: (row.bucket, row.name))


def _tool_runtime_enabled(db: Session, row: Tool) -> bool:
    if not row.enabled:
        return False
    if not row.mcp_server_id:
        return True
    server = db.get(MCPServer, row.mcp_server_id)
    return bool(
        server
        and server.tenant_id == row.tenant_id
        and server.enabled
    )


def visible_knowledge_base_ids(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = False,
) -> list[str]:
    return list(visible_knowledge_base_versions(db, tenant_id, agent_id, include_inactive).keys())


def visible_knowledge_base_versions(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = False,
) -> dict[str, KnowledgeBaseVersion]:
    """员工可见知识库 = 自己拥有的 ∪ 已引用的广场知识库。无分支、无副本。"""
    agent = get_agent(db, tenant_id, agent_id)
    if not agent:
        rows = _gallery_rows(db, tenant_id, KnowledgeBase, include_inactive)
    else:
        rows = _agent_visible_rows(
            db, tenant_id, agent.id, KnowledgeBase, "knowledge_base", include_inactive
        )
    return {
        row.id: ensure_knowledge_base_version(db, row, _current_knowledge_version(row))
        for row in rows
        if getattr(row, "status", None) != "deleted"
    }


def visible_knowledge_base_version_ids(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = False,
) -> list[str]:
    return [
        row.id
        for row in visible_knowledge_base_versions(
            db, tenant_id, agent_id, include_inactive
        ).values()
    ]


def ensure_knowledge_base_version(
    db: Session, kb: KnowledgeBase, version: str | None = None
) -> KnowledgeBaseVersion:
    normalized_version = version or _current_knowledge_version(kb)
    row = db.exec(
        select(KnowledgeBaseVersion).where(
            KnowledgeBaseVersion.tenant_id == kb.tenant_id,
            KnowledgeBaseVersion.knowledge_base_id == kb.id,
            KnowledgeBaseVersion.version == normalized_version,
        )
    ).first()
    if row:
        return row
    row = KnowledgeBaseVersion(
        id=f"kbver_{kb.id}_{_safe_version_id(normalized_version)}",
        tenant_id=kb.tenant_id,
        knowledge_base_id=kb.id,
        version=normalized_version,
        name=kb.name,
        description=kb.description,
        status=kb.status,
        capability_scope=kb.capability_scope,
        metadata_json=dict(kb.metadata_json or {}),
    )
    db.add(row)
    db.flush()
    return row


def _apply_knowledge_version_metadata(
    db: Session,
    tenant_id: str,
    agent: AgentProfile | None,
    version: KnowledgeBaseVersion,
    metadata_json: dict[str, Any] | None,
) -> None:
    if not metadata_json:
        return
    # 版本元数据跟随知识库本身，不再按"哪个员工在看"分层 —— 内容只有一份。
    version.metadata_json = {**(version.metadata_json or {}), **metadata_json}
    version.updated_at = utc_now()
    db.add(version)


def knowledge_version_for_upload(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
    metadata_json: dict[str, Any] | None = None,
) -> KnowledgeBaseVersion:
    """上传落点。

    知识库内容只有一份（归属该知识库本身），不再为每个员工克隆一份分支。
    员工通过引用使用它 —— 归属人上传新内容，所有引用者立刻可见。
    """
    kb = db.get(KnowledgeBase, knowledge_base_id)
    if not kb or kb.tenant_id != tenant_id or kb.status == "archived":
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Knowledge base not found")
    agent = get_agent(db, tenant_id, agent_id)
    version = ensure_knowledge_base_version(db, kb, _current_knowledge_version(kb))
    _apply_knowledge_version_metadata(db, tenant_id, agent, version, metadata_json)
    return version


def model_for_agent(
    db: Session, tenant_id: str, agent_id: str | None, role: str = "default"
) -> ResolvedModelConfig | None:
    """Resolve every employee task through the tenant's enabled default model.

    ``agent_id`` and ``role`` remain in the signature for call-site compatibility. Employee-level
    model bindings are intentionally no longer part of runtime model selection.
    """
    _ = agent_id, role
    model = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    return _runtime_model(db, tenant_id, model) if model else None


def _runtime_model(
    db: Session, tenant_id: str, model: ModelConfig
) -> ResolvedModelConfig:
    return resolve_model_config_for_runtime(db, tenant_id, model.id)


def next_global_version(version: str) -> str:
    parts = version.split(".")
    if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return f"{version}.1"


def _is_semver(version: str) -> bool:
    parts = version.split(".")
    return len(parts) == 3 and all(part.isdigit() for part in parts)


def _resource_metadata(resource: object) -> dict[str, Any]:
    metadata = getattr(resource, "metadata_json", None)
    return dict(metadata) if isinstance(metadata, dict) else {}


def _get_knowledge_base(db: Session, tenant_id: str, knowledge_base_id: str) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, knowledge_base_id)
    if not kb or kb.tenant_id != tenant_id or kb.status == "archived":
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return kb


def _current_knowledge_version(kb: KnowledgeBase) -> str:
    metadata = kb.metadata_json or {}
    version = metadata.get("current_version") if isinstance(metadata, dict) else None
    return str(version or "1.0.0")


def _safe_version_id(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)
