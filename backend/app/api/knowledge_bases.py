from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlmodel import Session, select

from app.agents.branching import (
    ensure_knowledge_base_version,
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    ensure_visible_name_unique,
    knowledge_version_for_upload,
    mark_resource_open_gallery,
    mark_resource_private_for_agent,
    metadata_preserving_creator,
    purge_resource_references,
    user_creator_metadata,
    visible_knowledge_base_versions,
)
from app.capability_scope import normalize_capability_scope
from app.db import get_session
from app.db.models import (
    AGENT_SCOPE,
    GALLERY_SCOPE,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    User,
    utc_now,
)
from app.knowledge.okf import (
    build_okf_for_document,
    export_okf_bundle,
    lint_okf_concepts,
    normalize_concept_id,
    parse_okf_markdown,
    persist_lint_issues,
    upsert_concepts,
)
from app.knowledge.schema import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseRead,
    KnowledgeBaseUpdateRequest,
    KnowledgeConceptRead,
    KnowledgeConceptUpdateRequest,
)
from app.security.auth import get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    ensure_resource_writer,
    require_agent_scope_viewer,
)
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/knowledge-bases",
    tags=["enterprise:knowledge-bases"],
    dependencies=[Depends(get_current_user)],
)


@router.get(
    "", response_model=list[KnowledgeBaseRead], dependencies=[Depends(require_agent_scope_viewer)]
)
def list_knowledge_bases(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[KnowledgeBaseRead]:
    """员工可见知识库 = 自己拥有的 ∪ 已引用的广场知识库。无分支、无副本。"""
    ensure_tenant(db, tenant_id)
    versions = visible_knowledge_base_versions(db, tenant_id, agent_id, include_inactive=True)
    if not versions:
        return []
    rows = db.exec(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.id.in_(list(versions.keys())),
        )
        .order_by(KnowledgeBase.updated_at.desc())
    ).all()
    stats = _knowledge_base_stats(db, tenant_id, [version.id for version in versions.values()])
    return [
        knowledge_base_read(row, stats.get(row.id, {}), version_row=versions.get(row.id))
        for row in rows
    ]


@router.post("", response_model=KnowledgeBaseRead)
def create_knowledge_base(
    request: KnowledgeBaseCreateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeBaseRead:
    ensure_tenant(db, request.tenant_id)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    private_owner_agent_id = agent.id if agent else None
    if not private_owner_agent_id:
        ensure_open_gallery_admin(request.tenant_id, current_user)
    # 重名只在同一归属内查：不同员工可以有同名知识库，广场内仍唯一。
    conflict_statement = select(KnowledgeBase).where(
        KnowledgeBase.tenant_id == request.tenant_id,
        KnowledgeBase.name == name,
    )
    if private_owner_agent_id:
        conflict_statement = conflict_statement.where(
            KnowledgeBase.scope == AGENT_SCOPE,
            KnowledgeBase.owner_agent_id == private_owner_agent_id,
        )
    else:
        conflict_statement = conflict_statement.where(KnowledgeBase.scope == GALLERY_SCOPE)
    if db.exec(conflict_statement).first():
        raise HTTPException(status_code=409, detail="Knowledge base name already exists")
    # 跨来源唯一性：私有知识库名不能与该员工已引用的广场知识库重名（设计 4.7）。
    ensure_visible_name_unique(
        db, request.tenant_id, private_owner_agent_id, "knowledge_base", name
    )
    creator_metadata = user_creator_metadata(current_user, request.metadata)
    row = KnowledgeBase(
        tenant_id=request.tenant_id,
        name=name,
        description=request.description,
        capability_scope=request.capability_scope,
        metadata_json=creator_metadata,
        status="active",
        scope=AGENT_SCOPE if private_owner_agent_id else GALLERY_SCOPE,
        owner_agent_id=private_owner_agent_id,
        created_by_user_id=current_user.id,
    )
    db.add(row)
    db.flush()
    if private_owner_agent_id:
        mark_resource_private_for_agent(row, private_owner_agent_id, creator_metadata)
        ensure_private_resource_binding(
            db, request.tenant_id, private_owner_agent_id, "knowledge_base", row.id
        )
    else:
        mark_resource_open_gallery(row, creator_metadata)
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "knowledge_base",
            row.id,
            "active",
            metadata_json=creator_metadata,
        )
    db.commit()
    db.refresh(row)
    return knowledge_base_read(row, {}, version_row=ensure_knowledge_base_version(db, row))


