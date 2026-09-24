from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import (
    model_for_agent,
)
from app.agents.schema import (
    AgentModelBindingInput,
    AgentModelsUpdateRequest,
    AgentProfileCreateRequest,
    AgentProfileUpdateRequest,
    AgentResourceReferenceInput,
    AgentResourcesUpdateRequest,
)
from app.api.agents import (
    create_agent,
    delete_agent,
    get_agent_models,
    list_agents,
    list_chat_agents,
    reference_agent_resource,
    unreference_agent_resource,
    unpublish_agent_from_gallery,
    update_agent,
    update_agent_models,
    update_agent_resources,
    use_chat_agent,
)
from app.api.general_skills import import_general_skill
from app.api.tools import create_tool, update_tool
from app.db.models import (
    AGENT_SCOPE,
    GALLERY_SCOPE,
    AgentModelBinding,
    AgentProfile,
    AgentResourceReference,
    AgentUsage,
    ChatSession,
    GeneralSkill,
    ModelConfig,
    Tenant,
    Tool,
    User,
)
from app.db.database import _purge_legacy_agent_model_bindings
from app.general_skills.schema import GeneralSkillImportRequest
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_tenant_admin,
    require_agent_scope_viewer,
)
from app.tools.tool_schema import ToolCreateRequest, ToolUpdateRequest


def test_only_owner_can_update_and_delete_agent() -> None:
    """员工写权限只有归属人 —— **管理员也没有分支**（管理员只多"下架"一项）。"""
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        agent = AgentProfile(
            owner_user_id=owner.id,
            id="agent_owned",
            tenant_id="tenant_demo",
            name="研发员工",
            metadata_json={"owner_username": owner.username},
        )
        db.add(agent)
        db.commit()

        with pytest.raises(HTTPException) as update_error:
            update_agent(
                agent.id,
                AgentProfileUpdateRequest(tenant_id="tenant_demo", name="非法修改"),
                db=db,
                current_user=other,
            )
        assert update_error.value.status_code == 403

        updated = update_agent(
            agent.id,
            AgentProfileUpdateRequest(tenant_id="tenant_demo", name="Owner 修改"),
            db=db,
            current_user=owner,
        )
        assert updated.name == "Owner 修改"

        # 管理员不能编辑别人的员工 —— 这是本轮需求的硬约束。
        with pytest.raises(HTTPException) as admin_error:
            update_agent(
                agent.id,
                AgentProfileUpdateRequest(tenant_id="tenant_demo", name="Admin 修改"),
                db=db,
                current_user=admin,
            )
        assert admin_error.value.status_code == 403

        with pytest.raises(HTTPException) as delete_error:
            delete_agent(agent.id, tenant_id="tenant_demo", db=db, current_user=other)
        assert delete_error.value.status_code == 403

        with pytest.raises(HTTPException) as admin_delete_error:
            delete_agent(agent.id, tenant_id="tenant_demo", db=db, current_user=admin)
        assert admin_delete_error.value.status_code == 403


def test_agents_always_inherit_tenant_default_model_and_legacy_bindings_are_removed() -> None:
    with _test_session() as db:
        owner, _other, _admin = _seed_users(db)
        agent = AgentProfile(
            owner_user_id=owner.id,
            id="agent_default_model",
            tenant_id="tenant_demo",
            name="默认模型员工",
            metadata_json={"owner_username": owner.username},
        )
        tenant_default = ModelConfig(
            id="model_tenant_default",
            tenant_id="tenant_demo",
            name="租户默认模型",
            api_key_encrypted="encrypted-default",
            model="default-model",
            is_default=True,
            enabled=True,
        )
        legacy_model = ModelConfig(
            id="model_legacy_binding",
            tenant_id="tenant_demo",
            name="旧员工绑定模型",
            api_key_encrypted="encrypted-legacy",
            model="legacy-model",
            enabled=True,
        )
        legacy_binding = AgentModelBinding(
            id="binding_legacy_model",
            tenant_id="tenant_demo",
            agent_id=agent.id,
            role="default",
            model_config_id=legacy_model.id,
        )
        db.add(agent)
        db.add(tenant_default)
        db.add(legacy_model)
        db.add(legacy_binding)
        db.commit()

        resolved = model_for_agent(db, "tenant_demo", agent.id)
        assert resolved is not None
        assert resolved.id == tenant_default.id

        rows = get_agent_models(
            agent.id,
            tenant_id="tenant_demo",
            db=db,
            current_user=owner,
        )
        assert rows == [
            {
                "role": "default",
                "model_config_id": tenant_default.id,
                "effective": False,
            }
        ]

        update_agent_models(
            agent.id,
            AgentModelsUpdateRequest(
                tenant_id="tenant_demo",
                bindings=[
                    AgentModelBindingInput(
                        role="default",
                        model_config_id=legacy_model.id,
                    )
                ],
            ),
            db=db,
            current_user=owner,
        )
        assert (
            db.exec(
                select(AgentModelBinding).where(
                    AgentModelBinding.tenant_id == "tenant_demo",
                    AgentModelBinding.agent_id == agent.id,
                )
            ).all()
            == []
        )


