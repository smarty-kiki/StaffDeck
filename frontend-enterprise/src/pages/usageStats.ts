/** 用量统计的纯逻辑：类型、区间换算、数字格式化、排序。
 *
 * 抽出来是为了能单独跑用例 —— 页面里只保留渲染。
 */

export type UsageTotals = {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens: number;
  avg_duration_ms: number;
};

export type UsageDailyPoint = {
  date: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens: number;
};

export type UsageBreakdownRow = {
  key: string;
  name?: string | null;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens: number;
};

export type UsageStats = {
  range: { start: string; end: string; days: number; timezone_offset_minutes: number };
  /** tenant = 管理员看全租户；self = 成员只看自己 */
  scope: 'tenant' | 'self';
  totals: UsageTotals;
  daily: UsageDailyPoint[];
  by_agent: UsageBreakdownRow[];
  by_user: UsageBreakdownRow[];
  by_model: UsageBreakdownRow[];
  by_operation: UsageBreakdownRow[];
};

/** 区间快捷档位（天）。0 表示自定义。 */
export const USAGE_RANGE_PRESETS = [7, 30, 90] as const;

const MS_PER_DAY = 24 * 60 * 60 * 1000;

/** 本地「今天」的 ISO 日期（不能用 toISOString：那是 UTC，跨时区会差一天）。 */
export function localDayIso(reference: Date = new Date()): string {
  const year = reference.getFullYear();
  const month = String(reference.getMonth() + 1).padStart(2, '0');
  const day = String(reference.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/** 浏览器本地相对 UTC 的偏移分钟数（东八区 = 480），与后端口径一致。 */
export function localTimezoneOffsetMinutes(reference: Date = new Date()): number {
  return -reference.getTimezoneOffset();
}

/** 以 ISO 日期加减天数；用 UTC 解析避免夏令时把日期推错。 */
export function shiftIsoDay(iso: string, days: number): string {
  const parsed = Date.parse(`${iso}T00:00:00Z`);
  if (Number.isNaN(parsed)) return iso;
  return new Date(parsed + days * MS_PER_DAY).toISOString().slice(0, 10);
}

/** 快捷档位换算成 [start, end]，含头含尾共 days 天。 */
export function rangeForPreset(days: number, reference: Date = new Date()): {
  start: string;
  end: string;
} {
  const end = localDayIso(reference);
  return { start: shiftIsoDay(end, -(Math.max(1, days) - 1)), end };
}

/** 精确千分位，用于卡片和表格（不缩写，避免"到底多少"的疑问）。 */
export function formatTokenCount(value: number): string {
  if (!Number.isFinite(value)) return '0';
  return Math.round(value).toLocaleString('en-US');
}

/** 柱高百分比；0 值返回 0，由页面渲染成灰色基线，避免除零也避免与"用量极小"混淆。 */
export function barHeightPercent(value: number, max: number): number {
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0) return 0;
  if (value <= 0) return 0;
  // 有值的柱子至少给 2%，否则一根线看不出差别
  return Math.max(2, Math.round((value / max) * 100));
}

export type UsageSortKey = 'calls' | 'total_tokens' | 'input_tokens' | 'output_tokens';

/** 后端表示「这条用量没能归因到具体用户/员工」的哨兵 key。 */
export const UNATTRIBUTED_KEY = '__unattributed__';

/** 按指标排序，默认 token 降序；同名次回落到 key，保证顺序稳定。 */
export function sortBreakdown(
  rows: UsageBreakdownRow[],
  key: UsageSortKey = 'total_tokens',
  direction: 'asc' | 'desc' = 'desc',
): UsageBreakdownRow[] {
  const factor = direction === 'asc' ? 1 : -1;
  return [...rows].sort((left, right) => {
    const delta = (left[key] - right[key]) * factor;
    if (delta !== 0) return delta;
    return left.key.localeCompare(right.key);
  });
}

/**
 * 明细行的显示名：优先用后端解析出的名字，其次原始 id。
 * 未归因（渠道会话、无用户上下文）给一个明确的占位，不要露出内部哨兵值。
 */
export function breakdownLabel(row: UsageBreakdownRow): string {
  if (row.name) return row.name;
  if (row.key === UNATTRIBUTED_KEY) return '未归因';
  return row.key;
}
