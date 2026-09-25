// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import PlatformResourceCard from './PlatformResourceCard';

afterEach(cleanup);

function renderCard(onDelete?: () => void) {
  const onOpen = vi.fn();
  render(
    <PlatformResourceCard
      title="Finance KB"
      meta="12 documents"
      description="Plaza knowledge base"
      tags={['v1.0.0']}
      onClick={onOpen}
      onDelete={onDelete}
    />,
  );
  return { onOpen };
}

describe('PlatformResourceCard plaza governance', () => {
  it('does not expose the delete action without an admin callback', () => {
    renderCard();
    expect(screen.queryByRole('button', { name: '从广场删除' })).toBeNull();
  });

  it('deletes the plaza item without opening its details', async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn();
    const { onOpen } = renderCard(onDelete);

    await user.click(screen.getByRole('button', { name: '从广场删除' }));

    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('never borrows the employee wording for a plaza resource', () => {
    // 数字员工是「下架」（员工本体保留），广场资源是「删除」，两套词不能串。
    renderCard(vi.fn());
    expect(screen.queryByRole('button', { name: '从广场下架' })).toBeNull();
    expect(screen.getByRole('button', { name: '从广场删除' })).not.toBeNull();
  });

  it('disables the action while the deletion is in flight', () => {
    const onOpen = vi.fn();
    render(
      <PlatformResourceCard
        title="Finance KB"
        meta="12 documents"
        description="Plaza knowledge base"
        onClick={onOpen}
        onDelete={vi.fn()}
        deleting
      />,
    );

    const action = screen.getByRole('button', { name: '从广场删除' }) as HTMLButtonElement;
    expect(action.disabled).toBe(true);
  });
});
