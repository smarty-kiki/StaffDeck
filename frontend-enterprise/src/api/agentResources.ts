import type { AgentResourceReferenceRead, AgentResourceType } from '../types';
import { TENANT_ID, api } from './client';

/**
 * 员工对广场资源的**引用**。
 *
 * 引用 = 插一行、取消引用 = 删一行；没有状态字段可改，也从不复制资源内容。
 * 资源只有一份、归属其作者 —— 作者更新后，所有引用者立刻看到最新内容。
 */
function tenantQuery(): string {
  return `tenant_id=${encodeURIComponent(TENANT_ID)}`;
}

/** 该员工引用的全部广场资源。 */
export async function listAgentReferences(agentId: string): Promise<AgentResourceReferenceRead[]> {
  return api.get<AgentResourceReferenceRead[]>(
    `/api/enterprise/agents/${encodeURIComponent(agentId)}/resources?${tenantQuery()}`,
  );
}

/**
 * 该员工**引用**（而非自有）的某一类资源 id 集合。
 *
 * 员工可见集 = 自有的（归属即生效）∪ 已引用的广场资源。列表接口只返回可见集，
 * 不区分来源，所以「取消引用」这个动作必须先问引用表才知道该给哪几行挂。
 */
export async function referencedResourceIdSet(
  agentId: string,
  resourceType: AgentResourceType,
): Promise<Set<string>> {
  const rows = await listAgentReferences(agentId);
  return new Set(
    rows.filter((row) => row.resource_type === resourceType).map((row) => row.resource_id),
  );
}

/** 引用一个广场资源（幂等：重复引用不报错）。 */
export async function referencePlazaResource(
  agentId: string,
  resourceType: AgentResourceType,
  resourceId: string,
): Promise<AgentResourceReferenceRead> {
  return api.post<AgentResourceReferenceRead>(
    `/api/enterprise/agents/${encodeURIComponent(agentId)}/resources:reference?${tenantQuery()}`,
    { resource_type: resourceType, resource_id: resourceId },
  );
}

/** 取消引用（只删引用行，不动资源本身，也不影响其他员工的引用）。 */
export async function unreferencePlazaResource(
  agentId: string,
  resourceType: AgentResourceType,
  resourceId: string,
): Promise<{ status: string }> {
  return api.delete<{ status: string }>(
    `/api/enterprise/agents/${encodeURIComponent(agentId)}/resources/${resourceType}/${encodeURIComponent(resourceId)}?${tenantQuery()}`,
  );
}

/**
 * 把**某一类型**的引用集合整体替换成 `resourceIds`（其他类型的引用保持不变）。
 *
 * 后端 `PUT /resources` 是整体覆盖语义，因此这里先把当前引用读出来，再合并写回。
 */
export async function replaceReferencedResources(
  agentId: string,
  resourceType: AgentResourceType,
  resourceIds: string[],
): Promise<AgentResourceReferenceRead[]> {
  const current = await listAgentReferences(agentId);
  const resources = [
    ...current
      .filter((row) => row.resource_type !== resourceType)
      .map((row) => ({ resource_type: row.resource_type, resource_id: row.resource_id })),
    ...resourceIds.map((resourceId) => ({ resource_type: resourceType, resource_id: resourceId })),
  ];
  return api.put<AgentResourceReferenceRead[]>(
    `/api/enterprise/agents/${encodeURIComponent(agentId)}/resources`,
    { tenant_id: TENANT_ID, resources },
  );
}
