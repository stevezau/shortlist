import { Bell, CircleAlert, Info, TriangleAlert, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { useDismissNotification, useNotifications } from "@/lib/queries";
import type { AppNotification } from "@/lib/types";
import { cn } from "@/lib/utils";

const SEVERITY = {
  error: { icon: CircleAlert, className: "text-destructive" },
  warning: { icon: TriangleAlert, className: "text-warning" },
  info: { icon: Info, className: "text-primary" },
} as const;

function NotificationRow({
  item,
  onDismiss,
  onNavigate,
}: {
  item: AppNotification;
  onDismiss: (id: string) => void;
  onNavigate: () => void;
}) {
  const { icon: Icon, className } = SEVERITY[item.severity] ?? SEVERITY.info;
  const isExternal = /^https?:\/\//.test(item.action_url);
  return (
    <li className="flex gap-2.5 px-3 py-2.5">
      <Icon
        className={cn("mt-0.5 h-4 w-4 shrink-0", className)}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-sm font-medium">{item.title}</p>
        <p className="text-xs text-muted-foreground">{item.body}</p>
        <div className="flex items-center gap-3 pt-0.5">
          {item.action_url &&
            (isExternal ? (
              <a
                href={item.action_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-primary underline-offset-4 hover:underline"
              >
                {item.action_label}
              </a>
            ) : (
              <Link
                to={item.action_url}
                onClick={onNavigate}
                className="text-xs text-primary underline-offset-4 hover:underline"
              >
                {item.action_label}
              </Link>
            ))}
          {item.dismissable && (
            <button
              type="button"
              onClick={() => onDismiss(item.id)}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              Dismiss
            </button>
          )}
        </div>
      </div>
    </li>
  );
}

/** A bell with a count badge that opens a panel of the owner's current notifications. `align`
 *  controls which way the panel opens: "left" (into the content, for the desktop sidebar) or
 *  "right" (for the mobile top bar). */
export function NotificationBell({
  align = "left",
}: {
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const notifications = useNotifications();
  const dismiss = useDismissNotification();
  const items = notifications.data?.notifications ?? [];
  const count = items.length;
  const hasError = items.some((n) => n.severity === "error");

  // Close on an outside click or Escape — the expected behaviour for a dropdown.
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node))
        setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <Button
        variant="ghost"
        size="icon"
        aria-label={count ? `Notifications (${count})` : "Notifications"}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Bell aria-hidden="true" />
        {count > 0 && (
          <span
            className={cn(
              "absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] font-semibold text-white",
              hasError ? "bg-destructive" : "bg-amber-500",
            )}
          >
            {count}
          </span>
        )}
      </Button>
      {open && (
        <div
          role="dialog"
          aria-label="Notifications"
          className={cn(
            "absolute z-50 mt-2 w-80 max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg border bg-card shadow-lg",
            align === "left" ? "left-0" : "right-0",
          )}
        >
          <div className="flex items-center justify-between border-b px-3 py-2">
            <span className="text-sm font-medium">Notifications</span>
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6"
              aria-label="Close notifications"
              onClick={() => setOpen(false)}
            >
              <X aria-hidden="true" />
            </Button>
          </div>
          {count === 0 ? (
            <p className="px-3 py-8 text-center text-sm text-muted-foreground">
              You&rsquo;re all caught up.
            </p>
          ) : (
            <ul className="max-h-96 divide-y overflow-y-auto">
              {items.map((item) => (
                <NotificationRow
                  key={item.id}
                  item={item}
                  onDismiss={(id) => dismiss.mutate(id)}
                  onNavigate={() => setOpen(false)}
                />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
