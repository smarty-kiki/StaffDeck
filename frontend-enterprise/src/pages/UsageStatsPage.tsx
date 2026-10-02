import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  Button as UIButton,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Skeleton,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  notify,
} from '@/components/ui';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { StatCard } from '@/components/StatCard';
import { cn } from '@/lib/utils';
import { api, TENANT_ID } from '../api/client';
import {
  USAGE_RANGE_PRESETS,
  barHeightPercent,
  breakdownLabel,
  formatTokenCount,
  localTimezoneOffsetMinutes,
  rangeForPreset,
  sortBreakdown,
  type UsageBreakdownRow,
  type UsageSortKey,
  type UsageStats,
} from './usageStats';

type BreakdownTab = 'by_agent' | 'by_user' | 'by_model' | 'by_operation';

const BREAKDOWN_TABS: { value: BreakdownTab; label: string; emptyText: string }[] = [
  { value: 'by_agent', label: '数字员工', emptyText: '该区间内没有数字员工用量' },
  { value: 'by_user', label: '用户', emptyText: '该区间内没有用户用量' },
  { value: 'by_model', label: '模型', emptyText: '该区间内没有模型用量' },
  { value: 'by_operation', label: '调用场景', emptyText: '该区间内没有调用场景数据' },
];

const CHART_HEIGHT = 168;
/** 当日没有调用时留一段灰色基线，让「空档」和「用量极小」一眼能分开。 */
const CHART_EMPTY_STUB_PERCENT = 2;
const RANGE_CONTROL_CLASS = 'h-[28px] rounded-[8px] px-[12px] text-[12px]';