def test_startup_seed_removes_legacy_agent_model_bindings() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(
            AgentProfile(
                owner_user_id="user_owner",
                id="agent_startup_cleanup",
                tenant_id="tenant_demo",
                name="启动清理员工",
            )
        )
        db.add(
            ModelConfig(
                id="model_startup_default",
                tenant_id="tenant_demo",
                name="默认模型",
                api_key_encrypted="encrypted-default",
                model="default-model",
                is_default=True,
            )
        )
        db.add(
            AgentModelBinding(
                id="binding_startup_cleanup",
                tenant_id="tenant_demo",
                agent_id="agent_startup_cleanup",
                role="default",
                model_config_id="model_startup_default",
            )
        )
        db.commit()

    table_names = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        _purge_legacy_agent_model_bindings(conn, table_names)

    with Session(engine) as db:
        assert db.exec(select(AgentModelBinding)).all() == []


def test_resource_binding_requires_agent_manager() -> None:
    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        agent = AgentProfile(
            owner_user_id=owner.id,
            id="agent_resource_owner",
            tenant_id="tenant_demo",
            name="资源员工",
            metadata_json={"owner_username": owner.username},
        )
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather",
            display_name="天气查询",
            method="POST",
            url="/weather",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        db.add(agent)
        db.add(tool)
        db.commit()
        request = AgentResourcesUpdateRequest(
            tenant_id="tenant_demo",
            resources=[AgentResourceReferenceInput(resource_type="tool", resource_id=tool.id)],
        )

        with pytest.raises(HTTPException) as update_error:
            update_agent_resources(agent.id, request, db=db, current_user=other)
        assert update_error.value.status_code == 403

        bindings = update_agent_resources(agent.id, request, db=db, current_user=owner)
        assert [(item.resource_type, item.resource_id) for item in bindings] == [("tool", tool.id)]


def test_list_agents_filters_to_visible_agents_for_non_admin() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        db.add(
            AgentProfile(
                owner_user_id=owner.id,
                id="agent_owned",
                tenant_id="tenant_demo",
                name="我的员工",
                metadata_json={"owner_username": owner.username},
            )
        )
        db.add(
            AgentProfile(
                owner_user_id="user_owner",
                id="agent_gallery",
                tenant_id="tenant_demo",
                name="广场员工",
                is_published=True,
                metadata_json={"owner_username": other.username},
            )
        )
        db.add(
            AgentProfile(
                owner_user_id=other.id,
                id="agent_private",
                tenant_id="tenant_demo",
                name="别人私有员工",
                metadata_json={"owner_username": other.username},
            )
        )
        db.add(
            AgentProfile(
                owner_user_id=other.id,
                id="agent_created_by_owner_only",
                tenant_id="tenant_demo",
                name="创建字段命中但非本人",
                metadata_json={
                    "owner_username": other.username,
                    "created_by_user_id": owner.id,
                    "created_by_username": owner.username,
                },
            )
        )
        db.commit()

        owner_rows = list_agents("tenant_demo", db=db, current_user=owner)
        admin_rows = list_agents("tenant_demo", db=db, current_user=admin)

        # 可见性 = 自己的 ∪ 已发布到广场的：别人的私有员工一律看不见，管理员也一样
        # （管理员相对归属人只多「下架」，不是「看见别人的私有员工」）。
        assert {row.id for row in owner_rows} == {"agent_owned", "agent_gallery"}
        assert {row.id for row in admin_rows} == {
            "agent_owned",
            "agent_gallery",
            "agent_private",
            "agent_created_by_owner_only",
        }


