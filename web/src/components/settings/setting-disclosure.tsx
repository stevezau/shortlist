import { ChevronDown } from "lucide-react";
import { type ReactNode, useEffect, useRef } from "react";
import { useLocation } from "react-router";

/** A setting's current value stays visible; its original controls stay mounted below it. */
export function SettingDisclosure({ title, value, description, children, defaultOpen = false }: {
  title: string;
  value?: ReactNode;
  description?: string;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const { hash, key } = useLocation();
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const target = hash ? document.getElementById(hash.slice(1)) : null;
    if (target && ref.current?.contains(target)) ref.current.open = true;
  }, [hash, key]);

  return (
    <details ref={ref} open={defaultOpen || undefined} className="group/setting">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-4 py-4 sm:px-5 [&::-webkit-details-marker]:hidden">
        <span className="min-w-0">
          <span className="block text-sm font-medium">{title}</span>
          {description && <span className="mt-1 block text-sm text-muted-foreground">{description}</span>}
        </span>
        <span className="flex max-w-[45%] shrink-0 items-center gap-2 rounded-md border bg-background/30 px-2.5 py-1.5 text-right text-xs text-muted-foreground">
          {value}<ChevronDown aria-hidden="true" className="size-3 shrink-0 transition-transform group-open/setting:rotate-180" />
        </span>
      </summary>
      <div className="mx-4 mb-4 space-y-3 border-t border-border/60 pt-4 sm:mx-5">{children}</div>
    </details>
  );
}
