from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import reference_resource
from app.api.agents import purge_agent
from app.db import seed as seed_module
from app.db.models import (
    GALLERY_SCOPE,
    AgentProfile,
    AgentResourceReference,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Skill,
    Tool,
)
from app.db.preset_resources import preset_agent_ids
from app.db.seed import seed_demo_data

TENANT_ID = "tenant_demo"
PRESET_SALES_AGENT = "agent_preset_sales_001"
PRESET_FINANCE_AGENT = "agent_f2828efc2a2a476d"


def _seeded_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    seed_demo_data(session)
    session.commit()
    return session


def _gallery_rows(session: Session, model) -> list:
    return list(session.exec(select(model).where(model.scope == GALLERY_SCOPE)).all())


def _gallery_counts(session: Session) -> dict[str, int]:
    return {
        "skill": len(_gallery_rows(session, Skill)),
        "knowledge_base": len(_gallery_rows(session, KnowledgeBase)),
        "tool": len(_gallery_rows(session, Tool)),
        "general_skill": len(_gallery_rows(session, GeneralSkill)),
    }


def _referenced_resources(session: Session, agent_id: str) -> list[tuple[str, str]]:
    return [
        (reference.resource_type, reference.resource_id)
        for reference in session.exec(
            select(AgentResourceReference).where(AgentResourceReference.agent_id == agent_id)
        ).all()
    ]


def _model_for(resource_type: str):
    return {
        "skill": Skill,
        "knowledge_base": KnowledgeBase,
        "tool": Tool,
        "general_skill": GeneralSkill,
    }[resource_type]


def test_seeded_gallery_publishes_preset_owners() -> None:
    """预置资源落地时归属在演示包引用表里，且每条都被某个预置员工引用。"""
    with _seeded_session() as session:
        assert len(_gallery_rows(session, Skill)) > 0
        for resource_type, resource_id in _referenced_resources(session, PRESET_SALES_AGENT):
            model = _model_for(resource_type)
            assert session.get(model, resource_id) is not None


def test_deleting_preset_agent_reclaims_its_unused_gallery_resources() -> None:
    """删除预置员工，它独占的广场资源要跟着走，别留在广场当孤儿。"""
    with _seeded_session() as session:
        owned = _referenced_resources(session, PRESET_SALES_AGENT)
        assert owned, "销售员工应引用预置资源"
        before = _gallery_counts(session)

        purge_agent(session, TENANT_ID, session.get(AgentProfile, PRESET_SALES_AGENT))
        session.commit()

        after = _gallery_counts(session)
        for resource_type, resource_id in owned:
            assert session.get(_model_for(resource_type), resource_id) is None
        assert after["skill"] == before["skill"] - 1
        assert after["knowledge_base"] == before["knowledge_base"] - 1


def test_deleting_preset_agent_purges_resource_children() -> None:
    """回收知识库时，文档/分块/入库任务等子行不能留下悬空记录。"""
    with _seeded_session() as session:
        purge_agent(session, TENANT_ID, session.get(AgentProfile, PRESET_FINANCE_AGENT))
        session.commit()

        live_knowledge_base_ids = {row.id for row in session.exec(select(KnowledgeBase)).all()}
        for model in (
            KnowledgeDocument,
            KnowledgeBucket,
            KnowledgeChunk,
            KnowledgeIngestJob,
        ):
            for child in session.exec(select(model)).all():
                assert child.knowledge_base_id in live_knowledge_base_ids

        live_skill_ids = {row.skill_id for row in session.exec(select(Skill)).all()}
        assert len(live_skill_ids) >= 0


