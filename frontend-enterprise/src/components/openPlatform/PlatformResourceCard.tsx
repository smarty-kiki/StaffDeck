import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import IconFolder from '../../assets/icons/cap-folder.svg?react';
import IconTrash from '../../assets/icons/trash.svg?react';

/** Per-module accent used for the meta line and tag pills (SD1 232:4634 family). */
export type PlatformResourceAccent = 'green' | 'blue' | 'indigo' | 'orange';

const ACCENT_STYLES: Record<PlatformResourceAccent, { meta: string; tag: string }> = {
  green: { meta: 'text-[#2cb360]', tag: 'bg-[#e9f7ef] text-[#2cb360]' },
  blue: { meta: 'text-[#27c9ff]', tag: 'bg-[#c4f1ff] text-[#25c7ff]' },
  indigo: { meta: 'text-[#1a71ff]', tag: 'bg-[#e8f0ff] text-[#1a71ff]' },
  orange: { meta: 'text-[#ff7f00]', tag: 'bg-[#fff2e5] text-[#ff7f00]' },
};

export const platformResourceAccentStyles = ACCENT_STYLES;

export type PlatformResourceCardProps = {
  title: ReactNode;
  /** Accent metric line under the title, e.g. "12M / 6个片段". */
  meta: ReactNode;
  description: ReactNode;
  tags?: string[];
  /** Full 36px icon visual. When omitted a default folder tile is shown. */
  icon?: ReactNode;
  /** Module accent color for the meta line and tag pills. Defaults to green (知识库). */
  accent?: PlatformResourceAccent;
  onClick?: () => void;
  /**
   * 管理员的广场治理动作。
   *
   * 和数字员工的「下架」不同：员工下架只是从广场撤下、员工本体和资源都留着；
   * 广场上的知识库 / 技能 / SOP / 工具没有「published 开关」这套中间态，
   * 从广场拿掉就是真的删掉这条资源，所以这里叫删除。
   */
  onDelete?: () => void;
  deleting?: boolean;
  className?: string;
};

/**
 * 广场 resource card shared by the 知识库 / 技能 / SOP / 工具 modules. It renders a
 * colorful module icon, a title with a green meta line, a two-line description
 * and a row of green pills on a clean white card (SD1 232:4923).
 */
export default function PlatformResourceCard({
  title,
  meta,
  description,
  tags,
  icon,
  accent = 'green',
  onClick,
  onDelete,
  deleting = false,
  className,
}: PlatformResourceCardProps) {
  const accentStyles = ACCENT_STYLES[accent];
  return (
    <article
      className={cn(
        'group relative h-[112px] w-full shrink-0 rounded-[14px] border-[0.5px] border-[#f6f6f6] bg-white p-[4px] text-left backdrop-blur-[1.835px] transition-shadow hover:shadow-[0_8px_20px_rgba(15,23,42,0.06)]',
        className,
      )}
    >
      <button
        type="button"
        onClick={onClick}
        className="flex h-full w-full flex-col items-center justify-center overflow-hidden rounded-[12px] text-left outline-none focus-visible:ring-2 focus-visible:ring-[#9dd7cf]"
      >
        <div className="flex w-full flex-col items-start gap-[6px] px-[8px]">
          <div className="flex w-full items-center gap-[4px]">
            {icon ?? (
              <span className="grid size-[32px] shrink-0 place-items-center rounded-[10px] bg-[#f2f4f8] text-[#8a94a6]">
                <IconFolder className="size-[18px]" />
              </span>
            )}
            <div className={cn(
              'flex min-w-0 flex-1 flex-col gap-[4px]',
              // 治理按钮常驻在右上角（hover 才显形），标题要给它在右侧留出位置，免得被压住。
              onDelete && 'pr-[40px]',
            )}>
              <p className="truncate text-[12px] font-medium text-[#464c5e]">{title}</p>
              <p className={cn('truncate text-[10px]', accentStyles.meta)}>{meta}</p>
            </div>
          </div>

          <p className="line-clamp-2 h-[26px] w-full text-[10px] leading-[13px] text-[#757f9c]">
            {description}
          </p>

          {tags && tags.length > 0 && (
            <div className="flex flex-wrap items-center gap-[6px]">
              {tags.map((tag) => (
                <span
                  key={tag}
                  className={cn(
                    'inline-flex items-center rounded-[90px] px-[8px] py-[2px] text-[8px] leading-[normal]',
                    accentStyles.tag,
                  )}
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>
      </button>

      {onDelete && (
        <button
          type="button"
          aria-label="从广场删除"
          title="从广场删除"
          disabled={deleting}
          onClick={onDelete}
          className={cn(
            'absolute top-[8px] right-[8px] inline-flex h-[24px] items-center gap-[4px] rounded-[9px] border border-[#f3c7c7] bg-white px-[7px] text-[9px] font-medium text-[#b42318] shadow-[0_3px_10px_rgba(20,20,20,0.06)] transition-all',
            'pointer-events-none opacity-0 group-hover:pointer-events-auto group-hover:opacity-100 focus-visible:pointer-events-auto focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#f1aaaa]',
            'hover:border-[#e49b9b] hover:bg-[#fff7f7] disabled:cursor-wait disabled:opacity-50',
          )}
        >
          <IconTrash className="size-[11px]" />
          删除
        </button>
      )}
    </article>
  );
}
