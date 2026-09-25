import { describe, expect, it } from 'vitest';

import {
  isPlazaCreateKind,
  isPlazaScopeValue,
  plazaCreateLabel,
  plazaCreatePath,
  resolveReturnTarget,
} from './plaza-navigation';

describe('resolveReturnTarget', () => {
  it('returns to the plaza module the editor was opened from', () => {
    expect(resolveReturnTarget('/enterprise/platform/knowledge', {
      path: '/enterprise/knowledge',
      label: '返回',
    })).toEqual({ path: '/enterprise/platform/knowledge', label: '返回开放广场' });
  });

  it('keeps the module label for sources inside the module', () => {
    expect(resolveReturnTarget('/enterprise/tools', {
      path: '/enterprise/tools',
      label: '返回工具',
    })).toEqual({ path: '/enterprise/tools', label: '返回工具' });
  });

  it('falls back when there is no source', () => {
    for (const from of [null, undefined, '', '   ']) {
      expect(resolveReturnTarget(from, { path: '/enterprise/tools', label: '返回工具' })).toEqual({
        path: '/enterprise/tools',
        label: '返回工具',
      });
    }
  });

  it('rejects sources outside the enterprise app', () => {
    // 来源只接受站内路径，避免被拼成外部地址。
    for (const from of ['https://example.com', '//evil.test', '/dashboard', 'enterprise/x']) {
      expect(resolveReturnTarget(from, { path: '/enterprise/tools', label: '返回工具' }).path).toBe(
        '/enterprise/tools',
      );
    }
  });

  it('does not mistake a sibling route for the plaza', () => {
    expect(resolveReturnTarget('/enterprise/platform-admin', {
      path: '/enterprise/tools',
      label: '返回工具',
    }).label).toBe('返回工具');
  });
});

describe('plazaCreatePath', () => {
  const from = '/enterprise/platform/tools';

  it('sends every module into its own create flow in plaza scope', () => {
    for (const kind of ['knowledge', 'general-skills', 'tools'] as const) {
      const path = plazaCreatePath(kind, from);
      expect(path).toContain('scope=gallery');
      expect(path).toContain(encodeURIComponent(from));
      expect(path.startsWith('/enterprise/')).toBe(true);
    }
    expect(plazaCreatePath('knowledge', from)).toContain('/enterprise/knowledge/new?');
    expect(plazaCreatePath('general-skills', from)).toContain('/enterprise/general-skills/new?');
    expect(plazaCreatePath('tools', from)).toContain('/enterprise/tools/new?');
  });

  it('gives the SOP create flow a create-mode entry', () => {
    const path = plazaCreatePath('skills', from);
    expect(path).toContain('/enterprise/skills/distill?');
    expect(path).toContain('mode=create');
  });
});

describe('plaza create labels', () => {
  it('only covers the modules that have an open counterpart', () => {
    expect(isPlazaCreateKind('knowledge')).toBe(true);
    expect(isPlazaCreateKind('general-skills')).toBe(true);
    expect(isPlazaCreateKind('skills')).toBe(true);
    expect(isPlazaCreateKind('tools')).toBe(true);
    expect(isPlazaCreateKind('agents')).toBe(false);
    expect(isPlazaCreateKind('toString')).toBe(false);
  });

  it('names the created resource after the module', () => {
    expect(plazaCreateLabel('knowledge')).toBe('创建开放知识库');
    expect(plazaCreateLabel('skills')).toBe('创建开放 SOP');
    expect(plazaCreateLabel('tools')).toBe('创建开放工具');
  });
});

describe('isPlazaScopeValue', () => {
  it('only accepts the gallery marker', () => {
    expect(isPlazaScopeValue('gallery')).toBe(true);
    expect(isPlazaScopeValue('plaza')).toBe(false);
    expect(isPlazaScopeValue(null)).toBe(false);
  });
});
