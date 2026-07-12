import type { ReactNode } from "react";
import * as RDropdown from "@radix-ui/react-dropdown-menu";
import { Check } from "lucide-react";
import { cn } from "@/lib/cn";
import { Kbd } from "./Kbd";

export const DropdownMenu = RDropdown.Root;
export const DropdownMenuTrigger = RDropdown.Trigger;

export interface DropdownMenuContentProps {
  children: ReactNode;
  align?: "start" | "center" | "end";
  side?: "top" | "right" | "bottom" | "left";
  className?: string;
}

export function DropdownMenuContent({
  children,
  align = "start",
  side = "bottom",
  className,
}: DropdownMenuContentProps) {
  return (
    <RDropdown.Portal>
      <RDropdown.Content
        align={align}
        side={side}
        sideOffset={6}
        collisionPadding={8}
        className={cn(
          "pop-in z-50 min-w-[200px] max-w-[280px] rounded-card border border-ink-700 bg-ink-800 p-1",
          "max-h-[var(--radix-dropdown-menu-content-available-height)] overflow-y-auto overscroll-contain",
          "shadow-[var(--shadow-pop)] focus:outline-none",
          className,
        )}
      >
        {children}
      </RDropdown.Content>
    </RDropdown.Portal>
  );
}

export interface DropdownMenuItemProps {
  children: ReactNode;
  onSelect?: () => void;
  icon?: ReactNode;
  shortcut?: string;
  danger?: boolean;
  disabled?: boolean;
  /** Keep the menu open after selecting — for multi-toggle lists (e.g. labels)
   *  where the user picks several items in one visit. */
  keepOpen?: boolean;
}

const itemBase =
  "flex items-center gap-2.5 rounded-control px-2 py-1.5 text-sm text-moon-100 " +
  "cursor-pointer select-none outline-none " +
  "data-[highlighted]:bg-ink-700 data-[disabled]:opacity-40 data-[disabled]:pointer-events-none";

export function DropdownMenuItem({
  children,
  onSelect,
  icon,
  shortcut,
  danger,
  disabled,
  keepOpen,
}: DropdownMenuItemProps) {
  return (
    <RDropdown.Item
      disabled={disabled}
      onSelect={(e) => {
        // Radix closes the menu on select by default; preventDefault keeps it
        // open so multiple toggles land without reopening each time.
        if (keepOpen) e.preventDefault();
        onSelect?.();
      }}
      className={cn(itemBase, danger && "text-failed data-[highlighted]:bg-failed/15")}
    >
      {icon && <span className="text-moon-400">{icon}</span>}
      <span className="flex min-w-0 flex-1 items-center gap-2">{children}</span>
      {shortcut && <Kbd>{shortcut}</Kbd>}
    </RDropdown.Item>
  );
}

export interface DropdownMenuCheckboxItemProps {
  children: ReactNode;
  checked: boolean;
  onCheckedChange?: (checked: boolean) => void;
}

export function DropdownMenuCheckboxItem({
  children,
  checked,
  onCheckedChange,
}: DropdownMenuCheckboxItemProps) {
  return (
    <RDropdown.CheckboxItem
      checked={checked}
      onCheckedChange={onCheckedChange}
      className={cn(itemBase, "pl-2")}
    >
      <span className="flex h-4 w-4 items-center justify-center text-lamp">
        {checked && <Check size={14} />}
      </span>
      <span className="flex-1">{children}</span>
    </RDropdown.CheckboxItem>
  );
}

export function DropdownMenuLabel({ children }: { children: ReactNode }) {
  return (
    <div className="px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
      {children}
    </div>
  );
}

export function DropdownMenuSeparator() {
  return <RDropdown.Separator className="my-1 h-px bg-ink-700" />;
}
