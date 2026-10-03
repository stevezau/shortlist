import { Ellipsis, type LucideIcon } from "lucide-react";
import { Fragment, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type OverflowMenuItem = {
  label: string;
  icon: LucideIcon;
  /** A link item; the menu closes as it navigates. */
  to?: string;
  /** A button item, for an action that isn't a page. */
  onSelect?: () => void;
  /** Red text, for the item that ends in removing something. Only ever shown once the menu is open. */
  danger?: boolean;
  /** Draw a rule above this item, fencing it off from the ones before. */
  separated?: boolean;
};

/**
 * A "⋯" button that opens a short list of actions — the WAI-ARIA menu button pattern.
 *
 * There is no dropdown primitive in `components/ui` (no Radix menu dependency), so this is the small
 * hand-rolled one: Enter, Space or ArrowDown open it on the first item, ArrowUp/ArrowDown/Home/End
 * move through it, Escape closes it and puts focus back on the button, and Tab or a click outside
 * closes it where focus is going.
 */
export function OverflowMenu({ label, items }: { label: string; items: OverflowMenuItem[] }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const closeOutside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!menu.current?.contains(target) && !trigger.current?.contains(target)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  const onMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const entries = Array.from(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    const at = entries.indexOf(document.activeElement as HTMLElement);
    const focusAt = (index: number) => entries[(index + entries.length) % entries.length]?.focus();
    switch (event.key) {
      case "Escape":
        event.preventDefault();
        setOpen(false);
        trigger.current?.focus();
        break;
      case "ArrowDown":
        event.preventDefault();
        focusAt(at + 1);
        break;
      case "ArrowUp":
        event.preventDefault();
        focusAt(at - 1);
        break;
      case "Home":
        event.preventDefault();
        focusAt(0);
        break;
      case "End":
        event.preventDefault();
        focusAt(entries.length - 1);
        break;
      case "Tab":
        setOpen(false);
        break;
    }
  };

  const itemClass = (item: OverflowMenuItem) =>
    cn(
      "flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm outline-none",
      "hover:bg-raised focus-visible:bg-raised focus-visible:ring-2 focus-visible:ring-ring",
      "[&_svg]:size-4 [&_svg]:shrink-0",
      item.danger ? "text-destructive-text" : "text-foreground [&_svg]:text-muted-foreground",
    );

  return (
    <div className="relative">
      <Button
        ref={trigger}
        type="button"
        variant="ghost"
        size="icon"
        className="size-8 text-muted-foreground"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((was) => !was)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <Ellipsis aria-hidden="true" />
      </Button>
      {open && (
        <div
          ref={menu}
          id={menuId}
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKeyDown}
          className="absolute right-0 top-full z-30 mt-1 min-w-52 rounded-lg border bg-popover p-1 text-popover-foreground shadow-elevated"
        >
          {items.map((item) => {
            const Icon = item.icon;
            const body = (
              <>
                <Icon aria-hidden="true" />
                {item.label}
              </>
            );
            return (
              <Fragment key={item.label}>
                {item.separated && <div role="separator" className="my-1 h-px bg-border" />}
                {item.to !== undefined ? (
                  <Link
                    role="menuitem"
                    tabIndex={-1}
                    to={item.to}
                    className={itemClass(item)}
                    onClick={() => setOpen(false)}
                  >
                    {body}
                  </Link>
                ) : (
                  <button
                    role="menuitem"
                    type="button"
                    tabIndex={-1}
                    className={itemClass(item)}
                    onClick={() => {
                      setOpen(false);
                      item.onSelect?.();
                    }}
                  >
                    {body}
                  </button>
                )}
              </Fragment>
            );
          })}
        </div>
      )}
    </div>
  );
}
