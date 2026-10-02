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
import { api, TENANT_ID } from '../api/client';
import {
  USAGE_RANGE_PRESETS,
  barHeightPercent,
  breakdownLabel,
  formatCompactTokenCount,
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
                className="h-[30px] rounded-[8px] px-[14px] text-[12px]"
                onClick={() => applyPreset(days)}
              >
                {`近 ${days} 天`}
              </UIButton>
            ))}
            <Input
              type="date"
              aria-label="开始日期"
              className="h-[30px] w-[148px] text-[12px]"
              value={range.start}
              max={range.end}
              onChange={(event) => applyCustomDay('start', event.target.value)}
            />
            <span className="text-[12px] text-[#8b94aa]">至</span>
            <Input
              type="date"
              aria-label="结束日期"
              className="h-[30px] w-[148px] text-[12px]"
              value={range.end}
              min={range.start}
              onChange={(event) => applyCustomDay('end', event.target.value)}
            />
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-[20px]">
          {stats && stats.scope === 'self' && (
            <p className="text-[12px] text-[#8b94aa]">当前账号为普通成员，这里只统计你自己的用量。</p>
          )}
          <div className="grid grid-cols-2 gap-[12px] lg:grid-cols-4">
            <MetricCard label="调用次数" value={stats ? formatTokenCount(stats.totals.calls) : ''} loading={loading} />
            <MetricCard
              label="总 Token"
              value={stats ? formatTokenCount(totalTokens) : ''}
              hint={stats ? formatCompactTokenCount(totalTokens) : ''}
              loading={loading}
            />
            <MetricCard
              label="输入 Token"
              value={stats ? formatTokenCount(stats.totals.input_tokens) : ''}
              loading={loading}
            />
            <MetricCard
              label="输出 Token"
              value={stats ? formatTokenCount(stats.totals.output_tokens) : ''}
              loading={loading}
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
                {stats?.daily.map((point) => (
                  <div
                    key={point.date}
                    className="group flex h-full min-w-[8px] flex-1 flex-col justify-end"
                    title={`${point.date} · ${formatTokenCount(point.calls)} 次 · ${formatTokenCount(point.total_tokens)} Token`}
                  >
                    <div
                      className="w-full rounded-t-[3px] bg-[#0f766e] transition-colors group-hover:bg-[#0b5c56]"
                      style={{ height: `${barHeightPercent(point.total_tokens, dailyMax)}%` }}
                    />
                  </div>
                ))}
              </div>
              <div className="mt-[6px] flex items-center justify-between text-[11px] text-[#8b94aa]">
                <span>{stats?.daily[0]?.date ?? range.start}</span>
                <span>{stats?.daily[stats.daily.length - 1]?.date ?? range.end}</span>
              </div>
            </div>
          ) : (
            <p className="py-[48px] text-center text-[13px] text-[#8b94aa]">该区间没有大模型调用记录</p>
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

function MetricCard({
  label,
  value,
  hint,
  loading,
}: {
  label: string;
  value: string;
  hint?: string;
  loading: boolean;
}) {
  return (
    <div className="rounded-[12px] border border-[#eceef1] bg-[#f7f8fa] px-[16px] py-[14px]">
      <p className="text-[12px] text-[#8b94aa]">{label}</p>
      {loading ? (
        <Skeleton className="mt-[8px] h-[26px] w-[80px] rounded-[6px]" />
      ) : (
        <p className="mt-[6px] text-[24px] leading-none font-semibold text-[#202226]">{value || '0'}</p>
      )}
      {hint && !loading && <p className="mt-[6px] text-[11px] text-[#8b94aa]">{hint}</p>}
    </div>
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
      className="inline-flex items-center gap-[4px] transition-colors hover:text-[#0f766e]"
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
          <span className="truncate text-[#202226]" title={breakdownLabel(row)}>
            {breakdownLabel(row)}
          </span>
          {row.name && row.name !== row.key && (
            <span className="truncate text-[11px] text-[#9aa3b5]" title={row.key}>
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
        <span className="font-medium text-[#202226]">{formatTokenCount(row.total_tokens)}</span>
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
            <span className="h-[6px] w-[72px] overflow-hidden rounded-full bg-[#eceef1]">
              <span
                className="block h-full rounded-full bg-[#0f766e]"
                style={{ width: `${Math.min(100, Math.max(share > 0 ? 2 : 0, share))}%` }}
              />
            </span>
            <span className="text-[12px] text-[#8b94aa]">{`${share.toFixed(1)}%`}</span>
          </span>
        );
      },
    },
  ];
}
