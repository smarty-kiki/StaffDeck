// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import PlatformColumn from './PlatformColumn';

afterEach(cleanup);

describe('PlatformColumn management entry', () => {
  it('keeps the "查看全部" entry when the column has no content', async () => {
    // 空列也要能进模块管理页，否则「没数据 → 没入口 → 永远没数据」。
    const onViewAll = vi.fn();
    render(
      <PlatformColumn
        icon={<span>icon</span>}
        title="知识库广场"
        count={0}
        countLabel="内容"
        isEmpty
        emptyText="暂无开放内容"
        onViewAll={onViewAll}
      />,
    );

    const entry = screen.getByRole('button', { name: /查看全部/ });
    await userEvent.click(entry);
    expect(onViewAll).toHaveBeenCalledTimes(1);
  });

  it('keeps the entry while the column is still loading', () => {
    render(
      <PlatformColumn
        icon={<span>icon</span>}
        title="工具广场"
        count={0}
        countLabel="工具"
        loading
        onViewAll={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: /查看全部/ })).not.toBeNull();
  });
});
