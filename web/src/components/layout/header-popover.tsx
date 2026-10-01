import * as Dialog from "@radix-ui/react-dialog";
import { useCallback, useLayoutEffect, useRef, type ReactElement, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Header panels stay inside the viewport even when their trigger is beside other mobile icons. */
export function HeaderPopover({ open, onOpenChange, align, label, trigger, children, className, width = 320 }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  align: "left" | "right";
  label: string;
  trigger: ReactElement;
  children: ReactNode;
  className?: string;
  width?: number;
}) {
  const anchor = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const position = useCallback(() => {
    if (!anchor.current || !panel.current) return;
    const rect = anchor.current.getBoundingClientRect();
    const panelWidth = Math.min(width, window.innerWidth - 24);
    const left = Math.max(12, Math.min(align === "left" ? rect.left : rect.right - panelWidth, window.innerWidth - panelWidth - 12));
    panel.current.style.width = `${panelWidth}px`;
    panel.current.style.left = `${left}px`;
    panel.current.style.top = `${rect.bottom + 8}px`;
    panel.current.style.maxHeight = `${Math.max(120, window.innerHeight - rect.bottom - 20)}px`;
  }, [align, width]);
  // Portal content mounts after the parent's layout effect. Position on attachment as well as resize.
  const attachPanel = useCallback((node: HTMLDivElement | null) => {
    panel.current = node;
    position();
  }, [position]);
  useLayoutEffect(() => {
    if (!open) return;
    position();
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    return () => {
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", position, true);
    };
  }, [open, position]);
  return <Dialog.Root open={open} onOpenChange={onOpenChange} modal={false}>
    <Dialog.Trigger ref={anchor} asChild>{trigger}</Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Content ref={attachPanel} aria-describedby={undefined} className={cn("fixed z-50 w-80 max-w-[calc(100vw-1.5rem)] overflow-y-auto rounded-lg border bg-elevated shadow-xl focus:outline-none", className)}>
        <Dialog.Title className="sr-only">{label}</Dialog.Title>
        {children}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
