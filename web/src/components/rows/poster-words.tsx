import { useLayoutEffect, useRef } from "react";

/**
 * A compact, local rendering of a text poster. It deliberately does not fetch the poster endpoint:
 * a Rows card should show the configured words without creating a cached asset or asking an image
 * provider to make one.
 */
export function PosterWords({
  title,
  subtitle,
  posterStyle = "",
}: {
  title: string;
  subtitle: string;
  /** The configured style participates in the deterministic local palette, as it does on the server. */
  posterStyle?: string;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const words = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const fit = () => {
      if (!frame.current || !words.current || !frame.current.clientWidth) return;
      const box = frame.current;
      const content = words.current;
      const padding = Math.round(box.clientWidth * 0.09);
      box.style.padding = `${padding}px`;
      let size = Math.min(20, box.clientWidth * 0.145);
      content.style.fontSize = `${size}px`;
      while (size > 4 && (content.scrollHeight > box.clientHeight - padding * 2 || content.scrollWidth > box.clientWidth - padding * 2)) {
        size -= 0.5;
        content.style.fontSize = `${size}px`;
      }
    };
    fit();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(fit) : null;
    if (frame.current) observer?.observe(frame.current);
    return () => observer?.disconnect();
  }, [title, subtitle]);
  return (
    <div
      ref={frame}
      data-poster-words
      data-poster-style={posterStyle || undefined}
      className="flex size-full items-end bg-accent text-accent-foreground"
      style={{ backgroundImage: posterBackground(title, subtitle, posterStyle) }}
    >
      <div ref={words} className="w-full min-w-0 space-y-1 text-sm leading-tight [overflow-wrap:anywhere]">
        <p className="font-semibold">{title}</p>
        {subtitle && <p className="text-[0.75em] text-accent-foreground/80">{subtitle}</p>}
      </div>
    </div>
  );
}

/** A stable dark palette so changing configured title, subtitle, or style refreshes the local preview too. */
function posterBackground(title: string, subtitle: string, posterStyle: string): string {
  let hash = 2166136261;
  for (const char of `${title}|${subtitle}|${posterStyle}`) {
    hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
  }
  const hue = (hash >>> 0) % 360;
  return `linear-gradient(160deg, hsl(${hue} 45% 24%), hsl(${(hue + 35) % 360} 50% 7%))`;
}