def test_gallery_agent_is_visible_but_not_manageable_by_non_owner() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        gallery_agent = AgentProfile(
            owner_user_id=other.id,
            id="agent_gallery",
            tenant_id="tenant_demo",
            name="广场员工",
            is_published=True,
            metadata_json={"owner_username": other.username},
        )
        db.add(gallery_agent)
        db.commit()

        owner_visible_rows = list_agents("tenant_demo", db=db, current_user=owner)
        assert {row.id for row in owner_visible_rows} == {"agent_gallery"}

        for user in (owner, admin):
            with pytest.raises(HTTPException) as manage_error:
                ensure_agent_scope_manager(db, "tenant_demo", gallery_agent.id, user)
            assert manage_error.value.status_code == 403

        assert (
            ensure_agent_scope_manager(db, "tenant_demo", gallery_agent.id, other).id
            == gallery_agent.id
        )

        with pytest.raises(HTTPException) as create_error:
            create_tool(
                ToolCreateRequest(
                    tenant_id="tenant_demo",
                    name="blocked_gallery_tool",
                    display_name="不应创建",
                    url="/blocked",
                ),
                agent_id=gallery_agent.id,
                db=db,
                current_user=owner,
            )
        assert create_error.value.status_code == 403
        assert db.exec(select(Tool).where(Tool.name == "blocked_gallery_tool")).first() is None


def test_admin_can_unpublish_a_published_agent_but_not_edit_it() -> None:
    """下架是管理员相对归属人的**唯一**额外权限：下架可以，编辑不行。"""
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        gallery_agent = AgentProfile(
            owner_user_id=other.id,
            id="agent_gallery_governance",
            tenant_id="tenant_demo",
            name="待治理广场员工",
            is_published=True,
            published_by=other.username,
        )
        db.add(gallery_agent)
        db.commit()

        # 非归属人的普通成员既不能下架别人的员工。
        with pytest.raises(HTTPException) as member_error:
            unpublish_agent_from_gallery(
                gallery_agent.id,
                tenant_id="tenant_demo",
                db=db,
                current_user=owner,
            )
        assert member_error.value.status_code == 403

        # 管理员可以下架别人的员工。
        unpublished = unpublish_agent_from_gallery(
            gallery_agent.id,
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )
        assert unpublished.is_published is False
        assert db.get(AgentProfile, gallery_agent.id) is not None

        # 作者下架自己的员工同样允许。
        assert (
            unpublish_agent_from_gallery(
                gallery_agent.id,
                tenant_id="tenant_demo",
                db=db,
                current_user=other,
            ).is_published
            is False
        )
        # 下架不等于删除：作者仍然看得见自己的员工。
        assert gallery_agent.id in {
            row.id for row in list_agents("tenant_demo", db=db, current_user=other)
        }


def test_agent_ownership_uses_immutable_user_id_not_username_metadata() -> None:
    with _test_session() as db:
        owner, other, _admin = _seed_users(db)
        agent = AgentProfile(
            owner_user_id=other.id,
            id="agent_spoofed_owner_name",
            tenant_id="tenant_demo",
            name="用户名不能授权",
            metadata_json={
                "owner_username": owner.username,
            },
        )
        db.add(agent)
        db.commit()

        assert list_agents("tenant_demo", db=db, current_user=owner) == []
        with pytest.raises(HTTPException) as manage_error:
            ensure_agent_scope_manager(db, "tenant_demo", agent.id, owner)
        assert manage_error.value.status_code == 403
        assert ensure_agent_scope_manager(db, "tenant_demo", agent.id, other).id == agent.id