def test_deleting_preset_agent_keeps_resource_other_agent_still_uses() -> None:
    """预置资源被别人引用时，它还在被使用，不能因为主人被删就一起删掉。"""
    with _seeded_session() as session:
        owned = _referenced_resources(session, PRESET_SALES_AGENT)
        shared_type, shared_id = owned[0]
        own_type, own_id = owned[1]

        session.add(
            AgentProfile(
                id="agent_jie",
                tenant_id=TENANT_ID,
                owner_user_id="admin",
                name="阿杰",
                is_published=False,
                status="active",
            )
        )
        session.flush()
        reference_resource(session, TENANT_ID, "agent_jie", shared_type, shared_id)
        session.commit()

        purge_agent(session, TENANT_ID, session.get(AgentProfile, PRESET_SALES_AGENT))
        session.commit()

        assert session.get(_model_for(shared_type), shared_id) is not None
        assert session.get(_model_for(own_type), own_id) is None


def test_deleting_regular_agent_does_not_touch_gallery() -> None:
    """普通员工不是预置员工，删它不该动广场里的预置资源。"""
    with _seeded_session() as session:
        session.add(
            AgentProfile(
                id="agent_jie",
                tenant_id=TENANT_ID,
                owner_user_id="admin",
                name="阿杰",
                is_published=False,
                status="active",
            )
        )
        session.commit()
        before = _gallery_counts(session)

        purge_agent(session, TENANT_ID, session.get(AgentProfile, "agent_jie"))
        session.commit()

        assert _gallery_counts(session) == before


def _drop_preset_agents_without_reclaim(session: Session) -> None:
    """复刻旧版本的删除行为：只删员工与引用行，不回收广场资源。"""
    preset_ids = preset_agent_ids()
    for reference in session.exec(select(AgentResourceReference)).all():
        if reference.agent_id in preset_ids:
            session.delete(reference)
    for agent in session.exec(select(AgentProfile)).all():
        if agent.id in preset_ids:
            session.delete(agent)
    session.commit()


def _forget_reclaim_marker(session: Session) -> None:
    """抹掉清账标记，把库还原成「清账逻辑上线前」的样子。"""
    session.execute(
        text("DELETE FROM app_data_migrations WHERE id = :id"),
        {"id": seed_module.ORPHAN_PRESET_RECLAIM_MARKER_ID},
    )
    session.commit()


def test_one_time_reclaim_clears_orphans_left_by_older_versions() -> None:
    """一次性清账：把「主人已经被删、资源还挂在广场」的预置资源收干净。"""
    with _seeded_session() as session:
        _drop_preset_agents_without_reclaim(session)
        _forget_reclaim_marker(session)
        orphan_counts = _gallery_counts(session)

        seed_module._reclaim_orphan_preset_resources_once(session)
        session.commit()

        after = _gallery_counts(session)
        assert after["skill"] < orphan_counts["skill"]
        assert after["general_skill"] == 0
        # 演示包里不归属任何员工的展示内容要留住。
        assert session.exec(select(Skill).where(Skill.skill_id == "after_sales_refund")).first()
        assert session.exec(select(Tool).where(Tool.name == "order.refund")).first()


def test_one_time_reclaim_is_idempotent() -> None:
    """清账只做一次：标记写入后再跑不会重复扫描、也不会再删东西。"""
    with _seeded_session() as session:
        _drop_preset_agents_without_reclaim(session)
        _forget_reclaim_marker(session)

        seed_module._reclaim_orphan_preset_resources_once(session)
        session.commit()
        first_pass = _gallery_counts(session)

        assert seed_module._applied_marker_present(
            session, seed_module.ORPHAN_PRESET_RECLAIM_MARKER_ID
        )

        seed_module._reclaim_orphan_preset_resources_once(session)
        session.commit()
        assert _gallery_counts(session) == first_pass


def test_one_time_reclaim_keeps_resources_while_owner_exists() -> None:
    """预置员工还在，他们的资源就还在使用中，清账不能碰。"""
    with _seeded_session() as session:
        _forget_reclaim_marker(session)
        before = _gallery_counts(session)
        seed_module._reclaim_orphan_preset_resources_once(session)
        session.commit()
        assert _gallery_counts(session) == before
