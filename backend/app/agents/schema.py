from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

AgentResourceType = Literal["skill", "general_skill", "knowledge_base", "tool"]
AgentWorkRecordEventKind = Literal["chat", "task", "sop", "tool", "knowledge", "skill"]
AgentWorkRecordEventPhase = Literal["reply", "last_run", "next_run", "assigned"]


class AgentProfileCreateRequest(BaseModel):
    """新建数字员工。

    没有 `copy_from_agent_id` —— 员工之间不复刻资源。新员工需要什么广场资源，
    用引用端点挂上去（引用而非复制，作者更新则使用者实时可见）。
    """

    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    harness_max_actions: int = Field(default=32, ge=1, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentProfileUpdateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    status: Optional[Literal["active", "archived"]] = None
    harness_max_actions: Optional[int] = Field(default=None, ge=1, le=100)
    metadata: Optional[dict[str, Any]] = None


class AgentResourceReferenceRead(BaseModel):
    """员工对广场资源的一条引用。没有 `status` / `metadata` —— 有行即启用，删行即取消。"""

    id: str
    tenant_id: str
    agent_id: str
    resource_type: AgentResourceType
    resource_id: str
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class AgentProfileRead(BaseModel):
    id: str
    tenant_id: str
    owner_user_id: str
    owner_display_name: str = ""
    name: str
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_published: bool = False
    published_at: Optional[str] = None
    published_by: Optional[str] = None
    status: str
    harness_max_actions: int = 32
    metadata: dict[str, Any] = Field(default_factory=dict)
    resources: list[AgentResourceReferenceRead] = Field(default_factory=list)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class AgentScopeRead(BaseModel):
    tenant_id: str
    agents: list[AgentProfileRead] = Field(default_factory=list)


class AgentWorkRecordReplyStatsRead(BaseModel):
    total: int = 0
    today: int = 0
    by_day: dict[str, int] = Field(default_factory=dict)


class AgentWorkRecordEventRead(BaseModel):
    id: str
    kind: AgentWorkRecordEventKind
    phase: AgentWorkRecordEventPhase
    timestamp: str
    label: str = ""


class AgentWorkRecordRead(BaseModel):
    agent_id: str
    timezone: str
    generated_at: str
    reply_stats: AgentWorkRecordReplyStatsRead
    events: list[AgentWorkRecordEventRead] = Field(default_factory=list)


class AgentResourceReferenceInput(BaseModel):
    """引用广场资源：只需指出引用哪一条资源。

    没有 `metadata` —— 引用行的存在即状态，创建人由列派生。
    """

    resource_type: AgentResourceType
    resource_id: str


class AgentResourcesUpdateRequest(BaseModel):
    """整体覆盖该员工引用的广场资源集合。删行 = 取消引用。"""

    tenant_id: str
    resources: list[AgentResourceReferenceInput] = Field(default_factory=list)


class AgentModelBindingInput(BaseModel):
    role: Literal["default", "router", "step", "response", "general_skill"]
    model_config_id: str


class AgentModelsUpdateRequest(BaseModel):
    tenant_id: str
    bindings: list[AgentModelBindingInput] = Field(default_factory=list)


class AgentModelBindingRead(BaseModel):
    role: str
    model_config_id: str
    effective: bool = False


class AgentAPICredentialCreateRequest(BaseModel):
    tenant_id: str
    name: str = Field(min_length=1, max_length=120)
    access: Literal["runtime"] = "runtime"
    expires_at: datetime | None = None


class AgentAPICredentialRead(BaseModel):
    id: str
    agent_id: str
    name: str
    access: Literal["runtime", "full_access"]
    key_prefix: str
    scopes: list[str] = Field(default_factory=list)
    status: str
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    revoked_at: datetime | None = None


class AgentAPICredentialCreated(AgentAPICredentialRead):
    api_key: str