def test_agent_scope_viewer_allows_owned_and_gallery_but_blocks_private_agents() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        private = AgentProfile(
            owner_user_id=owner.id,
            id="agent_private_scope",
            tenant_id="tenant_demo",
            name="私有员工",
            metadata_json={"owner_username": owner.username},
        )
        gallery = AgentProfile(
            owner_user_id=owner.id,
            id="agent_gallery_scope",
            tenant_id="tenant_demo",
            name="广场员工",
            is_published=True,
            metadata_json={"owner_username": owner.username},
        )
        db.add(private)
        db.add(gallery)
        db.commit()

        assert require_agent_scope_viewer("tenant_demo", private.id, owner, db) is owner
        assert require_agent_scope_viewer("tenant_demo", gallery.id, other, db) is other
        # 读权限上管理员也不再无条件放行：别人的私人员工管理员同样读不到。
        with pytest.raises(HTTPException) as admin_private_error:
            require_agent_scope_viewer("tenant_demo", private.id, admin, db)
        assert admin_private_error.value.status_code == 403
        with pytest.raises(HTTPException) as private_error:
            require_agent_scope_viewer("tenant_demo", private.id, other, db)
        assert private_error.value.status_code == 403
        with pytest.raises(HTTPException) as tenant_error:
            require_agent_scope_viewer("another_tenant", private.id, owner, db)
        assert tenant_error.value.status_code == 403


def test_tenant_settings_require_an_administrator() -> None:
    with _test_session() as db:
        owner, _, admin = _seed_users(db)
        assert ensure_tenant_admin("tenant_demo", admin) is admin
        with pytest.raises(HTTPException) as role_error:
            ensure_tenant_admin("tenant_demo", owner)
        assert role_error.value.status_code == 403
        with pytest.raises(HTTPException) as tenant_error:
            ensure_tenant_admin("another_tenant", admin)
        assert tenant_error.value.status_code == 403


def test_chat_agents_exclude_unused_gallery_agents_until_current_user_marks_used() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)
        owned = AgentProfile(
            owner_user_id=owner.id,
            id="agent_owned",
            tenant_id="tenant_demo",
            name="我的员工",
            metadata_json={"owner_username": owner.username},
        )
        gallery = AgentProfile(
            owner_user_id=other.id,
            id="agent_gallery",
            tenant_id="tenant_demo",
            name="广场员工",
            is_published=True,
            metadata_json={"owner_username": other.username},
        )
        private = AgentProfile(
            owner_user_id=other.id,
            id="agent_private",
            tenant_id="tenant_demo",
            name="别人私有员工",
            metadata_json={"owner_username": other.username},
        )
        db.add(owned)
        db.add(gallery)
        db.add(private)
        db.commit()

        enterprise_rows = list_agents("tenant_demo", db=db, current_user=owner)
        assert {row.id for row in enterprise_rows} == {"agent_owned", "agent_gallery"}
        assert {row.id for row in list_chat_agents("tenant_demo", current_user=owner, db=db)} == {
            "agent_owned"
        }
        # 管理员在聊天列表里不再无条件看到所有人的员工 —— 额外权限只有"下架"。
        assert list_chat_agents("tenant_demo", current_user=admin, db=db) == []

        used = use_chat_agent(gallery.id, tenant_id="tenant_demo", current_user=owner, db=db)
        assert used.id == gallery.id
        assert used.metadata["used_by_current_user"] is True
        used_again = use_chat_agent(gallery.id, tenant_id="tenant_demo", current_user=owner, db=db)
        assert used_again.id == gallery.id
        assert (
            db.exec(
                select(ChatSession).where(
                    ChatSession.user_id == owner.id, ChatSession.agent_id == gallery.id
                )
            ).first()
            is None
        )
        usage_rows = db.exec(
            select(AgentUsage).where(
                AgentUsage.user_id == owner.id, AgentUsage.agent_id == gallery.id
            )
        ).all()
        assert len(usage_rows) == 1

        chat_rows = list_chat_agents("tenant_demo", current_user=owner, db=db)
        assert {row.id for row in chat_rows} == {"agent_owned", "agent_gallery"}
        assert (
            next(row for row in chat_rows if row.id == "agent_gallery").metadata[
                "used_by_current_user"
            ]
            is True
        )


