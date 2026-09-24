"""资源归属 + 广场引用模型的行为测试。

本轮改造把"每个员工各自复制一份资源（分支）"整体删掉了，替换成：

- 私有资源 `scope='agent' + owner_agent_id` —— **归属即生效**，不产生引用行；
- 广场资源 `scope='gallery' + owner_agent_id IS NULL` —— 全员可**引用**；
- 员工可见集 = 自己拥有的 ∪ 已引用的广场资源；资源内容永远只有一份。

因此本文件里的"分支（branch）/ 同步状态（sync_state）/ 复制副本"断言全部作废，
改写为引用/归属语义。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import (
    count_resource_references,
    ensure_visible_name_unique,
    knowledge_version_for_upload,
    purge_agent_owned_resources,
    reference_resource,
    referenced_resource_ids,
    resource_creator_metadata,
    unreference_resource,
    visible_general_skill_rows,
    visible_knowledge_base_versions,
    visible_resource_names,
    visible_skill_rows,
)
from app.agents.schema import AgentResourceReferenceInput, AgentResourcesUpdateRequest
from app.api.agents import (
    list_agents,
    list_chat_agents,
    purge_agent,
    reference_agent_resource,
    unreference_agent_resource,
)
from app.api.general_skills import archive_general_skill
from app.api.knowledge_bases import list_knowledge_bases, update_knowledge_base
from app.api.skills import (
    archive_skill,
    create_skill,
    delete_skill,
    skill_read,
    update_skill,
)
from app.api.tools import create_tool, list_tools
from app.db.models import (
    AGENT_SCOPE,
    GALLERY_SCOPE,
    AgentProfile,
    AgentResourceReference,
    AgentUsage,
    GeneralSkill,
    KnowledgeBase,
    Skill,
    Tenant,
    Tool,
    User,
)
from app.db.seed import EXCHANGE_SKILL, REFUND_SKILL, _publish_seeded_system_resources
from app.knowledge.schema import KnowledgeBaseUpdateRequest
from app.skills.skill_schema import SkillCard, SkillCreateRequest, SkillUpdateRequest


def _admin_user() -> User:
    return User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash="test",
    )


def _member_user() -> User:
    return User(
        id="user_member",
        tenant_id="tenant_demo",
        username="member",
        role="member",
        password_hash="test",
    )


# --------------------------------------------------------------------------- #
# 员工列表可见性
# --------------------------------------------------------------------------- #


def test_management_and_chat_agent_lists_share_one_access_scope() -> None:
    with _test_session() as db:
        member = _member_user()
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(member)
        db.add(
            AgentProfile(
                owner_user_id=member.id,
                id="agent_owned",
                tenant_id="tenant_demo",
                name="本人员工",
            )
        )
        db.add(
            AgentProfile(
                owner_user_id="other",
                id="agent_gallery_unused",
                tenant_id="tenant_demo",
                name="未使用广场员工",
                is_published=True,
            )
        )
        db.add(
            AgentProfile(
                owner_user_id="other",
                id="agent_gallery_used",
                tenant_id="tenant_demo",
                name="已使用广场员工",
                is_published=True,
            )
        )
        db.add(
            AgentProfile(
                owner_user_id="other",
                id="agent_private_other",
                tenant_id="tenant_demo",
                name="他人私有员工",
            )
        )
        db.add(
            AgentUsage(
                tenant_id="tenant_demo",
                user_id=member.id,
                agent_id="agent_gallery_used",
            )
        )
        db.commit()

        management = list_agents("tenant_demo", db, member)
        chat = list_chat_agents("tenant_demo", member, db)
        management_by_id = {row.id: row for row in management}

        assert set(management_by_id) == {
            "agent_owned",
            "agent_gallery_unused",
            "agent_gallery_used",
        }
        assert {row.id for row in chat} == {"agent_gallery_used", "agent_owned"}
        assert management_by_id["agent_gallery_unused"].metadata["used_by_current_user"] is False
        assert management_by_id["agent_gallery_used"].metadata["used_by_current_user"] is True


# --------------------------------------------------------------------------- #
# seed：广场资源落 scope='gallery'，不碰用户资源
# --------------------------------------------------------------------------- #


def test_seed_publishing_marks_system_resources_as_gallery_and_leaves_user_skill_private() -> None:
    with _test_session() as db:
        default_agent = AgentProfile(
            owner_user_id="user_admin",
            id="agent_tenant_demo_default",
            tenant_id="tenant_demo",
            name="默认员工",
        )
        user_agent = AgentProfile(
            owner_user_id="user_member",
            id="agent_user_owned",
            tenant_id="tenant_demo",
            name="用户员工",
        )
        seeded_published = Skill(
            id="skill_seed_published_row",
            tenant_id="tenant_demo",
            skill_id=str(REFUND_SKILL["skill_id"]),
            version="1.0.0",
            name="系统退款流程",
            content_json=dict(REFUND_SKILL),
            status="published",
            scope=AGENT_SCOPE,
            owner_agent_id=default_agent.id,
        )
        seeded_archived = Skill(
            id="skill_seed_archived_row",
            tenant_id="tenant_demo",
            skill_id=str(EXCHANGE_SKILL["skill_id"]),
            version="1.0.0",
            name="已停用的系统流程",
            content_json=dict(EXCHANGE_SKILL),
            status="archived",
            scope=AGENT_SCOPE,
            owner_agent_id=default_agent.id,
        )
        user_skill = Skill(
            id="skill_user_row",
            tenant_id="tenant_demo",
            skill_id="user_skill",
            version="1.0.0",
            name="用户流程",
            content_json={"skill_id": "user_skill", "name": "用户流程", "nodes": []},
            status="published",
            scope=AGENT_SCOPE,
            owner_agent_id=user_agent.id,
        )
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(default_agent)
        db.add(user_agent)
        db.add(seeded_published)
        db.add(seeded_archived)
        db.add(user_skill)
        db.commit()

        _publish_seeded_system_resources(db)
        db.commit()

        for row in (seeded_published, seeded_archived):
            db.refresh(row)
            assert row.scope == GALLERY_SCOPE
            assert row.owner_agent_id is None

        db.refresh(user_skill)
        assert user_skill.scope == AGENT_SCOPE
        assert user_skill.owner_agent_id == user_agent.id

        assert default_agent.status == "archived"
        assert default_agent.metadata_json["hidden_from_staffdeck"] is True
        # 种子不再往用户资源上写任何归属痕迹。
        assert user_agent.metadata_json == {}


# --------------------------------------------------------------------------- #
# 引用 / 取消引用
# --------------------------------------------------------------------------- #


def test_referencing_gallery_skill_makes_it_visible_and_unreferencing_removes_the_row() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        gallery_skill = _gallery_skill(db, skill_id="weather", name="天气流程")

        assert visible_skill_rows(db, "tenant_demo", tenant.staff.id) == []

        assert (
            reference_resource(db, "tenant_demo", tenant.staff.id, "skill", gallery_skill.id) is True
        )
        db.commit()

        visible = visible_skill_rows(db, "tenant_demo", tenant.staff.id)
        assert [row.id for row in visible] == [gallery_skill.id]
        assert referenced_resource_ids(db, "tenant_demo", tenant.staff.id, "skill") == [
            gallery_skill.id
        ]

        # 重复引用是幂等的：不会插出第二行。
        assert (
            reference_resource(db, "tenant_demo", tenant.staff.id, "skill", gallery_skill.id) is True
        )
        db.commit()
        assert count_resource_references(db, "tenant_demo", "skill", gallery_skill.id) == 1

        assert (
            unreference_resource(db, "tenant_demo", tenant.staff.id, "skill", gallery_skill.id)
            is True
        )
        db.commit()

        assert visible_skill_rows(db, "tenant_demo", tenant.staff.id) == []
        # 取消引用 = 删行，库中不留任何残留行。
        assert db.exec(select(AgentResourceReference)).all() == []
        assert gallery_skill.id not in referenced_resource_ids(
            db, "tenant_demo", tenant.staff.id, "skill"
        )


def test_only_gallery_resources_can_be_referenced() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        private_skill = Skill(
            id="skill_private_row",
            tenant_id="tenant_demo",
            skill_id="private_sop",
            version="1.0.0",
            name="私有 SOP",
            status="published",
            content_json=_graph("私有 SOP", "1.0.0"),
            scope=AGENT_SCOPE,
            owner_agent_id=tenant.staff.id,
        )
        db.add(private_skill)
        db.commit()

        assert (
            reference_resource(db, "tenant_demo", tenant.staff.id, "skill", private_skill.id)
            is False
        )
        assert db.exec(select(AgentResourceReference)).all() == []


def test_reference_endpoint_and_bulk_update_agree_on_the_reference_set() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        skill = _gallery_skill(db, skill_id="weather", name="天气流程")
        tool = _gallery_tool(db, name="weather.tool")

        reference_agent_resource(
            tenant.staff.id,
            AgentResourceReferenceInput(resource_type="skill", resource_id=skill.id),
            tenant_id="tenant_demo",
            db=db,
            current_user=tenant.owner,
        )
        db.commit()
        assert count_resource_references(db, "tenant_demo", "skill", skill.id) == 1

        # 批量覆盖 = 期望集合；不在集合里的引用被删掉。
        update_rows = [
            AgentResourceReferenceInput(resource_type="tool", resource_id=tool.id),
            AgentResourceReferenceInput(resource_type="skill", resource_id=skill.id),
        ]
        from app.api.agents import update_agent_resources

        update_agent_resources(
            tenant.staff.id,
            AgentResourcesUpdateRequest(tenant_id="tenant_demo", resources=update_rows),
            db=db,
            current_user=tenant.owner,
        )
        assert {
            (row.resource_type, row.resource_id)
            for row in db.exec(select(AgentResourceReference)).all()
        } == {("skill", skill.id), ("tool", tool.id)}

        unreference_agent_resource(
            tenant.staff.id,
            "skill",
            skill.id,
            tenant_id="tenant_demo",
            db=db,
            current_user=tenant.owner,
        )
        assert count_resource_references(db, "tenant_demo", "skill", skill.id) == 0
        assert count_resource_references(db, "tenant_demo", "tool", tool.id) == 1


def test_deleting_gallery_skill_purges_every_reference() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        skill = _gallery_skill(db, skill_id="weather", name="天气流程")
        other_staff = _staff(db, "agent_other", "另一个员工")
        for staff in (tenant.staff, other_staff):
            reference_resource(db, "tenant_demo", staff.id, "skill", skill.id)
        db.commit()
        assert count_resource_references(db, "tenant_demo", "skill", skill.id) == 2

        assert (
            delete_skill(
                "weather",
                tenant_id="tenant_demo",
                db=db,
                agent_id=None,
                current_user=tenant.admin,
            )
            == {"status": "deleted"}
        )

        assert db.get(Skill, skill.id) is None
        # 引用行级联清空 —— 不留下指向不存在资源的悬挂引用。
        assert db.exec(select(AgentResourceReference)).all() == []
        for staff in (tenant.staff, other_staff):
            assert visible_skill_rows(db, "tenant_demo", staff.id) == []


# --------------------------------------------------------------------------- #
# 作者改 → 使用者实时看到（无副本）
# --------------------------------------------------------------------------- #


def test_author_edit_is_visible_to_referencing_staff_without_a_copy() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        skill = _gallery_skill(db, skill_id="purchase", name="购买流程")
        reference_resource(db, "tenant_demo", tenant.staff.id, "skill", skill.id)
        db.commit()

        update_skill(
            "purchase",
            SkillUpdateRequest(
                tenant_id="tenant_demo",
                content=SkillCard.model_validate(
                    _graph("购买流程 · 已更新", "1.0.1", skill_id="purchase")
                ),
                status="published",
            ),
            None,
            db,
            tenant.admin,
        )

        rows = visible_skill_rows(db, "tenant_demo", tenant.staff.id)
        assert len(rows) == 1
        # 同一个资源行 —— 使用者读到的就是作者改后的那一份。
        assert rows[0].id == skill.id
        assert rows[0].name == "购买流程 · 已更新"
        assert db.exec(select(Skill).where(Skill.skill_id == "purchase")).all().__len__() == 1


def test_knowledge_version_for_upload_returns_the_single_canonical_version() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        kb = KnowledgeBase(
            id="kb_shared",
            tenant_id="tenant_demo",
            name="业务资料",
            status="active",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        db.add(kb)
        db.commit()

        first = knowledge_version_for_upload(db, "tenant_demo", kb.id, tenant.staff.id)
        second = knowledge_version_for_upload(db, "tenant_demo", kb.id, tenant.staff.id)

        # 上传落点永远是该知识库唯一的当前版本，不再按员工克隆分支。
        assert first.id == second.id
        assert first.knowledge_base_id == kb.id


# --------------------------------------------------------------------------- #
# 跨来源唯一性（设计 4.7）
# --------------------------------------------------------------------------- #


def test_private_and_referenced_gallery_cannot_share_a_business_key() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        gallery_skill = _gallery_skill(db, skill_id="weather", name="广场天气流程")
        reference_resource(db, "tenant_demo", tenant.staff.id, "skill", gallery_skill.id)
        db.commit()

        assert visible_resource_names(db, "tenant_demo", tenant.staff.id, "skill") == {
            "weather": gallery_skill.id
        }

        # 再建一个同名私有 SOP → 显式 409，而不是悄悄地隐式改名。
        with pytest.raises(HTTPException) as exc_info:
            create_skill(
                SkillCreateRequest(
                    tenant_id="tenant_demo",
                    content=SkillCard.model_validate(
                        _graph("私有天气流程", "1.0.0", skill_id="weather")
                    ),
                    status="published",
                ),
                agent_id=tenant.staff.id,
                db=db,
                current_user=tenant.owner,
            )
        assert exc_info.value.status_code == 409

        # 另一个员工没有引用它，可以自由使用同一个 skill_id。
        other_staff = _staff(db, "agent_other", "另一个员工")
        created = create_skill(
            SkillCreateRequest(
                tenant_id="tenant_demo",
                content=SkillCard.model_validate(
                    _graph("私有天气流程", "1.0.0", skill_id="weather")
                ),
                status="published",
            ),
            agent_id=other_staff.id,
            db=db,
            current_user=tenant.owner,
        )
        assert created.skill_id == "weather"


def test_ensure_visible_name_unique_is_scoped_to_one_staff() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        other = _staff(db, "agent_other", "另一个员工")
        private_tool = Tool(
            id="tool_private_row",
            tenant_id="tenant_demo",
            name="search",
            display_name="搜索",
            method="POST",
            url="/api/search",
            scope=AGENT_SCOPE,
            owner_agent_id=tenant.staff.id,
        )
        db.add(private_tool)
        db.commit()

        # 同归属重名 → 409。
        with pytest.raises(HTTPException):
            ensure_visible_name_unique(db, "tenant_demo", tenant.staff.id, "tool", "search")
        # 不同归属 → 放行。
        ensure_visible_name_unique(db, "tenant_demo", other.id, "tool", "search")
        # exclude_id 指向自己 → 放行（就地更新场景）。
        ensure_visible_name_unique(
            db, "tenant_demo", tenant.staff.id, "tool", "search", exclude_id=private_tool.id
        )


# --------------------------------------------------------------------------- #
# 可见资源集合
# --------------------------------------------------------------------------- #


def test_staff_visible_lists_cover_owned_and_referenced_resources() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        staff_id = tenant.staff.id

        private_skill = Skill(
            id="skill_owned",
            tenant_id="tenant_demo",
            skill_id="owned_sop",
            version="1.0.0",
            name="员工自己的 SOP",
            status="published",
            content_json=_graph("员工自己的 SOP", "1.0.0"),
            scope=AGENT_SCOPE,
            owner_agent_id=staff_id,
        )
        private_kb = KnowledgeBase(
            id="kb_owned",
            tenant_id="tenant_demo",
            name="员工自己的知识库",
            status="active",
            scope=AGENT_SCOPE,
            owner_agent_id=staff_id,
        )
        db.add(private_skill)
        db.add(private_kb)
        db.flush()

        private_tool = create_tool(
            _tool_request("owned.tool", "员工自己的工具"),
            agent_id=staff_id,
            db=db,
            current_user=tenant.owner,
        )
        gallery_general = GeneralSkill(
            id="genskill_gallery_row",
            tenant_id="tenant_demo",
            slug="gallery-skill",
            name="引用的广场技能",
            skill_markdown="# 引用的广场技能\n",
            status="published",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        db.add(gallery_general)
        db.commit()

        reference_resource(db, "tenant_demo", staff_id, "general_skill", gallery_general.id)
        db.commit()

        assert [row.id for row in visible_skill_rows(db, "tenant_demo", staff_id)] == [
            private_skill.id
        ]
        assert [row.id for row in list_tools("tenant_demo", None, staff_id, db)] == [
            private_tool.id
        ]
        assert set(visible_knowledge_base_versions(db, "tenant_demo", staff_id)) == {private_kb.id}
        visible_general = {row.id for row in visible_general_skill_rows(db, "tenant_demo", staff_id)}
        assert visible_general == {gallery_general.id}

        # 员工摘要里的 resources 只列"引用的广场资源"；私有资源由各自的资源列表承载。
        summary = _agent_summary(db, staff_id)
        assert {(row.resource_type, row.resource_id) for row in summary.resources} == {
            ("general_skill", gallery_general.id)
        }


def test_agent_summary_keeps_only_non_default_knowledge_references() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        real_kb = KnowledgeBase(
            id="kb_real",
            tenant_id="tenant_demo",
            name="业务资料",
            status="active",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        default_kb = KnowledgeBase(
            id="kb_default",
            tenant_id="tenant_demo",
            name="默认知识库",
            status="active",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        upload_placeholder = KnowledgeBase(
            id="kb_upload_placeholder",
            tenant_id="tenant_demo",
            name="上传占位",
            status="active",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
            metadata_json={"created_from_document_upload": True},
        )
        db.add(real_kb)
        db.add(default_kb)
        db.add(upload_placeholder)
        db.commit()

        for kb in (real_kb, default_kb, upload_placeholder):
            assert reference_resource(db, "tenant_demo", tenant.staff.id, "knowledge_base", kb.id)
        db.commit()

        summary = _agent_summary(db, tenant.staff.id)
        assert {row.resource_id for row in summary.resources} == {real_kb.id}


def test_gallery_view_only_lists_gallery_scoped_resources() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        gallery_skill = _gallery_skill(db, skill_id="weather", name="广场天气流程")
        private_skill = Skill(
            id="skill_private_row",
            tenant_id="tenant_demo",
            skill_id="private_sop",
            version="1.0.0",
            name="私有 SOP",
            status="published",
            content_json=_graph("私有 SOP", "1.0.0"),
            scope=AGENT_SCOPE,
            owner_agent_id=tenant.staff.id,
        )
        db.add(private_skill)
        db.commit()

        # 不给 agent_id 就是广场视图，只能看到 scope='gallery' 的资源。
        assert [row.id for row in visible_skill_rows(db, "tenant_demo")] == [gallery_skill.id]
        # 给一个不存在的 agent_id 同样落到广场视图 —— 不会因此漏出任何私有资源。
        assert [
            row.id
            for row in visible_skill_rows(db, "tenant_demo", "agent_does_not_exist")
        ] == [gallery_skill.id]


# --------------------------------------------------------------------------- #
# 归档 / 下架
# --------------------------------------------------------------------------- #


def test_archiving_gallery_resources_keeps_references_but_hides_them_from_runtime() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        skill = _gallery_skill(db, skill_id="weather", name="天气流程")
        general_skill = GeneralSkill(
            id="genskill_gallery_row",
            tenant_id="tenant_demo",
            slug="weather-skill",
            name="天气技能",
            skill_markdown="# 天气技能\n",
            status="published",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        kb = KnowledgeBase(
            id="kb_gallery_row",
            tenant_id="tenant_demo",
            name="天气资料",
            status="active",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        db.add(general_skill)
        db.add(kb)
        db.commit()
        for resource_type, resource_id in (
            ("skill", skill.id),
            ("general_skill", general_skill.id),
            ("knowledge_base", kb.id),
        ):
            reference_resource(db, "tenant_demo", tenant.staff.id, resource_type, resource_id)
        db.commit()

        archive_skill(
            "weather", tenant_id="tenant_demo", db=db, agent_id=None, current_user=tenant.admin
        )
        archive_general_skill(
            "weather-skill",
            tenant_id="tenant_demo",
            db=db,
            agent_id=None,
            current_user=tenant.admin,
        )
        update_knowledge_base(
            kb.id,
            KnowledgeBaseUpdateRequest(tenant_id="tenant_demo", status="archived"),
            None,
            db,
            tenant.admin,
        )

        # 归档不改引用关系 —— 引用行仍然三条。
        assert len(db.exec(select(AgentResourceReference)).all()) == 3
        # 运行时（include_inactive=False）看不到已归档资源。
        assert visible_skill_rows(db, "tenant_demo", tenant.staff.id, include_inactive=False) == []
        assert list_tools("tenant_demo", None, tenant.staff.id, db) == []
        assert (
            visible_knowledge_base_versions(
                db, "tenant_demo", tenant.staff.id, include_inactive=False
            )
            == {}
        )
        # 管理视图（include_inactive=True）仍然拿得到它们。
        assert [row.id for row in visible_skill_rows(db, "tenant_demo", tenant.staff.id)] == [
            skill.id
        ]


def test_archived_gallery_knowledge_base_is_listed_for_referencing_staff() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        kb = KnowledgeBase(
            id="kb_gallery_row",
            tenant_id="tenant_demo",
            name="天气资料",
            status="active",
            scope=GALLERY_SCOPE,
            owner_agent_id=None,
        )
        db.add(kb)
        db.commit()
        reference_resource(db, "tenant_demo", tenant.staff.id, "knowledge_base", kb.id)
        db.commit()

        kb.status = "archived"
        db.add(kb)
        db.commit()

        listed = list_knowledge_bases(tenant_id="tenant_demo", agent_id=tenant.staff.id, db=db)
        assert [row.id for row in listed] == [kb.id]


def test_staff_can_delete_own_private_skill_and_it_leaves_the_visible_set() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        created = create_skill(
            SkillCreateRequest(
                tenant_id="tenant_demo",
                content=SkillCard.model_validate(_graph("私有流程", "1.0.0")),
                status="published",
            ),
            agent_id=tenant.staff.id,
            db=db,
            current_user=tenant.owner,
        )
        assert created.id is not None
        row = db.exec(select(Skill).where(Skill.skill_id == "skill_purchase")).one()
        assert row.scope == AGENT_SCOPE
        assert row.owner_agent_id == tenant.staff.id
        assert row.created_by_user_id == tenant.owner.id

        assert (
            delete_skill(
                "skill_purchase",
                tenant_id="tenant_demo",
                db=db,
                agent_id=tenant.staff.id,
                current_user=tenant.owner,
            )
            == {"status": "deleted"}
        )
        assert db.get(Skill, row.id) is None
        assert visible_skill_rows(db, "tenant_demo", tenant.staff.id) == []


def test_non_owner_cannot_delete_a_gallery_skill_but_admin_can() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        skill = _gallery_skill(db, skill_id="weather", name="天气流程")

        with pytest.raises(HTTPException) as member_error:
            delete_skill(
                "weather",
                tenant_id="tenant_demo",
                db=db,
                agent_id=tenant.staff.id,
                current_user=tenant.owner,
            )
        assert member_error.value.status_code == 403
        assert db.get(Skill, skill.id) is not None

        assert (
            delete_skill(
                "weather",
                tenant_id="tenant_demo",
                db=db,
                agent_id=None,
                current_user=tenant.admin,
            )
            == {"status": "deleted"}
        )
        assert db.get(Skill, skill.id) is None


# --------------------------------------------------------------------------- #
# 创作人展示信息由列派生
# --------------------------------------------------------------------------- #


def test_gallery_skill_read_derives_creator_metadata_from_created_by_column() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        skill = _gallery_skill(db, skill_id="weather", name="天气流程", created_by=tenant.admin.id)

        meta = resource_creator_metadata(db, "tenant_demo", skill)
        assert meta["created_by_username"] == "admin"
        assert meta["creator_name"] == "admin"

        read = skill_read(skill, None, None, meta)
        assert read.metadata["created_by_username"] == "admin"


def test_private_skill_read_derives_creator_metadata_from_created_by_column() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        created = create_skill(
            SkillCreateRequest(
                tenant_id="tenant_demo",
                content=SkillCard.model_validate(_graph("私有流程", "1.0.0")),
                status="published",
            ),
            agent_id=tenant.staff.id,
            db=db,
            current_user=tenant.owner,
        )
        assert created.metadata["created_by_username"] == tenant.owner.username
        assert created.metadata["creator_name"] == tenant.owner.username

        row = db.exec(select(Skill).where(Skill.skill_id == "skill_purchase")).one()
        assert resource_creator_metadata(db, "tenant_demo", row)["created_by_username"] == "member"


# --------------------------------------------------------------------------- #
# 级联清理 & 归属列写入
# --------------------------------------------------------------------------- #


def test_purge_agent_removes_private_rows_and_references_but_spares_the_gallery() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        staff_id = tenant.staff.id
        private_skill = Skill(
            id="skill_owned",
            tenant_id="tenant_demo",
            skill_id="owned_sop",
            version="1.0.0",
            name="员工自己的 SOP",
            status="published",
            content_json=_graph("员工自己的 SOP", "1.0.0"),
            scope=AGENT_SCOPE,
            owner_agent_id=staff_id,
        )
        gallery_skill = _gallery_skill(db, skill_id="weather", name="广场天气流程")
        db.add(private_skill)
        db.commit()
        reference_resource(db, "tenant_demo", staff_id, "skill", gallery_skill.id)
        db.commit()

        purge_agent(db, "tenant_demo", tenant.staff)
        db.commit()

        assert db.get(AgentProfile, staff_id) is None
        assert db.get(Skill, private_skill.id) is None
        # 广场资源不属于任何员工，绝不能被误删；指向它的引用行随员工一起清掉。
        assert db.get(Skill, gallery_skill.id) is not None
        assert db.exec(select(AgentResourceReference)).all() == []


def test_purge_agent_owned_resources_only_touches_the_owner_plane() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        other_staff = _staff(db, "agent_other", "另一个员工")
        mine = Skill(
            id="skill_mine",
            tenant_id="tenant_demo",
            skill_id="mine_sop",
            version="1.0.0",
            name="我的 SOP",
            status="published",
            content_json=_graph("我的 SOP", "1.0.0"),
            scope=AGENT_SCOPE,
            owner_agent_id=tenant.staff.id,
        )
        theirs = Skill(
            id="skill_theirs",
            tenant_id="tenant_demo",
            skill_id="theirs_sop",
            version="1.0.0",
            name="别人的 SOP",
            status="published",
            content_json=_graph("别人的 SOP", "1.0.0"),
            scope=AGENT_SCOPE,
            owner_agent_id=other_staff.id,
        )
        db.add(mine)
        db.add(theirs)
        db.commit()

        counts = purge_agent_owned_resources(db, "tenant_demo", tenant.staff.id)
        db.commit()

        assert counts["skill"] == 1
        assert db.get(Skill, mine.id) is None
        assert db.get(Skill, theirs.id) is not None


def test_ensure_private_resource_binding_writes_ownership_columns_not_reference_rows() -> None:
    with _test_session() as db:
        tenant = _seed_tenant_with_staff(db)
        gallery_skill = _gallery_skill(db, skill_id="weather", name="广场天气流程")
        reference_resource(db, "tenant_demo", tenant.staff.id, "skill", gallery_skill.id)
        db.commit()

        from app.agents.branching import ensure_private_resource_binding

        # 把一个资源收归员工私有 → 写 scope/owner_agent_id 两列，并清掉旧引用行。
        ensure_private_resource_binding(
            db, "tenant_demo", tenant.staff.id, "skill", gallery_skill.id
        )
        db.commit()

        db.refresh(gallery_skill)
        assert gallery_skill.scope == AGENT_SCOPE
        assert gallery_skill.owner_agent_id == tenant.staff.id
        assert db.exec(select(AgentResourceReference)).all() == []


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class _TenantScope:
    def __init__(self, admin: User, owner: User, staff: AgentProfile) -> None:
        self.admin = admin
        self.owner = owner
        self.staff = staff


def _seed_tenant_with_staff(db: Session) -> _TenantScope:
    admin = _admin_user()
    owner = _member_user()
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(admin)
    db.add(owner)
    staff = AgentProfile(
        owner_user_id=owner.id,
        id="agent_staff",
        tenant_id="tenant_demo",
        name="本人员工",
    )
    db.add(staff)
    db.commit()
    return _TenantScope(admin=admin, owner=owner, staff=staff)


def _staff(db: Session, agent_id: str, name: str) -> AgentProfile:
    row = AgentProfile(
        owner_user_id="user_member",
        id=agent_id,
        tenant_id="tenant_demo",
        name=name,
    )
    db.add(row)
    db.commit()
    return row


def _gallery_skill(
    db: Session,
    *,
    skill_id: str,
    name: str,
    created_by: str = "user_admin",
) -> Skill:
    row = Skill(
        id=f"skill_{skill_id}_row",
        tenant_id="tenant_demo",
        skill_id=skill_id,
        version="1.0.0",
        name=name,
        business_domain="电商",
        description=f"{name} 说明",
        status="published",
        content_json=_graph(name, "1.0.0"),
        scope=GALLERY_SCOPE,
        owner_agent_id=None,
        created_by_user_id=created_by,
    )
    db.add(row)
    db.commit()
    return row


def _gallery_tool(db: Session, *, name: str) -> Tool:
    row = Tool(
        id=f"tool_{name.replace('.', '_')}",
        tenant_id="tenant_demo",
        name=name,
        display_name=name,
        method="POST",
        url=f"/api/mock/{name}",
        scope=GALLERY_SCOPE,
        owner_agent_id=None,
        created_by_user_id="user_admin",
    )
    db.add(row)
    db.commit()
    return row


def _tool_request(name: str, display_name: str):
    from app.tools.tool_schema import ToolCreateRequest

    return ToolCreateRequest(
        tenant_id="tenant_demo",
        name=name,
        display_name=display_name,
        url=f"/api/mock/{name}",
    )


def _agent_summary(db: Session, agent_id: str):
    rows = list_agents("tenant_demo", db, _admin_user())
    return next(row for row in rows if row.id == agent_id)


def _graph(name: str, version: str, skill_id: str = "skill_purchase") -> dict[str, object]:
    return {
        "skill_id": skill_id,
        "version": version,
        "name": name,
        "business_domain": "电商",
        "description": "购买商品",
        "nodes": [
            {
                "node_id": "collect",
                "type": "collect_info",
                "name": "收集信息",
                "instruction": "收集用户信息",
                "expected_user_info": ["user_name"],
                "allowed_actions": ["ask_user", "continue_flow"],
            },
            {
                "node_id": "reply",
                "type": "response",
                "name": "回复用户",
                "instruction": "回复用户",
                "allowed_actions": ["answer_user"],
            },
        ],
        "edges": [{"source_node_id": "collect", "next_node_id": "reply"}],
        "start_node_id": "collect",
        "terminal_node_ids": ["reply"],
    }


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)
