"""内置演示（预置）资源的清单。

预置内容由两条 seed 流程在首次启动时写入：

1. `staffdeck_seed.py` 的行业演示包（`seed_fixtures/*.json`）—— 十名预置员工及
   其知识库 / SOP / 通用技能 / 工具；
2. `seed.py` 的 demo 技能 / 工具 / MCP 工具集（`DEMO_SKILL_CONTENTS` 等）。

两条流程都是一次性初始化，但资源一旦以 `scope='gallery'` 发布到广场，就和播种它的
员工脱钩：员工被删除后资源仍留在广场，而带一次性标记的 seed 既不会补种也不会清理，
于是广场里就攒下一批没人能用的孤儿资源。

归属化改造把资源表上的 seed 标记清掉了，于是只剩两条线索能和种子定义对齐：资源自身的
「业务键」（判断它是不是预置内容），以及演示包自带的引用表（判断这份预置内容原本归属
哪个预置员工）。本模块把这两条线索都抽出来，供生命周期清理判定「谁是孤儿」。
"""

from __future__ import annotations

import json
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

_FIXTURE_DIR = Path(__file__).resolve().parent / "seed_fixtures"
_GALLERY_FIXTURE_NAMES = (
    "staffdeck_admin_gallery_seed.json",
    "staffdeck_expanded_gallery_seed.json",
)


@cache
def _gallery_fixture_rows(key: str) -> tuple[dict[str, Any], ...]:
    """读行业演示包里某类记录（两个 fixture 合并）。结果缓存 —— fixture 有数兆。"""
    rows: list[dict[str, Any]] = []
    for name in _GALLERY_FIXTURE_NAMES:
        path = _FIXTURE_DIR / name
        if not path.is_file():
            continue
        fixture = json.loads(path.read_text(encoding="utf-8"))
        rows.extend(row for row in fixture.get(key, []) if isinstance(row, dict))
    return tuple(rows)


def _demo_seed_module():
    """延迟导入 `app.db.seed`。它反向依赖 `app.agents.branching`，模块级导入会成环。"""
    from app.db import seed as seed_module

    return seed_module


@lru_cache(maxsize=1)
def preset_knowledge_base_aliases() -> dict[str, str]:
    """子文档派生的知识库 id → 其来源知识库 id。

    历史的知识库 schema 迁移会给每个知识文档单独建一个知识库，id 由文档 id 派生
    （`kb_doc_<文档 id>`，见 `database.py` 的 `_document_knowledge_base_id`）。演示包的
    文档同样会被这样派生，于是同一份预置知识库会以「原 id + 派生 id」两份留在库里，
    清理时两份都得认，否则就会漏掉一份。
    """
    aliases: dict[str, str] = {}
    for doc in _gallery_fixture_rows("knowledge_documents"):
        source_id = str(doc.get("knowledge_base_id") or "").strip()
        document_id = str(doc.get("id") or "").strip()
        if source_id and document_id:
            aliases[f"kb_doc_{document_id}"] = source_id
    return aliases


@lru_cache(maxsize=1)
def preset_resource_keys() -> dict[str, frozenset[str]]:
    """预置演示资源的业务键：`{资源类型: 业务键}`。

    业务键取各表唯一约束用的那一列：skill→skill_id、general_skill→slug、
    tool→name、knowledge_base→id。
    """
    seed_module = _demo_seed_module()

    skills = {str(row.get("skill_id") or "").strip() for row in _gallery_fixture_rows("skills")}
    skills |= {
        str(item.get("skill_id") or "").strip()
        for item in seed_module.DEMO_SKILL_CONTENTS
        if isinstance(item, dict)
    }

    general_skills = {
        str(row.get("slug") or "").strip() for row in _gallery_fixture_rows("general_skills")
    }

    tools = {str(row.get("name") or "").strip() for row in _gallery_fixture_rows("tools")}
    tools |= {
        str(item.get("name") or "").strip()
        for item in seed_module.DEMO_TOOLS
        if isinstance(item, dict)
    }
    for server in seed_module.MCP_SERVERS:
        if not isinstance(server, dict):
            continue
        server_name = str(server.get("name") or "")
        for tool in seed_module.MCP_SERVER_TOOLS.get(server_name, []):
            if isinstance(tool, dict) and tool.get("leaf"):
                tools.add(f"{server_name}.{tool['leaf']}")

    knowledge_bases = {
        str(row.get("id") or "").strip() for row in _gallery_fixture_rows("knowledge_bases")
    }
    knowledge_bases |= set(preset_knowledge_base_aliases())

    return {
        "skill": frozenset(skills - {""}),
        "general_skill": frozenset(general_skills - {""}),
        "tool": frozenset(tools - {""}),
        "knowledge_base": frozenset(knowledge_bases - {""}),
    }


@lru_cache(maxsize=1)
def preset_agent_ids() -> frozenset[str]:
    """行业演示包里的预置员工 id。"""
    return frozenset(
        str(row.get("id") or "").strip()
        for row in _gallery_fixture_rows("agent_profiles")
        if row.get("id")
    )


@lru_cache(maxsize=1)
def preset_resource_owners() -> dict[tuple[str, str], frozenset[str]]:
    """预置资源 → 配了它的预置员工 id 集合，键为 `(资源类型, 资源行 id)`。

    演示包自带一张引用表（`agent_resource_references`），记录了「哪个预置员工配了哪些
    预置资源」——这是资源归属的原始事实。归属化改造把资源表上的 seed 标记与归属列都
    清掉了，这张表就成了「这条广场资源到底属于谁、它的主人在不在」唯一可用的锚点。
    """
    owners: dict[tuple[str, str], set[str]] = {}
    for row in _gallery_fixture_rows("agent_resource_references"):
        resource_type = str(row.get("resource_type") or "").strip()
        resource_id = str(row.get("resource_id") or "").strip()
        agent_id = str(row.get("agent_id") or "").strip()
        if not (resource_type and resource_id and agent_id):
            continue
        owners.setdefault((resource_type, resource_id), set()).add(agent_id)
    # 文档派生的知识库是同一份预置知识库的第二种落库形态，归属随来源知识库。
    for alias, source_id in preset_knowledge_base_aliases().items():
        if ("knowledge_base", alias) in owners:
            continue
        source_owners = owners.get(("knowledge_base", source_id))
        if source_owners:
            owners[("knowledge_base", alias)] = set(source_owners)
    return {key: frozenset(value) for key, value in owners.items()}


def preset_resource_key(resource_type: str, row: object) -> str:
    """资源的业务键，口径与 `preset_resource_keys` 一致。"""
    if resource_type == "skill":
        return str(getattr(row, "skill_id", "") or "").strip()
    if resource_type == "general_skill":
        return str(getattr(row, "slug", "") or "").strip()
    if resource_type == "tool":
        return str(getattr(row, "name", "") or "").strip()
    return str(getattr(row, "id", "") or "").strip()


def is_preset_resource(resource_type: str, row: object) -> bool:
    """这条资源是不是随安装包附带的预置演示内容。"""
    return preset_resource_key(resource_type, row) in preset_resource_keys().get(
        resource_type, frozenset()
    )


__all__ = [
    "is_preset_resource",
    "preset_agent_ids",
    "preset_knowledge_base_aliases",
    "preset_resource_key",
    "preset_resource_keys",
    "preset_resource_owners",
]