def test_create_agent_records_creator_and_keeps_name_unique_per_owner() -> None:
    with _test_session() as db:
        owner, other, admin = _seed_users(db)

        created = create_agent(
            AgentProfileCreateRequest(tenant_id="tenant_demo", name="新员工"),
            db=db,
            current_user=owner,
        )
        assert created.metadata["owner_user_id"] == owner.id
        assert created.metadata["owner_username"] == owner.username
        assert created.metadata["created_by_user_id"] == owner.id
        assert created.metadata["created_by_username"] == owner.username

        # 归属是列：即便管理员提交了伪造的 owner 字段，列的归属不变。
        with pytest.raises(HTTPException) as admin_error:
            update_agent(
                created.id,
                AgentProfileUpdateRequest(
                    tenant_id="tenant_demo",
                    metadata={
                        **created.metadata,
                        "owner_user_id": other.id,
                        "owner_username": other.username,
                        "role_name": "管理员可修改的业务字段",
                    },
                ),
                db=db,
                current_user=admin,
            )
        assert admin_error.value.status_code == 403

        owner_updated = update_agent(
            created.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo",
                metadata={**created.metadata, "role_name": "归属人可修改的业务字段"},
            ),
            db=db,
            current_user=owner,
        )
        assert owner_updated.metadata["owner_user_id"] == owner.id
        assert owner_updated.metadata["created_by_user_id"] == owner.id
        assert owner_updated.metadata["role_name"] == "归属人可修改的业务字段"

        # 同作者重名 → 409。
        with pytest.raises(HTTPException) as duplicate_error:
            create_agent(
                AgentProfileCreateRequest(tenant_id="tenant_demo", name="新员工"),
                db=db,
                current_user=owner,
            )
        assert duplicate_error.value.status_code == 409

        # 不同作者可以建同名员工 —— 广场里靠 @创建人 消歧。
        other_same_name = create_agent(
            AgentProfileCreateRequest(tenant_id="tenant_demo", name="新员工"),
            db=db,
            current_user=other,
        )
        assert other_same_name.name == "新员工"
        assert other_same_name.owner_user_id == other.id
        assert other_same_name.id != created.id


