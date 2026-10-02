import { describe, expect, it } from 'vitest';

import {
  UNATTRIBUTED_KEY,
  barHeightPercent,
  breakdownLabel,
  formatCompactTokenCount,
  formatTokenCount,
  localDayIso,
  localTimezoneOffsetMinutes,
  rangeForPreset,
  shiftIsoDay,
  sortBreakdown,
  type UsageBreakdownRow,
} from './usageStats';

function row(key: string, overrides: Partial<UsageBreakdownRow> = {}): UsageBreakdownRow {
  return {
    key,
    name: null,
    calls: 1,
    input_tokens: 10,
    output_tokens: 5,
    total_tokens: 15,
    cached_input_tokens: 0,
    ...overrides,
  };
}

describe('usage stats range helpers', () => {
  it('builds an inclusive preset range from a local reference day', () => {
    const reference = new Date(2026, 9, 2, 23, 30);
    expect(rangeForPreset(7, reference)).toEqual({ start: '2026-09-26', end: '2026-10-02' });
    expect(rangeForPreset(1, reference)).toEqual({ start: '2026-10-02', end: '2026-10-02' });
    expect(rangeForPreset(30, reference)).toEqual({ start: '2026-09-03', end: '2026-10-02' });
  });

  it('formats local day without shifting into UTC', () => {
    // toISOString() 会先转 UTC，时区偏移下可能落到前一天/后一天；
    // localDayIso 必须始终返回用户本地的那个日期。
    expect(localDayIso(new Date(2026, 9, 2, 23, 30))).toBe('2026-10-02');
    expect(localDayIso(new Date(2026, 9, 2, 0, 30))).toBe('2026-10-02');
    expect(localDayIso(new Date(2026, 0, 1, 0, 0))).toBe('2026-01-01');
  });

  it('shifts ISO days across month and year boundaries', () => {
    expect(shiftIsoDay('2026-10-02', -1)).toBe('2026-10-01');
    expect(shiftIsoDay('2026-03-01', -1)).toBe('2026-02-28');
    expect(shiftIsoDay('2026-01-01', -1)).toBe('2025-12-31');
    expect(shiftIsoDay('not-a-date', -1)).toBe('not-a-date');
  });

  it('reports the local UTC offset in minutes (east positive)', () => {
    const offset = localTimezoneOffsetMinutes(new Date(2026, 9, 2, 12, 0));
    expect(Number.isInteger(offset)).toBe(true);
    expect(offset).toBe(-new Date(2026, 9, 2, 12, 0).getTimezoneOffset());
  });
});

describe('usage stats number formatting', () => {
  it('keeps exact thousands separators for cards and tables', () => {
    expect(formatTokenCount(689868)).toBe('689,868');
    expect(formatTokenCount(0)).toBe('0');
    expect(formatTokenCount(Number.NaN)).toBe('0');
  });

  it('uses locale-independent K/M/B units in compact form', () => {
    // 单位不随语言变，避免中文「万」被翻译后倍数算错
    expect(formatCompactTokenCount(689868)).toBe('689.9K');
    expect(formatCompactTokenCount(2_500_000)).toBe('2.50M');
    expect(formatCompactTokenCount(3_000_000_000)).toBe('3.00B');
    expect(formatCompactTokenCount(999)).toBe('999');
    expect(formatCompactTokenCount(0)).toBe('0');
  });

  it('never scales a bar past 100% and keeps non-zero bars visible', () => {
    expect(barHeightPercent(0, 100)).toBe(0);
    expect(barHeightPercent(50, 100)).toBe(50);
    expect(barHeightPercent(100, 100)).toBe(100);
    // 有量但极小的柱子也要看得见，否则一整天像没有数据
    expect(barHeightPercent(1, 1_000_000)).toBe(2);
    expect(barHeightPercent(5, 0)).toBe(0);
  });
});

describe('usage stats breakdown sorting and labels', () => {
  const rows = [
    row('agent_a', { total_tokens: 100, calls: 3 }),
    row('agent_b', { total_tokens: 900, calls: 1 }),
    row('agent_c', { total_tokens: 500, calls: 9 }),
  ];

  it('defaults to token descending', () => {
    expect(sortBreakdown(rows).map((item) => item.key)).toEqual(['agent_b', 'agent_c', 'agent_a']);
  });

  it('supports other metrics and directions without mutating the input', () => {
    expect(sortBreakdown(rows, 'calls', 'desc').map((item) => item.key)).toEqual([
      'agent_c',
      'agent_a',
      'agent_b',
    ]);
    expect(sortBreakdown(rows, 'total_tokens', 'asc').map((item) => item.key)).toEqual([
      'agent_a',
      'agent_c',
      'agent_b',
    ]);
    expect(rows.map((item) => item.key)).toEqual(['agent_a', 'agent_b', 'agent_c']);
  });

  it('breaks ties by key so ordering stays stable', () => {
    const tied = [row('b', { total_tokens: 10 }), row('a', { total_tokens: 10 })];
    expect(sortBreakdown(tied).map((item) => item.key)).toEqual(['a', 'b']);
  });

  it('falls back to the raw key when no display name is resolved', () => {
    expect(breakdownLabel(row('agent_a'))).toBe('agent_a');
    expect(breakdownLabel(row('agent_a', { name: '客服助手' }))).toBe('客服助手');
  });

  it('labels rows that could not be attributed instead of leaking the sentinel', () => {
    expect(breakdownLabel(row(UNATTRIBUTED_KEY))).toBe('未归因');
    // 有名字时以名字为准，哨兵只影响没有名字的行
    expect(breakdownLabel(row(UNATTRIBUTED_KEY, { name: '渠道会话' }))).toBe('渠道会话');
  });
});
