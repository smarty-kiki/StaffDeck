// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import PlatformKindDetailView from './PlatformKindDetailView';

afterEach(cleanup);

const Icon = () => <svg role="img" aria-label="module-icon" />;

function renderDetail(overrides: Partial<Parameters<typeof PlatformKindDetailView>[0]> = {}) {
  const props = {
    kind: 'knowledge' as const,
    title: '知识库广场',
    subtitle: '发布到广场的知识库，可引用到你的数字员工。',
    countLabel: '内容',
    signals: [],
    icon: Icon,
    items: [],
    loading: false,
    employeeStats: () => [],
    onBack: vi.fn(),
    onRefresh: vi.fn(),
    onOpenItem: vi.fn(),
    ...overrides,
  };
  render(
    <I18nProvider>
      <MemoryRouter>
        <PlatformKindDetailView {...props} />
      </MemoryRouter>
    </I18nProvider>,
  );
  return props;
}

describe('PlatformKindDetailView create entry', () => {
  it('labels the create entry after the current module', () => {
    // 「创建开放 XX」必须随模块变化，否则知识库/工具广场会挂着一个「创建开放 Skill」。
    for (const [kind, label] of [
      ['knowledge', '创建开放知识库'],
      ['general-skills', '创建开放技能'],
      ['skills', '创建开放 SOP'],
      ['tools', '创建开放工具'],
    ] as const) {
      renderDetail({ kind, createLabel: label, onCreate: vi.fn() });
      expect(screen.getByRole('button', { name: new RegExp(label) })).not.toBeNull();
      cleanup();
    }
  });

  it('fires onCreate from the create entry', async () => {
    const onCreate = vi.fn();
    renderDetail({ createLabel: '创建开放知识库', onCreate });
    await userEvent.click(screen.getByRole('button', { name: /创建开放知识库/ }));
    expect(onCreate).toHaveBeenCalledTimes(1);
  });

  it('hides the create entry without a handler', () => {
    renderDetail({ createLabel: '创建开放知识库' });
    expect(screen.queryByRole('button', { name: /创建开放知识库/ })).toBeNull();
  });
});

describe('PlatformKindDetailView plaza governance', () => {
  const knowledgeItem = {
    id: 'row-uuid',
    deleteKey: 'kb-1',
    title: 'Finance KB',
    description: 'Plaza knowledge base',
    meta: '12 documents',
    tags: ['v1.0.0'],
  };

  it('offers delete on plaza resources, not the employee unpublish wording', async () => {
    const onRemoveItem = vi.fn();
    renderDetail({ items: [knowledgeItem], canManage: true, onRemoveItem });

    expect(screen.queryByRole('button', { name: '从广场下架' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: '从广场删除' }));
    expect(onRemoveItem).toHaveBeenCalledTimes(1);
    expect(onRemoveItem.mock.calls[0][0]).toMatchObject({ id: 'row-uuid' });
  });

  it('hides the governance action from non-admins', () => {
    renderDetail({ items: [knowledgeItem], onRemoveItem: vi.fn() });
    expect(screen.queryByRole('button', { name: '从广场删除' })).toBeNull();
  });

  it('matches the in-flight item by its plaza delete key, not the row id', () => {
    // 技能用 slug、SOP 用 skill_id：拿 row id 去比会永远匹配不上，按钮不会进 loading。
    renderDetail({
      kind: 'general-skills',
      items: [{ ...knowledgeItem, deleteKey: 'skill-slug' }],
      canManage: true,
      onRemoveItem: vi.fn(),
      removingItemKey: 'skill-slug',
    });

    const action = screen.getByRole('button', { name: '从广场删除' }) as HTMLButtonElement;
    expect(action.disabled).toBe(true);
  });
});
