// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';

import { resolveEditorReturn } from './GeneralSkillsPage';

describe('general skill editor return target', () => {
  it('returns to the platform module the editor was opened from', () => {
    expect(resolveEditorReturn('/enterprise/platform/general-skills')).toEqual({
      path: '/enterprise/platform/general-skills',
      label: '返回开放广场',
    });
  });

  it('falls back to the skill management page without a source', () => {
    expect(resolveEditorReturn(null)).toEqual({
      path: '/enterprise/general-skills',
      label: '返回技能',
    });
    expect(resolveEditorReturn('')).toEqual({
      path: '/enterprise/general-skills',
      label: '返回技能',
    });
  });

  it('falls back for sources outside the enterprise app', () => {
    // 来源只接受站内路径，避免被拼成外部地址。
    for (const from of ['https://example.com', '//evil.test', '/dashboard', 'enterprise/x']) {
      expect(resolveEditorReturn(from).path).toBe('/enterprise/general-skills');
    }
  });

  it('labels the skill management page entry as a skill return', () => {
    expect(resolveEditorReturn('/enterprise/general-skills')).toEqual({
      path: '/enterprise/general-skills',
      label: '返回技能',
    });
  });

  it('does not mistake a sibling route for the platform module', () => {
    expect(resolveEditorReturn('/enterprise/platform-admin').label).toBe('返回技能');
  });
});