def test_private_tool_is_separate_from_gallery_tool_and_gallery_is_admin_only() -> None:
    """私有工具与广场工具是两行，互不覆盖；广场工具只有管理员能改。"""
    with _test_session() as db:
        owner, _other, admin = _seed_users(db)
        agent = AgentProfile(
            owner_user_id=owner.id,
            id="agent_owned",
            tenant_id="tenant_demo",
            name="研发员工",
            metadata_json={"owner_username": owner.username},
        )
        gallery_tool = Tool(
            id="tool_open_weather",
            tenant_id="tenant_demo",
            name="weather",
            display_name="天气",
            method="POST",
            url="/api/weather",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        db.add(agent)
        db.add(gallery_tool)
        db.commit()

        # 归属人在自己的员工下建同名私有工具 —— 不同平面，允许同名。
        private_tool = create_tool(
            ToolCreateRequest(
                tenant_id="tenant_demo",
                name="weather",
                display_name="员工天气",
                description="员工私有配置",
                url="/api/private-weather",
            ),
            agent_id=agent.id,
            db=db,
            current_user=owner,
        )
        assert private_tool.id != gallery_tool.id
        private_row = db.get(Tool, private_tool.id)
        assert private_row is not None
        assert private_row.scope == AGENT_SCOPE
        assert private_row.owner_agent_id == agent.id

        # 改私有工具不影响广场工具。
        update_tool(
            private_tool.id,
            ToolUpdateRequest(
                tenant_id="tenant_demo",
                name="weather",
                display_name="员工天气 v2",
                description=private_tool.description,
                url=private_tool.url,
            ),
            agent_id=agent.id,
            db=db,
            current_user=owner,
        )
        db.refresh(gallery_tool)
        assert gallery_tool.display_name == "天气"
        assert gallery_tool.url == "/api/weather"

        # 成员不能改广场工具。
        with pytest.raises(HTTPException) as member_error:
            update_tool(
                gallery_tool.id,
                ToolUpdateRequest(
                    tenant_id="tenant_demo",
                    name="weather",
                    display_name="员工天气",
                    url="/api/weather",
                ),
                agent_id=None,
                db=db,
                current_user=owner,
            )
        assert member_error.value.status_code == 403

        # 管理员改广场工具，私有工具不受影响。
        update_tool(
            gallery_tool.id,
            ToolUpdateRequest(
                tenant_id="tenant_demo",
                name="weather",
                display_name="广场天气",
                url="/api/weather",
            ),
            agent_id=None,
            db=db,
            current_user=admin,
        )
        private_row = db.get(Tool, private_tool.id)
        assert private_row is not None
        assert private_row.display_name == "员工天气 v2"


def test_tool_name_cannot_be_modified_after_create() -> None:
    with _test_session() as db:
        _owner, _other, admin = _seed_users(db)
        tool = Tool(
            id="tool_weather",
            tenant_id="tenant_demo",
            name="weather",
            display_name="天气",
            method="POST",
            url="/api/weather",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        db.add(tool)
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            update_tool(
                tool.id,
                ToolUpdateRequest(
                    tenant_id="tenant_demo",
                    name="weather_v2",
                    display_name="天气新版",
                    url="/api/weather-v2",
                ),
                agent_id=None,
                db=db,
                current_user=admin,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Tool name cannot be modified"


def test_private_general_skill_edit_does_not_mutate_gallery_skill() -> None:
    """私有技能与广场技能是独立两行：改私有不动广场，改广场需管理员。"""
    with _test_session() as db:
        owner, _other, admin = _seed_users(db)
        agent = AgentProfile(
            owner_user_id=owner.id,
            id="agent_owned",
            tenant_id="tenant_demo",
            name="研发员工",
            metadata_json={"owner_username": owner.username},
        )
        gallery_skill = GeneralSkill(
            id="genskill_open_weather",
            tenant_id="tenant_demo",
            slug="weather",
            name="天气技能",
            description="开放广场版本",
            skill_markdown="# 天气技能\n",
            status="published",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        db.add(agent)
        db.add(gallery_skill)
        db.commit()

        private_skill = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id=agent.id,
                slug="weather",
                name="员工天气技能",
                description="员工私有版本",
                markdown="# 员工天气技能\n",
            ),
            db=db,
            current_user=owner,
        )

        db.refresh(gallery_skill)
        assert private_skill.id != gallery_skill.id
        private_row = db.get(GeneralSkill, private_skill.id)
        assert private_row is not None
        assert private_row.scope == AGENT_SCOPE
        assert private_row.owner_agent_id == agent.id
        assert gallery_skill.name == "天气技能"
        assert gallery_skill.description == "开放广场版本"

        # 归属人改写自己的私有技能，广场技能不变。
        updated = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id=agent.id,
                original_slug="weather",
                slug="weather",
                name="员工天气技能 v2",
                description="员工私有版本 v2",
                markdown="# 员工天气技能 v2\n",
            ),
            db=db,
            current_user=owner,
        )
        db.refresh(gallery_skill)
        assert updated.id == private_skill.id
        assert updated.name == "员工天气技能 v2"
        assert gallery_skill.name == "天气技能"

        with pytest.raises(HTTPException) as admin_only_error:
            import_general_skill(
                GeneralSkillImportRequest(
                    tenant_id="tenant_demo",
                    slug="weather",
                    name="成员改广场技能",
                    markdown="# 成员改广场技能\n",
                ),
                db=db,
                current_user=owner,
            )
        assert admin_only_error.value.status_code == 403


def test_create_agent_never_copies_resources_from_another_agent() -> None:
    """「以某员工为模板复制一份」这个能力整体消失：新建员工从空白开始。"""
    with _test_session() as db:
        _owner, _other, admin = _seed_users(db)
        source = AgentProfile(
            owner_user_id=admin.id,
            id="agent_template_source",
            tenant_id="tenant_demo",
            name="源员工",
        )
        skill = GeneralSkill(
            id="genskill_source_private",
            tenant_id="tenant_demo",
            slug="source-private",
            name="源员工私有技能",
            skill_markdown="# 源员工私有技能\n",
            status="published",
            scope=AGENT_SCOPE,
            owner_agent_id=source.id,
        )
        db.add(source)
        db.add(skill)
        db.commit()

        created = create_agent(
            AgentProfileCreateRequest(tenant_id="tenant_demo", name="全新建员工"),
            db=db,
            current_user=admin,
        )

        assert created.id != source.id
        # 新员工没有任何资源引用，也没有拿到源员工的私有资源
        assert (
            db.exec(
                select(AgentResourceReference).where(
                    AgentResourceReference.agent_id == created.id,
                )
            ).all()
            == []
        )
        # 源员工的私有资源仍然只归源员工
        assert skill.owner_agent_id == source.id


