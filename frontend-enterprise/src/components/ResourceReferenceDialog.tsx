import type { ReactNode } from 'react';

import {
  Checkbox,
  Dialog,
  DialogContent,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { SELECT_TRIGGER_CLASS } from '@/lib/enterprise-ui';

export type ReferenceTargetOption = { value: string; label: string };
export type ReferenceChoiceItem = { id: string; label: ReactNode };

export type ResourceReferenceDialogProps = {
  open: boolean;
  loading: boolean;
  /** Header icon (14px). */
  icon: ReactNode;
  title: string;
  /** Optional target select for flows where the destination is not implied by page scope. */
  targetPlaceholder?: string;
  targetLabel?: string;
  targets?: ReferenceTargetOption[];
  targetId?: string;
  /** Caption above the checkbox list, e.g. "选择 SOP" / "选择技能". */
  itemsLabel: string;
  items: ReferenceChoiceItem[];
  selectedIds: string[];
  /** Shown when the plaza has no referable items. */
  emptyText: string;
  /** Explanatory footer note. */
  note: ReactNode;
  submitText?: string;
  onTargetChange?: (value: string) => void;
  onSelectedChange: (ids: string[]) => void;
  onClose: () => void;
  onSubmit: () => void;
};

/**
 * 「引用广场资源到某个员工」对话框。
 *
 * 只从**广场**取资源：私有资源归属即生效，不能被别人引用。勾选 = 建立引用行，
 * 作者之后更新资源，所有引用者立刻看到最新内容（不是复制）。
 */
export function ResourceReferenceDialog({
  open,
  loading,
  icon,
  title,
  targetPlaceholder,
  targetLabel = '引用到',
  targets,
  targetId,
  itemsLabel,
  items,
  selectedIds,
  emptyText,
  note,
  submitText = '引用',
  onTargetChange,
  onSelectedChange,
  onClose,
  onSubmit,
}: ResourceReferenceDialogProps) {
  const showTargetSelect = Boolean(targets && onTargetChange);

  const toggle = (id: string, checked: boolean) => {
    onSelectedChange(checked ? [...selectedIds, id] : selectedIds.filter((value) => value !== id));
  };
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[640px]"
      >
        <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          {icon}
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            {title}
          </DialogTitle>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-[14px] overflow-y-auto px-[12px]">
          {showTargetSelect && (
            <div className="flex flex-col gap-[6px]">
              <span className="text-[11px] font-semibold text-[#858b9c]">{targetLabel}</span>
              <Select value={targetId || undefined} onValueChange={onTargetChange}>
                <SelectTrigger className={cn(SELECT_TRIGGER_CLASS, 'w-full')}>
                  <SelectValue placeholder={targetPlaceholder || targetLabel} />
                </SelectTrigger>
                <SelectContent>
                  {(targets || []).map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          <div className="flex flex-col gap-[6px]">
            <span className="text-[11px] font-semibold text-[#858b9c]">{itemsLabel}</span>
            <div className="max-h-[300px] overflow-y-auto rounded-[10px] border border-[#eef0f4] p-[6px]">
              {items.length === 0 ? (
                <div className="py-[28px] text-center text-[12px] text-[#858b9c]">{emptyText}</div>
              ) : (
                items.map((item) => (
                  <label
                    key={item.id}
                    className="flex cursor-pointer items-center gap-[10px] rounded-[8px] px-[8px] py-[7px] hover:bg-[#f6f6f6]"
                  >
                    <Checkbox
                      checked={selectedIds.includes(item.id)}
                      onCheckedChange={(checked) => toggle(item.id, checked === true)}
                    />
                    <span className="min-w-0 flex-1 truncate text-[12px] text-[#18181a]">
                      {item.label}
                    </span>
                  </label>
                ))
              )}
            </div>
          </div>

          <p className="text-[12px] leading-[1.6] text-[#858b9c]">{note}</p>
        </div>

        <div className="flex items-center justify-end gap-[8px] px-[12px]">
          <Button
            variant="outline"
            disabled={loading}
            onClick={onClose}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            取消
          </Button>
          <Button
            disabled={loading}
            onClick={onSubmit}
            className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030]"
          >
            {submitText}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
