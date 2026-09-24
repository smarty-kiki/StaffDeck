import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import (
    is_open_gallery_resource,
    reference_resource,
)
from app.db.models import (
    GALLERY_SCOPE,
    AgentProfile,
    AgentResourceReference,
    EvolutionProposal,
    GeneralSkill,
    Tenant,
    User,
)
from app.evolution.service import EvolutionService, _json_diff, _risk_for_sop_diff
from app.evolution.schema import EvolutionAnalyzeRequest


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_json_diff_is_a_stable_json_patch_style_list() -> None:
    changes = _json_diff(
        {"nodes": [{"node_id": "collect", "instruction": "old"}], "enabled": True},
        {"nodes": [{"node_id": "collect", "instruction": "new"}], "enabled": True},
    )

    assert changes == [
        {
            "op": "replace",
            "path": "/nodes/0/instruction",
            "before": "old",
            "after": "new",
        }
    ]
    assert _risk_for_sop_diff(changes) == "medium"


def test_approve_gallery_skill_edits_in_place_and_can_rollback() -> None:
    """广场技能的进化候选原地生效：不再产生员工的私有副本。

    引用模型下，员工只是「引用」广场技能；作者（或管理员）改写广场技能后，
    所有引用者直接看到最新内容。回滚同理，把广场技能还原到快照。
    """
    with _session() as db:
        db.add(Tenant(id="tenant_test", name="Test"))
        admin = User(
            id="user_admin",
            tenant_id="tenant_test",
            username="admin",
            password_hash="x",
            role="admin",
        )
        owner = User(
            id="user_owner",
            tenant_id="tenant_test",
            username="owner",
            password_hash="x",
        )
        agent = AgentProfile(owner_user_id=owner.id,
            id="agent_finance",
            tenant_id="tenant_test",
            name="财务员工",
            metadata_json={"owner_user_id": owner.id},
        )
        source = GeneralSkill(scope=GALLERY_SCOPE, owner_agent_id=None,
            id="genskill_policy",
            tenant_id="tenant_test",
            slug="policy-answer",
            name="政策答疑",
            skill_markdown="# 政策答疑\n旧说明\n",
            status="published",
        )
        db.add(admin)
        db.add(owner)
        db.add(agent)
        db.add(source)
        db.flush()
        # 员工「引用」广场技能 —— 引用行的存在即生效。
        reference_resource(db, "tenant_test", agent.id, "general_skill", source.id)
        proposal = EvolutionProposal(
            id="evo_gallery_reference",
            tenant_id="tenant_test",
            agent_id=agent.id,
            resource_type="general_skill",
            resource_id=source.id,
            resource_key=source.slug,
            resource_name=source.name,
            status="ready_for_review",
            hypothesis="指令不够明确",
            candidate_json={
                "skill_markdown": "# 政策答疑\n只引用正式政策回答。\n",
                "description": source.description,
            },
            diff_json=[
                {
                    "op": "replace",
                    "path": "/skill_markdown",
                    "before": source.skill_markdown,
                    "after": "# 政策答疑\n只引用正式政策回答。\n",
                }
            ],
            created_by_user_id=owner.id,
        )
        db.add(proposal)
        db.commit()

        # 广场资源归广场：非管理员无法借进化改写别人共享的技能。
        with pytest.raises(HTTPException) as denied:
            EvolutionService(db).approve(proposal, owner)
        assert denied.value.status_code == 403
        db.refresh(source)
        assert source.skill_markdown == "# 政策答疑\n旧说明\n"

        published = EvolutionService(db).approve(proposal, admin)
        db.refresh(source)

        assert published.status == "published"
        # 引用模型：没有私有副本，候选直接改写广场技能本体。
        assert published.resource_id == source.id
        assert source.skill_markdown == "# 政策答疑\n只引用正式政策回答。\n"
        assert is_open_gallery_resource(db, "tenant_test", "general_skill", source)
        # 引用行保持不变 —— 引用者拿到的就是刚改写后的最新内容。
        binding = db.exec(
            select(AgentResourceReference).where(
                AgentResourceReference.agent_id == agent.id,
                AgentResourceReference.resource_type == "general_skill",
                AgentResourceReference.resource_id == source.id,
            )
        ).first()
        assert binding is not None

        rolled_back = EvolutionService(db).rollback(published, admin)
        db.refresh(source)
        assert rolled_back.status == "rolled_back"
        assert source.skill_markdown == "# 政策答疑\n旧说明\n"


def test_candidate_does_not_modify_private_skill_before_approval() -> None:
    with _session() as db:
        db.add(Tenant(id="tenant_test", name="Test"))
        skill = GeneralSkill(scope=GALLERY_SCOPE, owner_agent_id=None,
            id="genskill_private",
            tenant_id="tenant_test",
            slug="private-skill",
            name="Private",
            skill_markdown="original",
            status="published",
        )
        proposal = EvolutionProposal(
            tenant_id="tenant_test",
            agent_id="agent_private",
            resource_type="general_skill",
            resource_id=skill.id,
            resource_key=skill.slug,
            resource_name=skill.name,
            status="ready_for_review",
            candidate_json={"skill_markdown": "candidate"},
            diff_json=[
                {
                    "op": "replace",
                    "path": "/skill_markdown",
                    "before": "original",
                    "after": "candidate",
                }
            ],
            created_by_user_id="user_owner",
        )
        db.add(skill)
        db.add(proposal)
        db.commit()

        db.refresh(skill)
        assert skill.skill_markdown == "original"


def test_analyze_without_feedback_returns_localizable_error_code() -> None:
    with _session() as db:
        db.add(Tenant(id="tenant_test", name="Test"))
        owner = User(
            id="user_owner",
            tenant_id="tenant_test",
            username="owner",
            password_hash="x",
        )
        agent = AgentProfile(owner_user_id=owner.id,
            id="agent_empty",
            tenant_id="tenant_test",
            name="Empty",
            metadata_json={"owner_user_id": owner.id},
        )
        db.add(owner)
        db.add(agent)
        db.commit()

        with pytest.raises(HTTPException) as caught:
            EvolutionService(db).analyze(
                agent.id,
                EvolutionAnalyzeRequest(tenant_id="tenant_test"),
                owner,
            )

        assert caught.value.status_code == 404
        assert caught.value.detail == {
            "code": "EVOLUTION_FEEDBACK_NOT_FOUND",
            "message": "未找到可用于改进的 Skill 或 SOP 反馈",
        }
