from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from sqlmodel import select

from app.agents.branching import (
    is_open_gallery_resource,
    referenced_resource_ids,
)
from app.capabilities.contracts import (
    CapabilityContext,
    GeneralSkillFile,
    GeneralSkillPackage,
    GeneralSkillResourceRef,
    GeneralSkillSummary,
)
from app.db.models import AGENT_SCOPE, AgentProfile, GeneralSkill

LOCAL_GENERAL_SKILL_PROVIDER_ID = "local_general_skill"
LOCAL_GENERAL_SKILL_PACKAGE_CONTRACT = "1"


@dataclass(frozen=True)
class GeneralSkillRuntimeSnapshot:
    """Runner input frozen in memory for the lifetime of one invocation."""

    tenant_id: str
    slug: str
    name: str
    description: str | None
    homepage: str | None
    skill_markdown: str
    skill_files_json: tuple[Mapping[str, object], ...]
    metadata_json: Mapping[str, object] = field(default_factory=dict)
    permissions_json: Mapping[str, object] = field(default_factory=dict)
    runtime_config_json: Mapping[str, object] = field(default_factory=dict)
    status: str = "published"
    package_id: str = ""
    package_version: str = ""
    package_digest: str = ""


class LocalGeneralSkillCatalog:
    """Compatibility Provider backed by existing rows; it never executes code."""

    provider_id = LOCAL_GENERAL_SKILL_PROVIDER_ID

    def __init__(self, db: Any) -> None:
        self.db = db

    def list_published(
        self, context: CapabilityContext
    ) -> Sequence[GeneralSkillSummary]:
        rows = self.db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == context.tenant_id,
                GeneralSkill.status == "published",
            )
        ).all()
        return tuple(
            _summary(row)
            for row in rows
            if self._is_visible(context, row)
        )

    def get_package(
        self,
        context: CapabilityContext,
        resource_ref: GeneralSkillResourceRef,
    ) -> GeneralSkillPackage | None:
        if resource_ref.catalog_binding_id != self.provider_id:
            return None
        row = self.db.get(GeneralSkill, resource_ref.package_id)
        if (
            row is None
            or row.tenant_id != context.tenant_id
            or row.status != "published"
            or not self._is_visible(context, row)
        ):
            return None
        package = package_from_row(row)
        if (
            package.version != resource_ref.version
            or package.digest != resource_ref.digest
            or package.package_contract_version
            != resource_ref.package_contract_version
        ):
            return None
        return package

    def _is_visible(self, context: CapabilityContext, row: GeneralSkill) -> bool:
        agent = self.db.get(AgentProfile, context.agent_id)
        if not agent or agent.tenant_id != context.tenant_id:
            return is_open_gallery_resource(
                self.db, context.tenant_id, "general_skill", row
            )
        # 私有技能归属即生效；广场技能需已引用。
        if getattr(row, "scope", None) == AGENT_SCOPE and row.owner_agent_id == agent.id:
            return True
        if not is_open_gallery_resource(self.db, context.tenant_id, "general_skill", row):
            return False
        return row.id in set(
            referenced_resource_ids(self.db, context.tenant_id, agent.id, "general_skill")
        )


