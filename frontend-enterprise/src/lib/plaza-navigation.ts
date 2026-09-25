/**
 * 开放广场与各模块之间的跳转约定。
 *
 * 广场不是一条「员工」记录，而是一个资源视角：模块的广场列表、模块自己的管理页、
 * 模块的新建页三者之间要能互相返回，且从广场新建时不能悄悄落回某个员工的私有范围。
 * 这里把这两条约定集中起来，免得每个模块各写一份、各漏一处。
 */

export type ReturnTarget = { path: string; label: string };

/** 广场模块列表页前缀（/enterprise/platform/:kind）。 */
const PLAZA_ROOT = '/enterprise/platform';

/**
 * 广场视角标记。带在 URL 上让目标页忽略当前选中的员工 —— 员工范围存在
 * localStorage 里，从广场进新建页时它可能还留着上一次选的员工，不显式覆盖就会
 * 把「开放内容」建成某个员工的私有资源。
 */
export const PLAZA_SCOPE_VALUE = 'gallery';

export function isPlazaScopeValue(value: string | null | undefined): boolean {
  return value === PLAZA_SCOPE_VALUE;
}

/** 站内路径判定：只接受 /enterprise 下的路径，避免 from 被拼成外部地址。 */
function isEnterpriseInternalPath(target: string): boolean {
  return target.startsWith('/enterprise/') && !target.startsWith('/enterprise//');
}

/** /enterprise/platform 及其子路由（各模块的广场列表）。 */
export function isPlazaModulePath(target: string): boolean {
  return target === PLAZA_ROOT || target.startsWith(`${PLAZA_ROOT}/`);
}

/**
 * 「返回」目标：跟着入口走。
 *
 * 从广场进来就回广场列表（文案「返回开放广场」），否则回模块自己的管理页 ——
 * 写死返回管理页会把从广场进来的用户丢出广场，链路断掉。非法来源一律退回 fallback。
 */
export function resolveReturnTarget(
  from: string | null | undefined,
  fallback: ReturnTarget,
): ReturnTarget {
  const target = (from ?? '').trim();
  if (!isEnterpriseInternalPath(target)) return fallback;
  return { path: target, label: isPlazaModulePath(target) ? '返回开放广场' : fallback.label };
}

/** 支持「创建开放 XX」的广场模块（数字员工没有对应概念，由员工页自己发布）。 */
export type PlazaCreateKind = 'knowledge' | 'general-skills' | 'skills' | 'tools';

const PLAZA_CREATE_LABEL: Record<PlazaCreateKind, string> = {
  knowledge: '创建开放知识库',
  'general-skills': '创建开放技能',
  skills: '创建开放 SOP',
  tools: '创建开放工具',
};

export function plazaCreateLabel(kind: PlazaCreateKind): string {
  return PLAZA_CREATE_LABEL[kind];
}

/**
 * 广场视角下的新建页标题。
 *
 * 入口按钮写的是「创建开放 XX」，落地页却还挂着「新建 XX」—— 用户一路点下来会觉得
 * 自己进错了页。标题统一走这里取词，两边由同一份映射产出，不会各改各的。
 */
export function resolveCreatePageTitle(
  kind: PlazaCreateKind,
  isPlazaScope: boolean,
  fallback: string,
): string {
  return isPlazaScope ? plazaCreateLabel(kind) : fallback;
}

export function isPlazaCreateKind(kind: string): kind is PlazaCreateKind {
  return Object.prototype.hasOwnProperty.call(PLAZA_CREATE_LABEL, kind);
}

/**
 * 「创建开放 XX」的目标地址：直接落在广场视角的新建流程里。
 *
 * scope=gallery 让目标页忽略当前选中的员工；from 让新建页的「返回」回到广场列表。
 * SOP 只带 mode=create，workspace_id 由蒸馏页自己补齐。
 */
export function plazaCreatePath(kind: PlazaCreateKind, from: string): string {
  const query = new URLSearchParams({ scope: PLAZA_SCOPE_VALUE, from });
  if (kind === 'knowledge') return `/enterprise/knowledge/new?${query.toString()}`;
  if (kind === 'general-skills') return `/enterprise/general-skills/new?${query.toString()}`;
  if (kind === 'tools') return `/enterprise/tools/new?${query.toString()}`;
  query.set('mode', 'create');
  return `/enterprise/skills/distill?${query.toString()}`;
}