@router.get(
    "/{knowledge_base_id}",
    response_model=KnowledgeBaseRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_knowledge_base(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeBaseRead:
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    visible_version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    stats = _knowledge_base_stats(
        db,
        tenant_id,
        [visible_version.id],
    )
    return knowledge_base_read(
        row,
        stats.get(row.id, {}),
        version_row=visible_version,
    )


@router.put("/{knowledge_base_id}", response_model=KnowledgeBaseRead)
def update_knowledge_base(
    knowledge_base_id: str,
    request: KnowledgeBaseUpdateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeBaseRead:
    """更新知识库。

    私有知识库只有归属人可改（`ensure_agent_scope_manager` 里没有 admin 分支）；
    广场知识库仅管理员可改。知识库只有一份内容，改动对所有引用者立即可见。
    """
    row = _get_knowledge_base(db, request.tenant_id, knowledge_base_id)
    ensure_resource_writer(db, request.tenant_id, current_user, row)
    version = ensure_knowledge_base_version(db, row)
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
        conflict_statement = select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == request.tenant_id,
            KnowledgeBase.name == name,
            KnowledgeBase.id != row.id,
        )
        if getattr(row, "scope", None) == AGENT_SCOPE:
            conflict_statement = conflict_statement.where(
                KnowledgeBase.scope == AGENT_SCOPE,
                KnowledgeBase.owner_agent_id == row.owner_agent_id,
            )
        else:
            conflict_statement = conflict_statement.where(KnowledgeBase.scope == GALLERY_SCOPE)
        if db.exec(conflict_statement).first():
            raise HTTPException(status_code=409, detail="Knowledge base name already exists")
        row.name = name
        version.name = name
    if request.description is not None:
        row.description = request.description
        version.description = request.description
    if request.capability_scope is not None:
        row.capability_scope = request.capability_scope
        version.capability_scope = request.capability_scope
    if request.status is not None:
        row.status = request.status
        version.status = request.status
    if request.metadata is not None:
        row.metadata_json = metadata_preserving_creator(row.metadata_json, request.metadata)
        version.metadata_json = metadata_preserving_creator(version.metadata_json, request.metadata)
    row.updated_at = utc_now()
    version.updated_at = utc_now()
    db.add(row)
    db.add(version)
    db.flush()
    db.commit()
    db.refresh(row)
    return knowledge_base_read(
        row,
        _knowledge_base_stats(db, request.tenant_id).get(row.id, {}),
        version_row=version,
    )


@router.get("/{knowledge_base_id}/versions", dependencies=[Depends(require_agent_scope_viewer)])
def list_knowledge_base_versions(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[dict[str, object]]:
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    rows = db.exec(
        select(KnowledgeBaseVersion)
        .where(
            KnowledgeBaseVersion.tenant_id == tenant_id,
            KnowledgeBaseVersion.knowledge_base_id == row.id,
        )
        .order_by(KnowledgeBaseVersion.updated_at.desc())
    ).all()
    return [
        {
            "id": version.id,
            "version": version.version,
            "name": version.name,
            "description": version.description,
            "status": version.status,
            "capability_scope": normalize_capability_scope(version.capability_scope),
            "updated_at": version.updated_at.isoformat(),
            "created_at": version.created_at.isoformat(),
        }
        for version in rows
    ]


@router.get(
    "/{knowledge_base_id}/okf/concepts",
    response_model=list[KnowledgeConceptRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_okf_concepts(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    concept_type: str | None = Query(None),
    db: Session = Depends(get_session),
) -> list[KnowledgeConceptRead]:
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    stmt = select(KnowledgeConcept).where(
        KnowledgeConcept.tenant_id == tenant_id,
        KnowledgeConcept.knowledge_base_id == knowledge_base_id,
        KnowledgeConcept.knowledge_base_version_id == version.id,
        KnowledgeConcept.status != "deleted",
    )
    if concept_type:
        stmt = stmt.where(KnowledgeConcept.concept_type == concept_type)
    rows = db.exec(stmt.order_by(KnowledgeConcept.concept_type, KnowledgeConcept.concept_id)).all()
    return [concept_read(row) for row in rows]


@router.get(
    "/{knowledge_base_id}/okf/concepts/{concept_id:path}",
    response_model=KnowledgeConceptRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_okf_concept(
    knowledge_base_id: str,
    concept_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> KnowledgeConceptRead:
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    row = _get_concept(db, tenant_id, knowledge_base_id, version.id, concept_id)
    return concept_read(row)


@router.put(
    "/{knowledge_base_id}/okf/concepts/{concept_id:path}", response_model=KnowledgeConceptRead
)
def upsert_okf_concept(
    knowledge_base_id: str,
    concept_id: str,
    request: KnowledgeConceptUpdateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeConceptRead:
    version = _writable_knowledge_version(
        db, request.tenant_id, knowledge_base_id, agent_id, current_user
    )
    document_id = _document_id_for_version(
        db, request.tenant_id, knowledge_base_id, version.id, request.document_id
    )
    parsed = parse_okf_markdown(concept_id, request.content_md)
    rows = upsert_concepts(
        db,
        request.tenant_id,
        knowledge_base_id,
        version.id,
        [
            {
                "concept_id": parsed.concept_id,
                "content_md": parsed.content_md,
                "document_id": document_id,
                "status": request.status,
            }
        ],
    )
    return concept_read(rows[0])


@router.get("/{knowledge_base_id}/okf/export", dependencies=[Depends(require_agent_scope_viewer)])
def export_okf(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> Response:
    kb = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    rows = db.exec(
        select(KnowledgeConcept)
        .where(
            KnowledgeConcept.tenant_id == tenant_id,
            KnowledgeConcept.knowledge_base_id == knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == version.id,
            KnowledgeConcept.status == "active",
        )
        .order_by(KnowledgeConcept.concept_id)
    ).all()
    archive = export_okf_bundle(kb, version.id, rows)
    filename = f"{kb.name or knowledge_base_id}-okf-{version.version}.zip"
    fallback_filename = f"{knowledge_base_id}-okf-{version.version}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{fallback_filename}\"; filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.post("/{knowledge_base_id}/okf/lint", dependencies=[Depends(require_agent_scope_viewer)])
def lint_okf(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> dict[str, object]:
    version = _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    issues = lint_okf_concepts(db, tenant_id, knowledge_base_id, version.id)
    persist_lint_issues(db, tenant_id, knowledge_base_id, version.id, issues)
    return {"status": "ok", "issue_count": len(issues), "issues": issues}


@router.delete("/{knowledge_base_id}")
def delete_knowledge_base(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    ensure_resource_writer(db, tenant_id, current_user, row)
    # 知识库只有一份内容，删除即真删 —— 同时清理所有引用行，不留悬挂引用。
    for model in (
        KnowledgeDiscoverySuggestion,
        KnowledgeIngestJob,
        KnowledgeConcept,
        KnowledgeChunk,
        KnowledgeBucket,
        KnowledgeDocument,
        KnowledgeBaseVersion,
    ):
        children = db.exec(
            select(model).where(
                model.tenant_id == tenant_id,
                model.knowledge_base_id == row.id,
            )
        ).all()
        for child in children:
            db.delete(child)
    purge_resource_references(db, tenant_id, "knowledge_base", row.id)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


def knowledge_base_read(
    row: KnowledgeBase,
    stats: dict[str, int],
    version_row: KnowledgeBaseVersion | None = None,
) -> KnowledgeBaseRead:
    return KnowledgeBaseRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=version_row.name if version_row else row.name,
        description=version_row.description if version_row else row.description,
        status=row.status,
        capability_scope=normalize_capability_scope(
            version_row.capability_scope if version_row else row.capability_scope
        ),
        version=version_row.version if version_row else None,
        metadata=dict((version_row.metadata_json if version_row else row.metadata_json) or {}),
        document_count=int(stats.get("document_count", 0)),
        bucket_count=int(stats.get("bucket_count", 0)),
        chunk_count=int(stats.get("chunk_count", 0)),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def concept_read(row: KnowledgeConcept) -> KnowledgeConceptRead:
    return KnowledgeConceptRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        knowledge_base_version_id=row.knowledge_base_version_id,
        document_id=row.document_id,
        concept_id=row.concept_id,
        concept_type=row.concept_type,
        title=row.title,
        description=row.description,
        content_md=row.content_md,
        frontmatter=row.frontmatter_json or {},
        links=row.links_json or [],
        citations=row.citations_json or [],
        source_refs=row.source_refs_json or [],
        status=row.status,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _visible_knowledge_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
) -> KnowledgeBaseVersion:
    _get_knowledge_base(db, tenant_id, knowledge_base_id)
    versions = _management_knowledge_base_versions(db, tenant_id, agent_id)
    version = versions.get(knowledge_base_id)
    if not version:
        raise HTTPException(status_code=404, detail="Knowledge base version not visible")
    return version


def _writable_knowledge_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
    current_user: User,
) -> KnowledgeBaseVersion:
    _get_knowledge_base(db, tenant_id, knowledge_base_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent:
        version = knowledge_version_for_upload(
            db,
            tenant_id,
            knowledge_base_id,
            agent.id,
            metadata_json=user_creator_metadata(current_user),
        )
        db.commit()
        return version
    ensure_open_gallery_admin(tenant_id, current_user)
    return _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)


def _ensure_okf_concepts_for_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    version_id: str,
) -> None:
    documents = db.exec(
        select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == version_id,
            KnowledgeDocument.status == "ready",
        )
    ).all()
    for document in documents:
        existing = db.exec(
            select(KnowledgeConcept.id).where(
                KnowledgeConcept.tenant_id == tenant_id,
                KnowledgeConcept.knowledge_base_id == knowledge_base_id,
                KnowledgeConcept.knowledge_base_version_id == version_id,
                KnowledgeConcept.document_id == document.id,
            )
        ).first()
        if existing:
            continue
        metadata = document.metadata_json or {}
        section_nodes = (
            metadata.get("section_tree") if isinstance(metadata.get("section_tree"), list) else []
        )
        buckets = db.exec(
            select(KnowledgeBucket)
            .where(
                KnowledgeBucket.tenant_id == tenant_id,
                KnowledgeBucket.knowledge_base_id == knowledge_base_id,
                KnowledgeBucket.knowledge_base_version_id == version_id,
                KnowledgeBucket.document_id == document.id,
            )
            .order_by(KnowledgeBucket.created_at.asc())
        ).all()
        if not section_nodes and not buckets:
            continue
        upsert_concepts(
            db,
            tenant_id,
            knowledge_base_id,
            version_id,
            build_okf_for_document(document, section_nodes, buckets),
        )


def _get_concept(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_version_id: str,
    concept_id: str,
) -> KnowledgeConcept:
    normalized = normalize_concept_id(concept_id)
    row = db.exec(
        select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == tenant_id,
            KnowledgeConcept.knowledge_base_id == knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == knowledge_base_version_id,
            KnowledgeConcept.concept_id == normalized,
            KnowledgeConcept.status != "deleted",
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="OKF concept not found")
    return row


def _document_id_for_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_version_id: str,
    document_id: str | None,
) -> str | None:
    if not document_id:
        return None
    current = db.get(KnowledgeDocument, document_id)
    if (
        current
        and current.tenant_id == tenant_id
        and current.knowledge_base_id == knowledge_base_id
        and current.knowledge_base_version_id == knowledge_base_version_id
    ):
        return current.id
    if (
        not current
        or current.tenant_id != tenant_id
        or current.knowledge_base_id != knowledge_base_id
    ):
        return document_id
    cloned = db.exec(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == knowledge_base_version_id,
            KnowledgeDocument.filename == current.filename,
            KnowledgeDocument.file_type == current.file_type,
        )
        .order_by(KnowledgeDocument.created_at.asc())
    ).first()
    return cloned.id if cloned else document_id


def _management_knowledge_base_versions(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
) -> dict[str, KnowledgeBaseVersion]:
    """兼容保留：直接委托给统一的可见性计算（无分支、无副本）。"""
    return visible_knowledge_base_versions(db, tenant_id, agent_id, include_inactive=True)


def _get_knowledge_base(db: Session, tenant_id: str, knowledge_base_id: str) -> KnowledgeBase:
    ensure_tenant(db, tenant_id)
    row = db.get(KnowledgeBase, knowledge_base_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return row


def _knowledge_base_stats(
    db: Session,
    tenant_id: str,
    version_ids: list[str] | None = None,
) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    doc_stmt = select(KnowledgeDocument.knowledge_base_id, func.count(KnowledgeDocument.id)).where(
        KnowledgeDocument.tenant_id == tenant_id
    )
    bucket_stmt = select(KnowledgeBucket.knowledge_base_id, func.count(KnowledgeBucket.id)).where(
        KnowledgeBucket.tenant_id == tenant_id
    )
    chunk_stmt = select(KnowledgeChunk.knowledge_base_id, func.count(KnowledgeChunk.id)).where(
        KnowledgeChunk.tenant_id == tenant_id
    )
    if version_ids is not None:
        doc_stmt = doc_stmt.where(KnowledgeDocument.knowledge_base_version_id.in_(version_ids))
        bucket_stmt = bucket_stmt.where(KnowledgeBucket.knowledge_base_version_id.in_(version_ids))
        chunk_stmt = chunk_stmt.where(KnowledgeChunk.knowledge_base_version_id.in_(version_ids))
    for knowledge_base_id, count in db.exec(
        doc_stmt.group_by(KnowledgeDocument.knowledge_base_id)
    ).all():
        stats.setdefault(knowledge_base_id, {})["document_count"] = int(count or 0)
    for knowledge_base_id, count in db.exec(
        bucket_stmt.group_by(KnowledgeBucket.knowledge_base_id)
    ).all():
        stats.setdefault(knowledge_base_id, {})["bucket_count"] = int(count or 0)
    for knowledge_base_id, count in db.exec(
        chunk_stmt.group_by(KnowledgeChunk.knowledge_base_id)
    ).all():
        stats.setdefault(knowledge_base_id, {})["chunk_count"] = int(count or 0)
    return stats


