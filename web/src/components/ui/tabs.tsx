import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/utils";

/** A single keyboard stop for related views. `id` also connects the active TabPanel. */
export function Tabs<T extends string>({ id, value, onChange, options, ariaLabel, className }: {
  id: string;
  value: T;
  onChange: (value: T) => void;
  options: readonly { value: T; label: ReactNode }[];
  ariaLabel: string;
  className?: string;
}) {
  return <div role="tablist" aria-label={ariaLabel} className={cn("flex max-w-full gap-1 overflow-x-auto border-b", className)}>
    {options.map((option, index) => <button
      key={option.value}
      type="button"
      role="tab"
      id={`${id}-tab-${option.value}`}
      aria-controls={`${id}-panel-${option.value}`}
      aria-selected={value === option.value}
      tabIndex={value === option.value ? 0 : -1}
      className={cn("shrink-0 border-b-2 px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", value === option.value ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground")}
      onClick={() => onChange(option.value)}
      onKeyDown={(event) => {
        const offset = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        if (!offset && event.key !== "Home" && event.key !== "End") return;
        event.preventDefault();
        const target = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : (index + offset + options.length) % options.length;
        const next = options[target];
        if (!next) return;
        onChange(next.value);
        event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[target]?.focus();
      }}
    >{option.label}</button>)}
  </div>;
}

export function TabPanel({ id, value, className, ...props }: HTMLAttributes<HTMLDivElement> & { id: string; value: string }) {
  return <div {...props} id={`${id}-panel-${value}`} role="tabpanel" aria-labelledby={`${id}-tab-${value}`} tabIndex={0} className={cn("focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring", className)} />;
}
