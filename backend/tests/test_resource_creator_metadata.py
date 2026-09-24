from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.general_skills import import_general_skill
from app.api.knowledge_bases import create_knowledge_base, update_knowledge_base
from app.api.skills import create_skill
from app.api.tools import create_tool, update_tool
from app.db.models import (
    AgentProfile,
    AgentResourceReference,
    GeneralSkill,
    KnowledgeBaseVersion,
    Skill,
    Tenant,
    Tool,
    User,
)
from app.general_skills.schema import GeneralSkillImportRequest
from app.knowledge.schema import KnowledgeBaseCreateRequest, KnowledgeBaseUpdateRequest
from app.skills.skill_schema import SkillCard, SkillCreateRequest
from app.tools.tool_schema import ToolCreateRequest, ToolUpdateRequest


def test_user_created_resource_metadata_is_bound_to_current_user() -> None:
    with _test_session() as db:
        user = _seed_user_and_agent(db)
        agent = db.get(AgentProfile, "agent_owner")
        assert agent is not None

        knowledge = create_knowledge_base(
            KnowledgeBaseCreateRequest(tenant_id="tenant_demo", name="用户知识库"),
            agent_id=agent.id,
            db=db,
            current_user=user,
        )
        assert knowledge.metadata["creator_name"] == "alice"
        assert knowledge.metadata["created_by_user_id"] == "user_alice"
        version = db.exec(select(KnowledgeBaseVersion)).first()
        assert version is not None
        assert version.metadata_json["creator_name"] == "alice"

        tool = create_tool(
            ToolCreateRequest(
                tenant_id="tenant_demo",
                name="user.weather",
                display_name="用户天气",
                url="https://example.com/weather",
            ),
            agent_id=agent.id,
            db=db,
            current_user=user,
        )
        assert tool.metadata["creator_name"] == "alice"
        assert tool.metadata["created_by_username"] == "alice"
        # 私有资源「归属即生效」——不再产生引用行，归属落在资源表上。
        assert _no_reference(db, agent.id, "tool", tool.id)
        stored_tool = db.get(Tool, tool.id)
        assert stored_tool is not None
        assert stored_tool.created_by_user_id == "user_alice"
        assert stored_tool.owner_agent_id == agent.id

        skill = create_skill(
            SkillCreateRequest(
                tenant_id="tenant_demo",
                content=_skill_card(),
                status="published",
            ),
            agent_id=agent.id,
            db=db,
            current_user=user,
        )
        assert skill.metadata["creator_name"] == "alice"
        stored_skill = db.exec(select(Skill)).first()
        assert stored_skill is not None
        assert stored_skill.created_by_user_id == "user_alice"
        assert stored_skill.owner_agent_id == agent.id
        assert _no_reference(db, agent.id, "skill", stored_skill.id)

        general_skill = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id=agent.id,
                name="用户通用技能",
                slug="user-general-skill",
                markdown="# 用户通用技能\n\n用于测试 creator metadata。",
            ),
            db=db,
            current_user=user,
        )
        assert general_skill.metadata["creator_name"] == "alice"
        assert general_skill.metadata["created_by_user_id"] == "user_alice"
        assert _no_reference(db, agent.id, "general_skill", general_skill.id)
        stored_general_skill = db.exec(select(GeneralSkill)).first()
        assert stored_general_skill is not None
        assert stored_general_skill.created_by_user_id == "user_alice"
        assert stored_general_skill.owner_agent_id == agent.id

        # 非归属人（哪怕是管理员）不能改写别人的员工资源。
        editor = User(
            id="user_editor",
            tenant_id="tenant_demo",
            username="editor",
            display_name="Editor",
            password_hash="test",
            role="admin",
        )
        db.add(editor)
        db.commit()

        with pytest.raises(HTTPException) as denied:
            update_knowledge_base(
                knowledge.id,
                KnowledgeBaseUpdateRequest(
                    tenant_id="tenant_demo",
                    metadata={"topic": "should-not-apply"},
                ),
                agent_id=agent.id,
                db=db,
                current_user=editor,
            )
        assert denied.value.status_code == 403

        # 归属人改写：创建人信息保持不变，可编辑字段正常更新。
        updated_knowledge = update_knowledge_base(
            knowledge.id,
            KnowledgeBaseUpdateRequest(
                tenant_id="tenant_demo",
                metadata={"topic": "updated"},
            ),
            agent_id=agent.id,
            db=db,
            current_user=user,
        )
        assert updated_knowledge.metadata["creator_name"] == "alice"
        assert updated_knowledge.metadata["topic"] == "updated"

        updated_tool = update_tool(
            tool.id,
            ToolUpdateRequest(
                tenant_id="tenant_demo",
                name="user.weather",
                display_name="更新后的用户天气",
                url="https://example.com/weather",
            ),
            agent_id=agent.id,
            db=db,
            current_user=user,
        )
        assert updated_tool.metadata["creator_name"] == "alice"

        updated_general_skill = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id=agent.id,
                name="更新后的用户通用技能",
                slug="user-general-skill",
                original_slug="user-general-skill",
                markdown="# 更新后的用户通用技能\n\n用于测试 creator metadata。",
            ),
            db=db,
            current_user=user,
        )
        assert updated_general_skill.metadata["creator_name"] == "alice"


def _seed_user_and_agent(db: Session) -> User:
    user = User(
        id="user_alice",
        tenant_id="tenant_demo",
        username="alice",
        display_name="Alice",
        password_hash="test",
    )
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(user)
    db.add(
        AgentProfile(
            owner_user_id=user.id,
            id="agent_owner",
            tenant_id="tenant_demo",
            name="研发员工",
            metadata_json={
                "owner_username": user.username,
                "owner_display_name": user.display_name,
                "created_by_user_id": user.id,
                "created_by_username": user.username,
            },
        )
    )
    db.commit()
    return user


def _skill_card() -> SkillCard:
    return SkillCard(
        skill_id="skill_user_creator",
        name="用户 SOP",
        description="测试 creator metadata",
        nodes=[
            {
                "node_id": "start",
                "type": "response",
                "name": "回复",
                "instruction": "回复用户",
                "allowed_actions": ["answer_user"],
            }
        ],
        start_node_id="start",
        terminal_node_ids=["start"],
    )


def _no_reference(db: Session, agent_id: str, resource_type: str, resource_id: str) -> bool:
    """私有资源不再产生引用行 —— 归属即生效。"""
    return (
        db.exec(
            select(AgentResourceReference).where(
                AgentResourceReference.tenant_id == "tenant_demo",
                AgentResourceReference.agent_id == agent_id,
                AgentResourceReference.resource_type == resource_type,
                AgentResourceReference.resource_id == resource_id,
            )
        ).first()
        is None
    )


@contextmanager
def _test_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