export default function UsageStatsPage() {
  const [stats, setStats] = useState<UsageStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [preset, setPreset] = useState<number>(30);
  const [range, setRange] = useState(() => rangeForPreset(30));
  const [sortKey, setSortKey] = useState<UsageSortKey>('total_tokens');
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc');

  const load = useCallback(async (window: { start: string; end: string }) => {
    setLoading(true);
    try {
      const query = new URLSearchParams({
        tenant_id: TENANT_ID,
        start: window.start,
        end: window.end,
        tz_offset_minutes: String(localTimezoneOffsetMinutes()),
      });
      setStats(await api.get<UsageStats>(`/api/enterprise/usage/stats?${query.toString()}`));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(range);
  }, [load, range]);

  function applyPreset(days: number) {
    setPreset(days);
    setRange(rangeForPreset(days));
  }

  function applyCustomDay(edge: 'start' | 'end', value: string) {
    if (!value) return;
    setPreset(0);
    setRange((current) => {
      const next = { ...current, [edge]: value };
      // 起止颠倒时把另一端让开，避免查出空区间
      if (edge === 'start' && next.start > next.end) next.end = next.start;
      if (edge === 'end' && next.end < next.start) next.start = next.end;
      return next;
    });
  }

  function toggleSort(key: UsageSortKey) {
    if (key === sortKey) {
      setSortDirection((current) => (current === 'desc' ? 'asc' : 'desc'));
      return;
    }
    setSortKey(key);
    setSortDirection('desc');
  }

  const dailyMax = useMemo(
    () => Math.max(0, ...(stats?.daily ?? []).map((point) => point.total_tokens)),
    [stats],
  );
  const totalTokens = stats?.totals.total_tokens ?? 0;

  const columns = useMemo(() => buildBreakdownColumns(sortKey, sortDirection, toggleSort, totalTokens), [
    sortKey,
    sortDirection,
    totalTokens,
  ]);

  return (
    <>
      <div className="page-title">
        <h3>用量统计</h3>
        <UIButton variant="outline" onClick={() => void load(range)} disabled={loading}>
          刷新
        </UIButton>
      </div>

      <Card className="data-card">
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-[12px]">
          <CardTitle>统计区间</CardTitle>
          <div className="flex flex-wrap items-center gap-[8px]">
            {USAGE_RANGE_PRESETS.map((days) => (
              <UIButton
                key={days}
                variant={preset === days ? 'default' : 'outline'}
                size="sm"
                className={RANGE_CONTROL_CLASS}
                onClick={() => applyPreset(days)}
              >
                {`近 ${days} 天`}
              </UIButton>
            ))}
            <Input
              type="date"
              aria-label="开始日期"
              className={cn(RANGE_CONTROL_CLASS, 'w-[148px]')}
              value={range.start}
              max={range.end}
              onChange={(event) => applyCustomDay('start', event.target.value)}
            />
            <span className="text-[12px] text-[#858b9c]">至</span>
            <Input
              type="date"
              aria-label="结束日期"
              className={cn(RANGE_CONTROL_CLASS, 'w-[148px]')}
              value={range.end}
              min={range.start}
              onChange={(event) => applyCustomDay('end', event.target.value)}
            />
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-[18px]">
          {stats && stats.scope === 'self' && (
            <p className="text-[12px] text-[#858b9c]">当前账号为普通成员，这里只统计你自己的用量。</p>
          )}
          <div className="flex flex-wrap items-stretch gap-[20px]" aria-label="用量统计">
            <StatCard label="调用次数" value={stats ? formatTokenCount(stats.totals.calls) : '-'} />
            <StatCard label="总 Token" value={stats ? formatTokenCount(totalTokens) : '-'} />
            <StatCard
              label="输入 Token"
              value={stats ? formatTokenCount(stats.totals.input_tokens) : '-'}
            />
            <StatCard
              label="输出 Token"
              value={stats ? formatTokenCount(stats.totals.output_tokens) : '-'}
            />
          </div>
        </CardContent>
      </Card>

      <Card className="data-card">
        <CardHeader>
          <CardTitle>每日用量</CardTitle>
        </CardHeader>
        <CardContent>
          {loading && !stats ? (
            <Skeleton className="h-[168px] w-full rounded-[10px]" />
          ) : dailyMax > 0 ? (
            <div className="overflow-x-auto pb-[4px]">
              <div className="flex min-w-full items-end gap-[3px]" style={{ height: CHART_HEIGHT }}>
                {stats?.daily.map((point) => {
                  const percent = barHeightPercent(point.total_tokens, dailyMax);
                  const isEmpty = percent === 0;
                  return (
                    <div
                      key={point.date}
                      className="group flex h-full min-w-[8px] flex-1 flex-col justify-end"
                      title={`${point.date} · ${formatTokenCount(point.calls)} 次 · ${formatTokenCount(point.total_tokens)} Token`}
                    >
                      <div
                        className={cn(
                          'w-full rounded-t-[3px]',
                          isEmpty
                            ? 'bg-[#e9e9e9]'
                            : 'bg-[#282931] transition-colors group-hover:bg-[#18181a]',
                        )}
                        style={{ height: `${isEmpty ? CHART_EMPTY_STUB_PERCENT : percent}%` }}
                      />
                    </div>
                  );
                })}
              </div>
              <div className="mt-[6px] flex items-center justify-between text-[11px] text-[#858b9c]">
                <span>{stats?.daily[0]?.date ?? range.start}</span>
                <span>{stats?.daily[stats.daily.length - 1]?.date ?? range.end}</span>
              </div>
            </div>
          ) : (
            <p className="py-[48px] text-center text-[13px] text-[#858b9c]">该区间没有大模型调用记录</p>
          )}
        </CardContent>
      </Card>

      <Card className="data-card">
        <CardHeader>
          <CardTitle>用量明细</CardTitle>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="by_agent">
            <TabsList>
              {BREAKDOWN_TABS.map((tab) => (
                <TabsTrigger key={tab.value} value={tab.value}>
                  {tab.label}
                </TabsTrigger>
              ))}
            </TabsList>
            {BREAKDOWN_TABS.map((tab) => (
              <TabsContent key={tab.value} value={tab.value}>
                <div className="overflow-x-auto">
                  <DataTable
                    aria-label={`按${tab.label}统计的用量`}
                    size="compact"
                    columns={columns}
                    data={sortBreakdown(stats?.[tab.value] ?? [], sortKey, sortDirection)}
                    rowKey={(row) => row.key}
                    loading={loading}
                    emptyText={tab.emptyText}
                    className="min-w-[820px]"
                  />
                </div>
              </TabsContent>
            ))}
          </Tabs>
        </CardContent>
      </Card>
    </>
  );
}

function buildBreakdownColumns(
  sortKey: UsageSortKey,
  sortDirection: 'asc' | 'desc',
  onSort: (key: UsageSortKey) => void,
  totalTokens: number,
): DataTableColumn<UsageBreakdownRow>[] {
  const sortableTitle = (key: UsageSortKey, label: string) => (
    <button
      type="button"
      className="inline-flex items-center gap-[4px] transition-colors hover:text-[#18181a]"
      onClick={() => onSort(key)}
    >
      {label}
      {sortKey === key && <span aria-hidden="true">{sortDirection === 'desc' ? '↓' : '↑'}</span>}
    </button>
  );

  return [
    {
      key: 'name',
      title: '名称',
      width: 260,
      render: (row) => (
        <span className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate text-[#18181a]" title={breakdownLabel(row)}>
            {breakdownLabel(row)}
          </span>
          {row.name && row.name !== row.key && (
            <span className="truncate text-[11px] text-[#a7adbb]" title={row.key}>
              {row.key}
            </span>
          )}
        </span>
      ),
    },
    {
      key: 'calls',
      title: sortableTitle('calls', '调用次数'),
      width: 110,
      align: 'right',
      render: (row) => formatTokenCount(row.calls),
    },
    {
      key: 'input_tokens',
      title: sortableTitle('input_tokens', '输入'),
      width: 120,
      align: 'right',
      render: (row) => formatTokenCount(row.input_tokens),
    },
    {
      key: 'output_tokens',
      title: sortableTitle('output_tokens', '输出'),
      width: 120,
      align: 'right',
      render: (row) => formatTokenCount(row.output_tokens),
    },
    {
      key: 'total_tokens',
      title: sortableTitle('total_tokens', '合计 Token'),
      width: 140,
      align: 'right',
      render: (row) => (
        <span className="font-medium text-[#18181a]">{formatTokenCount(row.total_tokens)}</span>
      ),
    },
    {
      key: 'share',
      title: '占比',
      width: 160,
      render: (row) => {
        const share = totalTokens > 0 ? (row.total_tokens / totalTokens) * 100 : 0;
        return (
          <span className="flex items-center gap-[8px]">
            <span className="block h-[4px] w-[72px] overflow-hidden rounded-[90px] bg-[#e9e9e9]">
              <span
                className="block h-full rounded-[90px] bg-[#282931]"
                style={{ width: `${Math.min(100, Math.max(share > 0 ? 2 : 0, share))}%` }}
              />
            </span>
            <span className="text-[12px] text-[#858b9c]">{`${share.toFixed(1)}%`}</span>
          </span>
        );
      },
    },
  ];
}
