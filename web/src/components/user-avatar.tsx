import { cn } from "@/lib/utils";

// A small palette (Tailwind's named scales, matching the FakePlexRow precedent) so each user gets a
// stable colour — enough variety to tell people apart in a list without a photo, muted for dark UI.
const TINTS = [
  "bg-amber-500/15 text-amber-300",
  "bg-rose-500/15 text-rose-300",
  "bg-sky-500/15 text-sky-300",
  "bg-emerald-500/15 text-emerald-300",
  "bg-violet-500/15 text-violet-300",
  "bg-cyan-500/15 text-cyan-300",
] as const;

const SIZES = {
  xs: "h-6 w-6 text-[11px]",
  sm: "h-7 w-7 text-xs",
  md: "h-9 w-9 text-sm",
  lg: "h-12 w-12 text-base",
} as const;

/** Deterministic tint from the name so the same user is always the same colour across screens. */
function tintFor(name: string): string {
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.charCodeAt(0)) & 0xffffffff;
  return TINTS[Math.abs(hash) % TINTS.length] ?? TINTS[0];
}

function initials(name: string): string {
  // Bracketed notes and punctuation are not the name: Plex display names read like
  // "Alex - Sam's Mate (P)", which must not give "A(" — a bracket where a letter belongs.
  const parts = name
    .replace(/\([^)]*\)|\[[^\]]*\]/g, " ")
    .split(/\s+/)
    .map((part) => part.replace(/[^\p{L}\p{N}]/gu, ""))
    .filter(Boolean);
  if (parts.length === 0) return "?";
  const first = parts[0] ?? "";
  if (parts.length === 1) return first.slice(0, 2).toUpperCase() || "?";
  const last = parts[parts.length - 1] ?? "";
  return (first.slice(0, 1) + last.slice(0, 1)).toUpperCase() || "?";
}

/** Circular initials badge that stands in for a user's avatar. */
export function UserAvatar({
  name,
  size = "md",
  className,
  labelled = false,
}: {
  name: string;
  size?: keyof typeof SIZES;
  className?: string;
  /** When the face stands alone with no name beside it (a stack of watchers), it must name itself:
   *  on hover, and to a screen reader. Beside a written name it stays decorative. */
  labelled?: boolean;
}) {
  return (
    <span
      {...(labelled ? { role: "img", "aria-label": name, title: name } : { "aria-hidden": true })}
      className={cn(
        "inline-grid shrink-0 place-items-center rounded-full font-semibold",
        SIZES[size],
        tintFor(name),
        className,
      )}
    >
      {initials(name)}
    </span>
  );
}