def package_from_row(row: GeneralSkill) -> GeneralSkillPackage:
    files = _files_from_row(row)
    version = _package_version(row)
    canonical = {
        "package_id": row.id,
        "slug": row.slug,
        "version": version,
        "skill_markdown": row.skill_markdown,
        "files": [
            {
                "path": item.path,
                "content": item.content,
                "size": item.size,
                "mime_type": item.mime_type,
            }
            for item in files
        ],
    }
    digest = "sha256:" + sha256(
        json.dumps(
            canonical,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    entrypoint = next(
        (item.path for item in files if item.path.lower() == "skill.md"),
        files[0].path,
    )
    return GeneralSkillPackage(
        package_id=row.id,
        slug=row.slug,
        version=version,
        digest=digest,
        package_contract_version=LOCAL_GENERAL_SKILL_PACKAGE_CONTRACT,
        skill_markdown=row.skill_markdown,
        files=files,
        entrypoint=entrypoint,
    )


def resource_ref_from_row(
    row: GeneralSkill,
    *,
    catalog_binding_id: str = LOCAL_GENERAL_SKILL_PROVIDER_ID,
) -> GeneralSkillResourceRef:
    metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
    configured = metadata.get("provider_resource_ref")
    if (
        catalog_binding_id != LOCAL_GENERAL_SKILL_PROVIDER_ID
        and isinstance(configured, Mapping)
    ):
        package_id = str(configured.get("package_id") or "").strip()
        version = str(configured.get("version") or "").strip()
        digest = str(configured.get("digest") or "").strip()
        contract_version = str(
            configured.get("package_contract_version") or ""
        ).strip()
        if package_id and version and digest and contract_version:
            return GeneralSkillResourceRef(
                catalog_binding_id=catalog_binding_id,
                package_id=package_id,
                version=version,
                digest=digest,
                package_contract_version=contract_version,
            )
    package = package_from_row(row)
    return GeneralSkillResourceRef(
        catalog_binding_id=catalog_binding_id,
        package_id=package.package_id,
        version=package.version,
        digest=package.digest,
        package_contract_version=package.package_contract_version,
    )


def runtime_snapshot_from_package(
    row: GeneralSkill,
    package: GeneralSkillPackage,
) -> GeneralSkillRuntimeSnapshot:
    files = tuple(
        MappingProxyType(
            {
                "path": item.path,
                "content": item.content,
                "size": item.size,
                "mime_type": item.mime_type,
            }
        )
        for item in package.files
    )
    return GeneralSkillRuntimeSnapshot(
        tenant_id=row.tenant_id,
        slug=package.slug,
        name=row.name,
        description=row.description,
        homepage=row.homepage,
        skill_markdown=package.skill_markdown,
        skill_files_json=files,
        metadata_json=MappingProxyType(dict(row.metadata_json or {})),
        permissions_json=MappingProxyType(dict(row.permissions_json or {})),
        runtime_config_json=MappingProxyType(dict(row.runtime_config_json or {})),
        status=row.status,
        package_id=package.package_id,
        package_version=package.version,
        package_digest=package.digest,
    )


def local_runtime_snapshot(row: GeneralSkill) -> GeneralSkillRuntimeSnapshot:
    return runtime_snapshot_from_package(row, package_from_row(row))


def _summary(row: GeneralSkill) -> GeneralSkillSummary:
    package = package_from_row(row)
    return GeneralSkillSummary(
        slug=row.slug,
        version=package.version,
        name=row.name,
        package_id=package.package_id,
        digest=package.digest,
    )


def _package_version(row: GeneralSkill) -> str:
    metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
    configured = str(metadata.get("version") or "").strip()
    if configured:
        return configured
    updated_at = getattr(row, "updated_at", None)
    return updated_at.isoformat() if updated_at is not None else "legacy-v1"


def _files_from_row(row: GeneralSkill) -> tuple[GeneralSkillFile, ...]:
    raw_files = row.skill_files_json
    files: list[GeneralSkillFile] = []
    if isinstance(raw_files, Sequence) and not isinstance(raw_files, (str, bytes)):
        for raw in raw_files:
            if not isinstance(raw, Mapping):
                continue
            path = str(raw.get("path") or "").strip()
            if not path:
                continue
            content = str(raw.get("content") or "")
            size = raw.get("size")
            files.append(
                GeneralSkillFile(
                    path=path,
                    content=content,
                    size=int(size) if isinstance(size, (int, float)) else len(content.encode()),
                    mime_type=str(raw.get("mime_type")) if raw.get("mime_type") else None,
                )
            )
    if files:
        return tuple(files)
    markdown = str(row.skill_markdown or "")
    return (
        GeneralSkillFile(
            path="SKILL.md",
            content=markdown,
            size=len(markdown.encode()),
            mime_type="text/markdown",
        ),
    )