def test_referencing_gallery_resource_does_not_fork_content() -> None:
    """启用广场资源 = 建引用行；内容始终只有一份，归作者。"""
    with _test_session() as db:
        _owner, _other, admin = _seed_users(db)
        agent = AgentProfile(
            owner_user_id=admin.id,
            id="agent_referencing",
            tenant_id="tenant_demo",
            name="引用员工",
        )
        gallery_skill = GeneralSkill(
            id="genskill_gallery_referenced",
            tenant_id="tenant_demo",
            slug="gallery-referenced",
            name="广场技能",
            skill_markdown="# 广场技能\n",
            status="published",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        db.add(agent)
        db.add(gallery_skill)
        db.commit()

        reference_agent_resource(
            agent.id,
            AgentResourceReferenceInput(
                tenant_id="tenant_demo",
                resource_type="general_skill",
                resource_id=gallery_skill.id,
            ),
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )
        db.commit()

        references = db.exec(
            select(AgentResourceReference).where(
                AgentResourceReference.agent_id == agent.id,
            )
        ).all()
        assert [row.resource_id for row in references] == [gallery_skill.id]
        # 没有产生第二份技能内容
        assert db.exec(select(GeneralSkill)).all() == [gallery_skill]


def test_unreferencing_gallery_resource_keeps_the_resource_alive() -> None:
    """取消引用 = 删引用行；资源本身与别人的引用都不受影响。"""
    with _test_session() as db:
        _owner, _other, admin = _seed_users(db)
        first = AgentProfile(
            owner_user_id=admin.id,
            id="agent_first_reference",
            tenant_id="tenant_demo",
            name="首个引用员工",
        )
        second = AgentProfile(
            owner_user_id=admin.id,
            id="agent_second_reference",
            tenant_id="tenant_demo",
            name="第二个引用员工",
        )
        gallery_skill = GeneralSkill(
            id="genskill_unref_target",
            tenant_id="tenant_demo",
            slug="unref-target",
            name="待取消引用技能",
            skill_markdown="# 待取消引用技能\n",
            status="published",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        db.add(first)
        db.add(second)
        db.add(gallery_skill)
        db.commit()
        for agent in (first, second):
            reference_agent_resource(
                agent.id,
                AgentResourceReferenceInput(
                    tenant_id="tenant_demo",
                    resource_type="general_skill",
                    resource_id=gallery_skill.id,
                ),
                tenant_id="tenant_demo",
                db=db,
                current_user=admin,
            )
        db.commit()

        unreference_agent_resource(
            first.id,
            "general_skill",
            gallery_skill.id,
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )
        db.commit()

        assert (
            db.exec(
                select(AgentResourceReference).where(
                    AgentResourceReference.agent_id == first.id,
                )
            ).all()
            == []
        )
        assert (
            db.exec(
                select(AgentResourceReference).where(
                    AgentResourceReference.agent_id == second.id,
                )
            ).one()
            is not None
        )
        assert db.get(GeneralSkill, gallery_skill.id) is not None


def _seed_users(db: Session) -> tuple[User, User, User]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    owner = User(
        id="user_owner",
        tenant_id="tenant_demo",
        username="owner",
        display_name="Owner",
        password_hash="x",
    )
    other = User(
        id="user_other",
        tenant_id="tenant_demo",
        username="other",
        display_name="Other",
        password_hash="x",
    )
    admin = User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        display_name="Admin",
        role="admin",
        password_hash="x",
    )
    db.add(owner)
    db.add(other)
    db.add(admin)
    db.commit()
    return owner, other, admin


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
