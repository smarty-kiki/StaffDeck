from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from app.agents.branching import (
    count_resource_references,
    is_open_gallery_resource,
    purge_agent_owned_resources,
    reference_resource,
    resource_model_for_type,
    unreference_resource,
    visible_skill_rows,
)
from app.agents.schema import (
    AgentAPICredentialCreated,
    AgentAPICredentialCreateRequest,
    AgentAPICredentialRead,
    AgentModelsUpdateRequest,
    AgentProfileCreateRequest,
    AgentProfileRead,
    AgentProfileUpdateRequest,
    AgentResourceReferenceInput,
    AgentResourceReferenceRead,
    AgentResourcesUpdateRequest,
    AgentScopeRead,
    AgentWorkRecordEventRead,
    AgentWorkRecordRead,
    AgentWorkRecordReplyStatsRead,
)
from app.db import get_session
from app.db.models import (
    AgentModelBinding,
    AgentProfile,
    AgentResourceReference,
    AgentUsage,
    APIClient,
    APICredential,
    ChannelBinding,
    ChannelBindingAgent,
    ChatSession,
    GeneralSkill,
    HumanHandoffRequest,
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    Message,
    ModelConfig,
    ScheduledTask,
    Skill,
    TeamMember,
    Tool,
    User,
    utc_now,
)
from app.public_api.auth import generate_api_key
from app.public_api.credential_profiles import (
    AGENT_KEY_ALLOWED_SCOPES,
    agent_access_for_scopes,
    scopes_for_agent_access,
)
from app.security.auth import get_current_user
from app.security.permissions import (
    agent_owned_by_user as _agent_owned_by_user,
)
from app.security.permissions import (
    ensure_agent_publish_manager as _ensure_agent_publish_manager,
)
from app.security.permissions import (
    is_admin_user as _is_admin_user,
)
from app.security.tenant import ensure_tenant
from app.session.cleanup import (
    purge_chat_session_records,
    remove_chat_session_workspace,
)

enterprise_router = APIRouter(prefix="/api/enterprise/agents", tags=["enterprise:agents"])
chat_router = APIRouter(prefix="/api/chat/agents", tags=["chat:agents"])
scope_router = APIRouter(prefix="/api/enterprise/agent-scope", tags=["enterprise:agent-scope"])

STAFFDECK_AGENT_API_CLIENT_NAME = "StaffDeck 员工 API 密钥"


