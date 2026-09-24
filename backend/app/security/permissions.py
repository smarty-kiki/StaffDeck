"""权限判定。

改造后的权限模型只有三条规则：

1. **归属人**：资源的 `owner_user_id` / `owner_agent_id` 指向你 —— 可读写。
2. **广场**：`scope='gallery'` 的资源（数字员工/知识库/技能/SOP/工具）
   仅管理员可写，全员可引用。
3. **下架**：把别人的数字员工从广场下架 —— 归属人或管理员，**这是管理员
   相对归属人的唯一额外权限**。管理员不能编辑别人的员工及其资源。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query
from sqlmodel import Session

from app.db import get_session
from app.db.models import GALLERY_SCOPE, AgentProfile, User
from app.security.auth import ensure_current_user_tenant, get_current_user

ADMIN_ROLE = "admin"
MEMBER_ROLE = "member"
USER_ROLES = {ADMIN_ROLE, MEMBER_ROLE}


def is_admin_user(current_user: User) -> bool:
    return current_user.role == ADMIN_ROLE


def ensure_tenant_admin(tenant_id: str, current_user: User) -> User:
    ensure_current_user_tenant(tenant_id, current_user)
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only administrator can manage tenant settings")
    return current_user


def require_tenant_admin(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
) -> User:
    return ensure_tenant_admin(tenant_id, current_user)


def agent_owned_by_user(row: AgentProfile, user: User) -> bool:
    """数字员工归属判定。归属落在 `owner_user_id` 列（不再是 metadata）。"""
    return bool(row.owner_user_id) and row.owner_user_id == user.id


def agent_is_usable_by_user(row: AgentProfile, user: User) -> bool:
    """只读可见性：自己的、已发布到广场的。"""
    if row.is_published:
        return True
    return agent_owned_by_user(row, user)


def require_agent_scope_viewer(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> User:
    """读权限。管理员不在此处获得额外放行 —— 广场资源已对全员可见，
    私人员工只有归属人可读。"""
    ensure_current_user_tenant(tenant_id, current_user)
    if not agent_id:
        return current_user
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent_is_usable_by_user(row, current_user):
        return current_user
    raise HTTPException(status_code=403, detail="Cannot access this staff")


def ensure_open_gallery_admin(tenant_id: str, current_user: User) -> None:
    """广场共享资源（scope='gallery'）的写权限：仅管理员。"""
    ensure_tenant_admin(tenant_id, current_user)


def ensure_agent_scope_manager(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    current_user: User,
) -> AgentProfile | None:
    """数字员工的写权限：**仅归属人**。

    这里刻意没有 admin 分支 —— 需求明确要求"管理员也不能编辑"。
    管理员需要的额外能力（下架别人的员工）由 `ensure_agent_publish_manager`
    单独授予。
    """
    ensure_current_user_tenant(tenant_id, current_user)
    if not agent_id:
        return None
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent_owned_by_user(row, current_user):
        return row
    raise HTTPException(status_code=403, detail="Only the owner can manage this staff")


def ensure_resource_writer(
    db: Session,
    tenant_id: str,
    current_user: User,
    resource: object,
) -> AgentProfile | None:
    """资源写权限：**看资源自身的归属**，与调用方带了哪个 `agent_id` 无关。

    这条判定必须建立在资源上，不能建立在「本次请求是不是以某个私人员工身份发起」上：
    后者会让任何人带上自己的 `agent_id` 就绕开广场管理员校验、直接改写广场共享资源。
    资源的 `scope` 才是归属的事实。
    """
    ensure_current_user_tenant(tenant_id, current_user)
    if getattr(resource, "scope", None) == GALLERY_SCOPE:
        ensure_open_gallery_admin(tenant_id, current_user)
        return None
    owner_agent_id = getattr(resource, "owner_agent_id", None)
    if not owner_agent_id:
        # 既不是广场、又没有归属 —— 只可能是不该存在的脏数据，按最严口径收口。
        ensure_open_gallery_admin(tenant_id, current_user)
        return None
    return ensure_agent_scope_manager(db, tenant_id, owner_agent_id, current_user)


def ensure_agent_publish_manager(row: AgentProfile, current_user: User) -> None:
    """发布/下架权限：归属人，或管理员（管理员相对归属人只多这一项）。"""
    ensure_current_user_tenant(row.tenant_id, current_user)
    if agent_owned_by_user(row, current_user) or is_admin_user(current_user):
        return
    raise HTTPException(
        status_code=403, detail="Only the owner or administrator can publish this staff"
    )
