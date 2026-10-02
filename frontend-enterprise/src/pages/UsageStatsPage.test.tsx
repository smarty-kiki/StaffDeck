// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MemoryRouter } from 'react-router-dom';

import { I18nProvider } from '@/i18n';

import UsageStatsPage from './UsageStatsPage';
import type { UsageStats } from './usageStats';

function statsBody(overrides: Partial<UsageStats> = {}): UsageStats {
  return {
    range: { start: '2026-09-03', end: '2026-10-02', days: 30, timezone_offset_minutes: 480 },
    scope: 'tenant',
    totals: {
      calls: 104,
      input_tokens: 512486,
      output_tokens: 177382,
      total_tokens: 689868,
      cached_input_tokens: 30,
      avg_duration_ms: 1200,
    },
    daily: [
      { date: '2026-09-21', calls: 103, input_tokens: 512286, output_tokens: 177382, total_tokens: 689668, cached_input_tokens: 0 },
      { date: '2026-10-02', calls: 1, input_tokens: 200, output_tokens: 0, total_tokens: 200, cached_input_tokens: 30 },
    ],
    by_agent: [
      {
        key: 'agent_a',
        name: '客服助手',
        calls: 104,
        input_tokens: 512486,
        output_tokens: 177382,
        total_tokens: 689868,
        cached_input_tokens: 30,
      },
    ],
    by_user: [
      {
        key: 'user_admin',
        name: '管理员',
        calls: 104,
        input_tokens: 512486,
        output_tokens: 177382,
        total_tokens: 689868,
        cached_input_tokens: 30,
      },
    ],
    by_model: [
      {
        key: 'deepseek-flash',
        name: null,
        calls: 104,
        input_tokens: 512486,
        output_tokens: 177382,
        total_tokens: 689868,
        cached_input_tokens: 30,
      },
    ],
    by_operation: [],
    ...overrides,
  };
}

const requestedUrls: string[] = [];

function stubFetch(body: UsageStats) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      requestedUrls.push(String(input));
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        text: async () => JSON.stringify(body),
      } as Response;
    }),
  );
}

function renderPage() {
  return render(
    <MemoryRouter>
      <I18nProvider>
        <UsageStatsPage />
      </I18nProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  requestedUrls.length = 0;
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('UsageStatsPage', () => {
  it('renders the totals returned by the usage API', async () => {
    stubFetch(statsBody());
    renderPage();

    // 同一个数字会同时出现在汇总卡与明细表里，故用 getAllByText 断言存在
    await waitFor(() => expect(screen.getAllByText('689,868').length).toBeGreaterThan(0));
    expect(screen.getAllByText('104').length).toBeGreaterThan(0);
    expect(screen.getAllByText('512,486').length).toBeGreaterThan(0);
    expect(screen.getAllByText('177,382').length).toBeGreaterThan(0);
    // 紧凑写法只作为总 Token 的副标题出现
    expect(screen.getAllByText('689.9K').length).toBe(1);
  });

  it('asks the backend for the selected range and the browser timezone', async () => {
    stubFetch(statsBody());
    renderPage();

    await waitFor(() => expect(requestedUrls.length).toBeGreaterThan(0));
    const first = new URL(requestedUrls[0], 'http://localhost');
    expect(first.pathname).toBe('/api/enterprise/usage/stats');
    expect(Number(first.searchParams.get('tz_offset_minutes'))).toBe(
      -new Date().getTimezoneOffset(),
    );
    // 默认 30 天：含头含尾
    const start = first.searchParams.get('start')!;
    const end = first.searchParams.get('end')!;
    const spanDays =
      (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000 + 1;
    expect(spanDays).toBe(30);
  });

  it('renders one row per breakdown entry with its resolved display name', async () => {
    stubFetch(statsBody());
    renderPage();

    await waitFor(() => expect(screen.getByText('客服助手')).toBeTruthy());
    expect(screen.getByText('agent_a')).toBeTruthy();
    expect(screen.getByText('100.0%')).toBeTruthy();
  });

  it('explains that members only see their own usage', async () => {
    stubFetch(
      statsBody({
        scope: 'self',
        totals: {
          calls: 0,
          input_tokens: 0,
          output_tokens: 0,
          total_tokens: 0,
          cached_input_tokens: 0,
          avg_duration_ms: 0,
        },
        daily: [],
        by_agent: [],
        by_user: [],
        by_model: [],
      }),
    );
    renderPage();

    await waitFor(() =>
      expect(screen.getByText('当前账号为普通成员，这里只统计你自己的用量。')).toBeTruthy(),
    );
    expect(screen.getAllByText('该区间没有大模型调用记录').length).toBe(1);
  });
});