@scope_router.get("", response_model=AgentScopeRead)
def get_agent_scope(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentScopeRead:
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    return AgentScopeRead(tenant_id=tenant_id, agents=list_agents(tenant_id, db, current_user))


@enterprise_router.get("", response_model=list[AgentProfileRead])
def list_agents(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentProfileRead]:
    ensure_tenant(db, tenant_id)
    user = current_user
    _ensure_request_tenant(tenant_id, user)
    rows = db.exec(
        select(AgentProfile)
        .where(AgentProfile.tenant_id == tenant_id)
        .order_by(AgentProfile.is_published.desc(), AgentProfile.updated_at.desc())
    ).all()
    rows = [row for row in rows if not _agent_hidden_from_staffdeck(row)]
    if not _is_admin_user(user):
        # 非管理员只看到：自己的员工 ∪ 已发布到广场的员工。
        # 别人的私人员工对他们完全不可见 —— 这里不再有任何"只为复制用"的暴露。
        rows = [row for row in rows if _agent_usable_by_user(row, user)]
    bindings = _bindings_by_agent(db, tenant_id)
    used_agent_ids = _used_agent_ids_for_user(db, tenant_id, user)
    return [agent_read(row, bindings.get(row.id, []), row.id in used_agent_ids) for row in rows]


@enterprise_router.post("", response_model=AgentProfileRead)
def create_agent(
    request: AgentProfileCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    ensure_tenant(db, request.tenant_id)
    user = current_user
    _ensure_request_tenant(request.tenant_id, user)
    name = str(request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Agent name cannot be empty")
    # 同名只在自己的员工里查重：不同作者可以有同名员工，广场里靠 @创建人 区分。
    existing = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == request.tenant_id,
            AgentProfile.owner_user_id == user.id,
            AgentProfile.name == name,
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="You already have a staff with this name")
    row = AgentProfile(
        tenant_id=request.tenant_id,
        owner_user_id=user.id,
        name=name,
        description=request.description,
        persona_prompt=request.persona_prompt,
        status="active",
        harness_max_actions=request.harness_max_actions,
        metadata_json=_metadata_with_creator(request.metadata or {}, user),
    )
    db.add(row)
    db.flush()
    # 新员工从空白开始。需要的广场资源由引用端点挂载（引用而非复制）。
    db.commit()
    db.refresh(row)
    return agent_read(row, _bindings_by_agent(db, request.tenant_id).get(row.id, []))


@enterprise_router.get("/{agent_id}", response_model=AgentProfileRead)
def get_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    row = _get_agent(db, tenant_id, agent_id)
    _ensure_can_access_agent(row, current_user)
    return agent_read(row, _bindings_by_agent(db, tenant_id).get(row.id, []))


@enterprise_router.get(
    "/{agent_id}/api-credentials",
    response_model=list[AgentAPICredentialRead],
)
def list_agent_api_credentials(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentAPICredentialRead]:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    rows = db.exec(
        select(APICredential)
        .where(
            APICredential.tenant_id == tenant_id,
            APICredential.agent_id == agent_id,
        )
        .order_by(APICredential.created_at.desc())
    ).all()
    return [_agent_api_credential_read(row) for row in rows]


@enterprise_router.post(
    "/{agent_id}/api-credentials",
    response_model=AgentAPICredentialCreated,
)
def create_agent_api_credential(
    agent_id: str,
    request: AgentAPICredentialCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentAPICredentialCreated:
    agent = _get_agent(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    client = _ensure_staffdeck_agent_api_client(db, request.tenant_id, current_user)
    token, prefix, digest = generate_api_key()
    row = APICredential(
        tenant_id=request.tenant_id,
        client_id=client.id,
        agent_id=agent_id,
        name=request.name.strip(),
        key_prefix=prefix,
        key_digest=digest,
        scopes_json=scopes_for_agent_access(request.access),
        expires_at=request.expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return AgentAPICredentialCreated(
        **_agent_api_credential_read(row).model_dump(),
        api_key=token,
    )


@enterprise_router.post(
    "/{agent_id}/api-credentials/{credential_id}/rotate",
    response_model=AgentAPICredentialCreated,
)
def rotate_agent_api_credential(
    agent_id: str,
    credential_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentAPICredentialCreated:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    row = _get_agent_api_credential(db, tenant_id, agent_id, credential_id)
    token, prefix, digest = generate_api_key()
    row.key_prefix = prefix
    row.key_digest = digest
    row.status = "active"
    row.revoked_at = None
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return AgentAPICredentialCreated(
        **_agent_api_credential_read(row).model_dump(),
        api_key=token,
    )


@enterprise_router.post(
    "/{agent_id}/api-credentials/{credential_id}/revoke",
    response_model=AgentAPICredentialRead,
)
def revoke_agent_api_credential(
    agent_id: str,
    credential_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentAPICredentialRead:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    row = _get_agent_api_credential(db, tenant_id, agent_id, credential_id)
    row.status = "revoked"
    row.revoked_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _agent_api_credential_read(row)


@enterprise_router.get("/{agent_id}/work-record", response_model=AgentWorkRecordRead)
def get_agent_work_record(
    agent_id: str,
    tenant_id: str = Query(...),
    timezone: str = Query("Asia/Shanghai"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentWorkRecordRead:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_access_agent(agent, current_user)
    try:
        local_timezone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid timezone") from exc

    now = utc_now()
    reply_rows = db.exec(
        select(Message)
        .join(ChatSession, Message.session_id == ChatSession.id)
        .where(
            Message.tenant_id == tenant_id,
            Message.role == "assistant",
            ChatSession.tenant_id == tenant_id,
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == current_user.id,
        )
        .order_by(Message.created_at.asc())
    ).all()
    by_day: dict[str, int] = {}
    events = [
        AgentWorkRecordEventRead(
            id=f"{message.id}:reply",
            kind="chat",
            phase="reply",
            timestamp=_iso_utc(message.created_at),
            label="对话回复",
        )
        for message in reply_rows
    ]
    for message in reply_rows:
        day = _as_utc(message.created_at).astimezone(local_timezone).date().isoformat()
        by_day[day] = by_day.get(day, 0) + 1

    events.extend(_agent_resource_timeline_events(db, tenant_id, agent_id))
    events.extend(_agent_scheduled_task_timeline_events(db, tenant_id, agent_id, current_user))
    events.sort(key=lambda item: (item.timestamp, item.id))
    today = _as_utc(now).astimezone(local_timezone).date().isoformat()
    return AgentWorkRecordRead(
        agent_id=agent_id,
        timezone=timezone,
        generated_at=_iso_utc(now),
        reply_stats=AgentWorkRecordReplyStatsRead(
            total=len(reply_rows),
            today=by_day.get(today, 0),
            by_day=dict(sorted(by_day.items())),
        ),
        events=events,
    )


@enterprise_router.put("/{agent_id}", response_model=AgentProfileRead)
def update_agent(
    agent_id: str,
    request: AgentProfileUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    row = _get_agent(db, request.tenant_id, agent_id)
    user = current_user
    _ensure_can_manage_agent(row, user)
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Agent name cannot be empty")
        # 重名只在归属人自己的员工里查 —— 同名不同作者是允许的。
        conflict = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == request.tenant_id,
                AgentProfile.owner_user_id == row.owner_user_id,
                AgentProfile.name == name,
                AgentProfile.id != row.id,
            )
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail="You already have a staff with this name")
        row.name = name
    if request.description is not None:
        row.description = request.description
    if request.persona_prompt is not None:
        row.persona_prompt = request.persona_prompt
    if request.status is not None:
        row.status = request.status
    if request.harness_max_actions is not None:
        row.harness_max_actions = request.harness_max_actions
    if request.metadata is not None:
        row.metadata_json = _metadata_preserving_creator(
            row.metadata_json or {}, request.metadata, user
        )
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return agent_read(row, _bindings_by_agent(db, request.tenant_id).get(row.id, []))


@enterprise_router.post("/{agent_id}/gallery:publish", response_model=AgentProfileRead)
def publish_agent_to_gallery(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    """把数字员工发布到广场。归属人自己做，即发布给全租户使用。

    发布后员工本身与其资源都保持**归属作者** —— 使用者看到的是实时引用，
    不是快照。其他账号（含管理员）不能编辑，管理员只能下架。
    """
    row = _get_agent(db, tenant_id, agent_id)
    _ensure_agent_publish_manager(row, current_user)
    now = utc_now()
    row.is_published = True
    row.published_at = now
    row.published_by = current_user.username
    row.updated_at = now
    db.add(row)
    db.commit()
    db.refresh(row)
    return agent_read(row, _bindings_by_agent(db, tenant_id).get(row.id, []))


@enterprise_router.post("/{agent_id}/gallery:unpublish", response_model=AgentProfileRead)
def unpublish_agent_from_gallery(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    """从广场下架（不删除员工本身）。

    下架权 = 归属人 **或** 管理员。这是管理员相对归属人的唯一额外权限 ——
    管理员可以下架别人的员工，但不能编辑它。
    """
    row = _get_agent(db, tenant_id, agent_id)
    _ensure_agent_publish_manager(row, current_user)
    if not row.is_published:
        return agent_read(row, _bindings_by_agent(db, tenant_id).get(row.id, []))
    now = utc_now()
    row.is_published = False
    row.published_at = None
    row.published_by = None
    row.updated_at = now
    db.add(row)
    db.commit()
    db.refresh(row)
    return agent_read(row, _bindings_by_agent(db, tenant_id).get(row.id, []))


@enterprise_router.delete("/{agent_id}")
def delete_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    row = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(row, current_user)
    workspace_keys = purge_agent(db, tenant_id, row)
    db.commit()
    for session_tenant_id, session_id in workspace_keys:
        remove_chat_session_workspace(tenant_id=session_tenant_id, session_id=session_id, db=db)
    return {"status": "deleted"}


def purge_agent(db: Session, tenant_id: str, row: AgentProfile) -> list[tuple[str, str]]:
    """彻底删除一个数字员工及其私有资源，返回待清理工作区。

    账号注销时也走这里（`app/api/auth.py` 的 delete_user），保证"账号被删除时，
    其创建的数字员工及资源一并删除"。
    """
    # 该员工私有资源（知识库/技能/SOP/工具）一并清理 —— 它们只属于这个员工。
    purge_agent_owned_resources(db, tenant_id, row.id)
    # 该员工对其他资源的引用行。
    references = db.exec(
        select(AgentResourceReference).where(AgentResourceReference.agent_id == row.id)
    ).all()
    for reference in references:
        db.delete(reference)
    # 渠道挂载同步清理:否则孤儿挂载行会让 /员工 列出已删除员工的裸 ID,
    # 甚至可被 /切换 路由到。默认挂载被删时把首个剩余挂载提升为默认并同步
    # binding.agent_id,保持存量绑定回退路径有效。会话指针无需处理:
    # resolve_current_agent 发现指针不在挂载集会自动重置默认。
    channel_mounts = db.exec(
        select(ChannelBindingAgent).where(ChannelBindingAgent.agent_id == row.id)
    ).all()
    affected_binding_ids = {mount.binding_id for mount in channel_mounts}
    for mount in channel_mounts:
        db.delete(mount)
    for binding_id in affected_binding_ids:
        channel_binding = db.get(ChannelBinding, binding_id)
        if not channel_binding or channel_binding.agent_id != row.id:
            continue
        remaining = db.exec(
            select(ChannelBindingAgent)
            .where(ChannelBindingAgent.binding_id == binding_id)
            .order_by(ChannelBindingAgent.sort_order, ChannelBindingAgent.created_at)
        ).first()
        if remaining:
            remaining.is_default = True
            channel_binding.agent_id = remaining.agent_id
            channel_binding.updated_at = utc_now()
            db.add(remaining)
            db.add(channel_binding)
    # 会话级联清理:员工删除后其会话若保留,新消息会由租户默认 persona 静默接管,
    # 与删除团队一致,直接清空其全部会话(消息/事件/反馈/Harness 记录与工作区)。
    sessions = db.exec(
        select(ChatSession).where(
            ChatSession.tenant_id == tenant_id, ChatSession.agent_id == row.id
        )
    ).all()
    workspace_keys = [(session.tenant_id, session.id) for session in sessions]
    for session in sessions:
        purge_chat_session_records(db, session)
    # 团队成员关系同步清理,避免花名册/TL 会话悬挂在已删除员工上。
    memberships = db.exec(select(TeamMember).where(TeamMember.agent_id == row.id)).all()
    for membership in memberships:
        db.delete(membership)
    # 定时任务立即暂停,而不是等到下次触发才失败。
    scheduled_tasks = db.exec(
        select(ScheduledTask).where(
            ScheduledTask.tenant_id == tenant_id,
            ScheduledTask.agent_id == row.id,
            ScheduledTask.status == "active",
        )
    ).all()
    for task in scheduled_tasks:
        task.status = "paused"
        task.next_run_at = None
        task.updated_at = utc_now()
        db.add(task)
    # 待处理人工转接直接取消,避免悬挂在已删员工的收件箱里。
    pending_handoffs = db.exec(
        select(HumanHandoffRequest).where(
            HumanHandoffRequest.tenant_id == tenant_id,
            HumanHandoffRequest.agent_id == row.id,
            HumanHandoffRequest.status == "pending",
        )
    ).all()
    for handoff in pending_handoffs:
        handoff.status = "cancelled"
        handoff.updated_at = utc_now()
        db.add(handoff)
    db.delete(row)
    return workspace_keys


@enterprise_router.get("/{agent_id}/resources", response_model=list[AgentResourceReferenceRead])
def get_agent_resources(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentResourceReferenceRead]:
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    rows = db.exec(
        select(AgentResourceReference)
        .where(
            AgentResourceReference.tenant_id == tenant_id, AgentResourceReference.agent_id == agent_id
        )
        .order_by(AgentResourceReference.resource_type, AgentResourceReference.created_at)
    ).all()
    return [binding_read(row) for row in rows]


@enterprise_router.put("/{agent_id}/resources", response_model=list[AgentResourceReferenceRead])
def update_agent_resources(
    agent_id: str,
    request: AgentResourcesUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentResourceReferenceRead]:
    """整体覆盖该员工引用的广场资源集合。

    只建/删引用行，**从不复制资源内容** —— 资源内容只有一份，归属其作者。
    作者更新资源，所有引用它的员工立刻生效。
    """
    agent = _get_agent(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    existing = db.exec(
        select(AgentResourceReference).where(
            AgentResourceReference.tenant_id == request.tenant_id,
            AgentResourceReference.agent_id == agent_id,
        )
    ).all()
    by_key = {(row.resource_type, row.resource_id): row for row in existing}
    desired_keys: set[tuple[str, str]] = set()
    for item in request.resources:
        key = (item.resource_type, item.resource_id)
        if key in desired_keys:
            continue
        if not reference_resource(
            db,
            request.tenant_id,
            agent_id,
            item.resource_type,
            item.resource_id,
            created_by_user_id=current_user.id,
        ):
            raise HTTPException(
                status_code=400,
                detail="Only gallery resources can be referenced by a staff",
            )
        desired_keys.add(key)
    # 不在目标集合里的引用行 → 取消引用（删行）。
    for key, row in by_key.items():
        if key not in desired_keys:
            db.delete(row)
    db.commit()
    return get_agent_resources(agent_id, request.tenant_id, db, current_user)


@enterprise_router.post(
    "/{agent_id}/resources:reference", response_model=AgentResourceReferenceRead
)
def reference_agent_resource(
    agent_id: str,
    request: AgentResourceReferenceInput,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentResourceReferenceRead:
    """启用一个广场资源到该员工（= 建立引用）。"""
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if not reference_resource(
        db,
        tenant_id,
        agent_id,
        request.resource_type,
        request.resource_id,
        created_by_user_id=current_user.id,
    ):
        raise HTTPException(
            status_code=400, detail="Only gallery resources can be referenced by a staff"
        )
    db.commit()
    row = db.exec(
        select(AgentResourceReference).where(
            AgentResourceReference.tenant_id == tenant_id,
            AgentResourceReference.agent_id == agent_id,
            AgentResourceReference.resource_type == request.resource_type,
            AgentResourceReference.resource_id == request.resource_id,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=500, detail="Reference was not persisted")
    return binding_read(row)


@enterprise_router.delete("/{agent_id}/resources/{resource_type}/{resource_id}")
def unreference_agent_resource(
    agent_id: str,
    resource_type: str,
    resource_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """取消引用（= 停用该广场资源）。

    只删引用行，**不删除资源本身** —— 资源仍属于作者，其他员工的引用不受影响。
    """
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    removed = unreference_resource(db, tenant_id, agent_id, resource_type, resource_id)
    db.commit()
    remaining = count_resource_references(db, tenant_id, resource_type, resource_id)
    return {
        "status": "unreferenced" if removed else "not_referenced",
        "resource_type": resource_type,
        "resource_id": resource_id,
        "remaining_reference_count": remaining,
    }


@enterprise_router.get("/{agent_id}/skills")
def get_agent_skills(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    return [
        _skill_read(skill)
        for skill in visible_skill_rows(db, tenant_id, agent_id, include_inactive=True)
    ]


@enterprise_router.put("/{agent_id}/models")
def update_agent_models(
    agent_id: str,
    request: AgentModelsUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    _ensure_can_manage_agent(_get_agent(db, request.tenant_id, agent_id), current_user)
    # Employee-specific model selection has been retired. Keep this endpoint as a backwards-
    # compatible reset operation so older clients cannot recreate legacy bindings.
    _delete_agent_model_bindings(db, request.tenant_id, agent_id)
    db.commit()
    return {"status": "updated", "agent_id": agent_id}


@enterprise_router.get("/{agent_id}/models", response_model=list[dict[str, object]])
def get_agent_models(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_access_agent(agent, current_user)
    default = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    if default is None:
        return []
    return [{"role": "default", "model_config_id": default.id, "effective": False}]


@chat_router.get("", response_model=list[AgentProfileRead])
def list_chat_agents(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AgentProfileRead]:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.status == "active",
        )
        .order_by(AgentProfile.updated_at.desc())
    ).all()
    rows = [row for row in rows if not _agent_hidden_from_staffdeck(row)]
    used_agent_ids = _used_agent_ids_for_user(db, tenant_id, current_user)
    rows = [
        row for row in rows if _chat_agent_selectable_to_user(row, current_user, used_agent_ids)
    ]
    bindings = _bindings_by_agent(db, tenant_id)
    return [agent_read(row, bindings.get(row.id, []), row.id in used_agent_ids) for row in rows]


@chat_router.post("/{agent_id}/use", response_model=AgentProfileRead)
def use_chat_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AgentProfileRead:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    row = _get_agent(db, tenant_id, agent_id)
    if (
        row.status != "active"
        or not _chat_agent_visible_to_user(row, current_user)
    ):
        raise HTTPException(status_code=403, detail="Cannot access this agent")
    _mark_agent_used(db, tenant_id, current_user, row.id)
    bindings = _bindings_by_agent(db, tenant_id)
    return agent_read(row, bindings.get(row.id, []), True)


def _agent_resource_timeline_events(
    db: Session,
    tenant_id: str,
    agent_id: str,
) -> list[AgentWorkRecordEventRead]:
    # 引用行的存在即状态：取消引用会直接删除该行，不再有 status 字段可过滤。
    bindings = db.exec(
        select(AgentResourceReference).where(
            AgentResourceReference.tenant_id == tenant_id,
            AgentResourceReference.agent_id == agent_id,
        )
    ).all()
    ids_by_type = {
        resource_type: {
            binding.resource_id
            for binding in bindings
            if binding.resource_type == resource_type
        }
        for resource_type in ("skill", "general_skill", "knowledge_base", "tool")
    }
    skills = {
        row.id: row
        for row in db.exec(
            select(Skill).where(
                Skill.tenant_id == tenant_id,
                Skill.id.in_(ids_by_type["skill"]),
            )
        ).all()
    } if ids_by_type["skill"] else {}
    general_skills = {
        row.id: row
        for row in db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == tenant_id,
                GeneralSkill.id.in_(ids_by_type["general_skill"]),
            )
        ).all()
    } if ids_by_type["general_skill"] else {}
    knowledge_bases = {
        row.id: row
        for row in db.exec(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.id.in_(ids_by_type["knowledge_base"]),
            )
        ).all()
    } if ids_by_type["knowledge_base"] else {}
    tools = {
        row.id: row
        for row in db.exec(
            select(Tool).where(
                Tool.tenant_id == tenant_id,
                Tool.id.in_(ids_by_type["tool"]),
            )
        ).all()
    } if ids_by_type["tool"] else {}

    events: list[AgentWorkRecordEventRead] = []
    for binding in bindings:
        kind: str
        label: str
        if binding.resource_type == "skill":
            resource = skills.get(binding.resource_id)
            if not resource or resource.status != "published":
                continue
            kind, label = "sop", resource.name
        elif binding.resource_type == "general_skill":
            resource = general_skills.get(binding.resource_id)
            if not resource or resource.status != "published":
                continue
            kind, label = "skill", resource.name
        elif binding.resource_type == "knowledge_base":
            resource = knowledge_bases.get(binding.resource_id)
            if not resource or resource.status != "active":
                continue
            kind, label = "knowledge", resource.name
        elif binding.resource_type == "tool":
            resource = tools.get(binding.resource_id)
            if not resource or not resource.enabled:
                continue
            kind, label = "tool", resource.display_name or resource.name
        else:
            continue
        events.append(
            AgentWorkRecordEventRead(
                id=f"{binding.id}:assigned",
                kind=kind,  # type: ignore[arg-type]
                phase="assigned",
                timestamp=_iso_utc(binding.created_at),
                label=label,
            )
        )
    return events


def _agent_scheduled_task_timeline_events(
    db: Session,
    tenant_id: str,
    agent_id: str,
    current_user: User,
) -> list[AgentWorkRecordEventRead]:
    conditions = [
        ScheduledTask.tenant_id == tenant_id,
        ScheduledTask.agent_id == agent_id,
        ScheduledTask.status != "archived",
    ]
    if not _is_admin_user(current_user):
        conditions.append(ScheduledTask.created_by_user_id == current_user.id)
    tasks = db.exec(select(ScheduledTask).where(*conditions)).all()
    events: list[AgentWorkRecordEventRead] = []
    for task in tasks:
        for phase, timestamp in (("last_run", task.last_run_at), ("next_run", task.next_run_at)):
            if not timestamp:
                continue
            events.append(
                AgentWorkRecordEventRead(
                    id=f"{task.id}:{phase}",
                    kind="task",
                    phase=phase,  # type: ignore[arg-type]
                    timestamp=_iso_utc(timestamp),
                    label=task.title,
                )
            )
    return events


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def agent_read(
    row: AgentProfile,
    bindings: list[AgentResourceReference],
    used_by_current_user: bool | None = None,
) -> AgentProfileRead:
    metadata = dict(row.metadata_json or {})
    if used_by_current_user is not None:
        metadata["used_by_current_user"] = used_by_current_user
        metadata["chat_used_by_current_user"] = used_by_current_user
    return AgentProfileRead(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id or "",
        owner_display_name=str(
            metadata.get("owner_display_name") or metadata.get("owner_username") or ""
        ),
        name=row.name,
        description=row.description,
        persona_prompt=row.persona_prompt,
        is_published=bool(row.is_published),
        published_at=row.published_at.isoformat() if row.published_at else None,
        published_by=row.published_by,
        status=row.status,
        harness_max_actions=max(1, min(int(row.harness_max_actions or 32), 100)),
        metadata=metadata,
        resources=[binding_read(binding) for binding in bindings],
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _is_database_locked_error(exc: OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _ensure_request_tenant(tenant_id: str, user: User) -> None:
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def _ensure_staffdeck_agent_api_client(
    db: Session,
    tenant_id: str,
    current_user: User,
) -> APIClient:
    row = db.exec(
        select(APIClient).where(
            APIClient.tenant_id == tenant_id,
            APIClient.name == STAFFDECK_AGENT_API_CLIENT_NAME,
        )
    ).first()
    required_scopes = sorted({"credentials:write", *AGENT_KEY_ALLOWED_SCOPES})
    if row:
        if row.status != "active" or not set(required_scopes).issubset(row.scopes_json or []):
            row.status = "active"
            row.scopes_json = sorted(set(row.scopes_json or []) | set(required_scopes))
            row.updated_at = utc_now()
            db.add(row)
            db.flush()
        return row
    row = APIClient(
        tenant_id=tenant_id,
        name=STAFFDECK_AGENT_API_CLIENT_NAME,
        description="由数字员工设置页管理的单员工运行密钥。",
        scopes_json=required_scopes,
        created_by_user_id=current_user.id,
        metadata_json={"managed_by": "agent_settings"},
    )
    db.add(row)
    db.flush()
    return row


def _get_agent_api_credential(
    db: Session,
    tenant_id: str,
    agent_id: str,
    credential_id: str,
) -> APICredential:
    row = db.get(APICredential, credential_id)
    if not row or row.tenant_id != tenant_id or row.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Employee API credential not found")
    return row


def _agent_api_credential_read(row: APICredential) -> AgentAPICredentialRead:
    return AgentAPICredentialRead(
        id=row.id,
        agent_id=str(row.agent_id or ""),
        name=row.name,
        access=agent_access_for_scopes(list(row.scopes_json or [])),
        key_prefix=f"{row.key_prefix}…",
        scopes=list(row.scopes_json or []),
        status=row.status,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


def _agent_usable_by_user(row: AgentProfile, user: User) -> bool:
    """只读可见性：自己的 ∪ 已发布到广场的。"""
    if row.is_published:
        return True
    return _agent_owned_by_user(row, user)


def _agent_visible_to_user(row: AgentProfile, user: User) -> bool:
    if _agent_hidden_from_staffdeck(row):
        return False
    if _is_admin_user(user):
        # 管理员能"看到"全部员工，是为了行使下架权 —— 但看不到不等于能改。
        return True
    return _agent_usable_by_user(row, user)


def _agent_hidden_from_staffdeck(row: AgentProfile) -> bool:
    return (row.metadata_json or {}).get("hidden_from_staffdeck") is True


def _agent_published_to_gallery(row: AgentProfile) -> bool:
    return bool(row.is_published)


def _used_agent_ids_for_user(db: Session, tenant_id: str, user: User) -> set[str]:
    usage_rows = db.exec(
        select(AgentUsage.agent_id).where(
            AgentUsage.tenant_id == tenant_id,
            AgentUsage.user_id == user.id,
            AgentUsage.agent_id != None,  # noqa: E711
        )
    ).all()
    session_rows = db.exec(
        select(ChatSession.agent_id).where(
            ChatSession.tenant_id == tenant_id,
            ChatSession.user_id == user.id,
            ChatSession.agent_id != None,  # noqa: E711
        )
    ).all()
    return {str(agent_id) for agent_id in [*usage_rows, *session_rows] if agent_id}


def _mark_agent_used(db: Session, tenant_id: str, user: User, agent_id: str) -> AgentUsage:
    row = db.exec(
        select(AgentUsage).where(
            AgentUsage.tenant_id == tenant_id,
            AgentUsage.user_id == user.id,
            AgentUsage.agent_id == agent_id,
        )
    ).first()
    if row:
        row.updated_at = utc_now()
    else:
        row = AgentUsage(tenant_id=tenant_id, user_id=user.id, agent_id=agent_id)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.exec(
            select(AgentUsage).where(
                AgentUsage.tenant_id == tenant_id,
                AgentUsage.user_id == user.id,
                AgentUsage.agent_id == agent_id,
            )
        ).first()
        if not row:
            raise
    db.refresh(row)
    return row


def _chat_agent_selectable_to_user(row: AgentProfile, user: User, used_agent_ids: set[str]) -> bool:
    if _agent_owned_by_user(row, user):
        return True
    # 广场员工：用过一次才留在自己的可选用列表里。管理员也不再无条件放行 ——
    # 管理员的额外权限只有"下架"，不是"替别人选用员工"。
    if row.is_published:
        return row.id in used_agent_ids
    return False


def _ensure_can_access_agent(row: AgentProfile, user: User) -> None:
    _ensure_request_tenant(row.tenant_id, user)
    if not _agent_visible_to_user(row, user):
        raise HTTPException(status_code=403, detail="Cannot access this agent")


def _ensure_can_manage_agent(row: AgentProfile, user: User) -> None:
    """数字员工的写权限：**仅归属人**。

    管理员没有分支 —— 需求要求"管理员也不能编辑别人的员工"，管理员相对归属人
    只多"下架"一项（见 `publish_agent_to_gallery` / `unpublish_agent_from_gallery`）。
    """
    _ensure_request_tenant(row.tenant_id, user)
    if _agent_owned_by_user(row, user):
        return
    raise HTTPException(status_code=403, detail="Only the owner can manage this staff")


def _ensure_admin_user(tenant_id: str, user: User) -> None:
    _ensure_request_tenant(tenant_id, user)
    if not _is_admin_user(user):
        raise HTTPException(
            status_code=403, detail="Only administrator can update the open gallery"
        )


def _metadata_with_creator(metadata: dict[str, object], user: User) -> dict[str, object]:
    normalized = dict(metadata or {})
    display_name = user.display_name or user.username
    normalized["owner_user_id"] = user.id
    normalized["owner_username"] = user.username
    normalized["owner_display_name"] = display_name
    normalized["created_by_user_id"] = user.id
    normalized["created_by_username"] = user.username
    normalized["created_by"] = user.username
    normalized["created_by_display_name"] = display_name
    normalized["creator_name"] = user.username
    return normalized


def _metadata_preserving_creator(
    existing_metadata: dict[str, object],
    next_metadata: dict[str, object],
    user: User,
) -> dict[str, object]:
    normalized = dict(next_metadata or {})
    for key in (
        "owner_user_id",
        "owner_username",
        "owner_display_name",
        "created_by_user_id",
        "created_by_username",
        "created_by",
        "created_by_display_name",
        "creator_name",
    ):
        existing_value = existing_metadata.get(key)
        if isinstance(existing_value, str) and existing_value.strip():
            normalized[key] = existing_value
    return normalized


def _chat_agent_visible_to_user(row: AgentProfile, user: User) -> bool:
    return _agent_visible_to_user(row, user)


def binding_read(row: AgentResourceReference) -> AgentResourceReferenceRead:
    return AgentResourceReferenceRead(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        resource_type=row.resource_type,  # type: ignore[arg-type]
        resource_id=row.resource_id,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _delete_agent_model_bindings(db: Session, tenant_id: str, agent_id: str) -> None:
    bindings = db.exec(
        select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == tenant_id,
            AgentModelBinding.agent_id == agent_id,
        )
    ).all()
    for binding in bindings:
        db.delete(binding)


def _get_agent(db: Session, tenant_id: str, agent_id: str) -> AgentProfile:
    ensure_tenant(db, tenant_id)
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return row


def _bindings_by_agent(db: Session, tenant_id: str) -> dict[str, list[AgentResourceReference]]:
    rows = db.exec(
        select(AgentResourceReference)
        .where(AgentResourceReference.tenant_id == tenant_id)
        .order_by(AgentResourceReference.created_at.asc())
    ).all()
    agent_ids = {row.agent_id for row in rows}
    agents_by_id = {
        row.id: row
        for row in db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.id.in_(agent_ids) if agent_ids else AgentProfile.id == "__none__",
            )
        ).all()
    }
    grouped: dict[str, list[AgentResourceReference]] = {}
    for row in rows:
        if not _resource_binding_visible_in_agent_summary(
            db, tenant_id, agents_by_id.get(row.agent_id), row
        ):
            continue
        grouped.setdefault(row.agent_id, []).append(row)
    return grouped


def _resource_binding_visible_in_agent_summary(
    db: Session,
    tenant_id: str,
    agent: AgentProfile | None,
    binding: AgentResourceReference,
) -> bool:
    """引用行是否应在员工摘要里展示。

    引用只指向广场资源；资源下架（status='hidden'）时对使用者不可见，
    但引用行保留 —— 资源恢复后引用自动生效。
    """
    if not agent:
        return False
    model = resource_model_for_type(binding.resource_type)
    if model is None:
        return False
    resource = db.get(model, binding.resource_id)
    if not resource or getattr(resource, "tenant_id", None) != tenant_id:
        return False
    if isinstance(resource, KnowledgeBase) and _is_empty_default_knowledge_base(
        db, tenant_id, resource
    ):
        return False
    return is_open_gallery_resource(db, tenant_id, binding.resource_type, resource)


def _is_empty_default_knowledge_base(db: Session, tenant_id: str, kb: KnowledgeBase) -> bool:
    metadata = kb.metadata_json or {}
    has_runtime_rows = any(
        db.exec(
            select(model.id).where(
                model.tenant_id == tenant_id,
                model.knowledge_base_id == kb.id,
            )
        ).first()
        for model in (KnowledgeDocument, KnowledgeBucket, KnowledgeChunk)
    )
    if has_runtime_rows:
        return False
    if metadata.get("created_from_document_upload") and not metadata.get("source_document_id"):
        return True
    return kb.name == "默认知识库"


def _get_global_skill(db: Session, tenant_id: str, skill_id: str) -> Skill:
    row = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return row


def _skill_read(skill: Skill) -> dict[str, object]:
    """员工可见的技能/SOP。

    技能内容只有一份，不存在"分支副本" —— 作者改动对使用者立即可见。
    """
    return {
        "id": skill.id,
        "tenant_id": skill.tenant_id,
        "skill_id": skill.skill_id,
        "version": skill.version,
        "name": skill.name,
        "business_domain": skill.business_domain,
        "description": skill.description,
        "content": skill.content_json,
        "status": skill.status,
        "scope": getattr(skill, "scope", None),
        "owner_agent_id": getattr(skill, "owner_agent_id", None),
        "created_at": skill.created_at.isoformat(),
        "updated_at": skill.updated_at.isoformat(),
    }
