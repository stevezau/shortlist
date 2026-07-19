// Brand-inspired, hand-authored SVG marks for the services Shortlist talks to. Deliberately NOT the
// pixel-official logos (drop those into web/src/assets and swap these out for a launch): they're
// clean, recognizable stand-ins so a non-technical owner spots "Plex" or "Claude" at a glance.
// Colours are each brand's own — logos can't be theme-tinted — so they live here, not in Tailwind.

import { type ReactNode, useId } from "react";

interface GlyphProps {
  className?: string;
}

// Official-ish brand colours.
const PLEX_GOLD = "#E5A00D";
const TAUTULLI_AMBER = "#DBA11E";
const CLAUDE_CLAY = "#D97757";
const OLLAMA_INK = "#EDEDED";

export function PlexGlyph({ className }: GlyphProps) {
  // Plex's brandmark is a chevron ">".
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path fill={PLEX_GOLD} d="M7 3h5.2l6.8 9-6.8 9H7l6.8-9z" />
    </svg>
  );
}

export function TautulliGlyph({ className }: GlyphProps) {
  // Monitoring/analytics — an amber tile with a watch-activity pulse.
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill={TAUTULLI_AMBER} />
      <path
        d="M5 13h3l2-5 3 8 2-4h4"
        fill="none"
        stroke="#241a02"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function TmdbGlyph({ className }: GlyphProps) {
  // TMDB's mark is a rounded bar with a teal->green gradient. useId keeps the gradient id unique
  // per instance so two on one page can't collide.
  const gid = useId();
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="#01B4E4" />
          <stop offset="1" stopColor="#90CEA1" />
        </linearGradient>
      </defs>
      <rect
        x="2"
        y="6.5"
        width="20"
        height="11"
        rx="2.5"
        fill={`url(#${gid})`}
      />
      <text
        x="12"
        y="14.6"
        textAnchor="middle"
        fontSize="6"
        fontWeight="700"
        fontFamily="Arial, sans-serif"
        fill="#032541"
      >
        TMDB
      </text>
    </svg>
  );
}

export function ImdbGlyph({ className }: GlyphProps) {
  // IMDb's mark: black "IMDb" on the signature yellow, rounded rect.
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <rect x="2" y="6.5" width="20" height="11" rx="2.5" fill="#F5C518" />
      <text
        x="12"
        y="14.7"
        textAnchor="middle"
        fontSize="6.5"
        fontWeight="800"
        fontFamily="Arial, sans-serif"
        fill="#000"
      >
        IMDb
      </text>
    </svg>
  );
}

export function MdblistGlyph({ className }: GlyphProps) {
  // MDBList's mark: white "MDB" on its signature blue, rounded rect — a ratings aggregator.
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <rect x="2" y="6.5" width="20" height="11" rx="2.5" fill="#2b6cb0" />
      <text
        x="12"
        y="14.6"
        textAnchor="middle"
        fontSize="6"
        fontWeight="800"
        fontFamily="Arial, sans-serif"
        fill="#fff"
      >
        MDB
      </text>
    </svg>
  );
}

export function TraktGlyph({ className }: GlyphProps) {
  // Trakt's mark: a red circle with a light ring — recognizable by colour at small sizes.
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <circle cx="12" cy="12" r="10" fill="#ED1C24" />
      <circle
        cx="12"
        cy="12"
        r="6"
        fill="none"
        stroke="#fff"
        strokeWidth="1.6"
        opacity="0.9"
      />
    </svg>
  );
}

function ClaudeGlyph({ className }: GlyphProps) {
  // Anthropic/Claude — a radial sunburst of tapered spokes.
  const spokes = Array.from({ length: 12 }, (_, i) => (
    <rect
      key={i}
      x="11.1"
      y="2"
      width="1.8"
      height="8"
      rx="0.9"
      fill={CLAUDE_CLAY}
      transform={`rotate(${i * 30} 12 12)`}
    />
  ));
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      {spokes}
    </svg>
  );
}

function OpenAiGlyph({ className }: GlyphProps) {
  // OpenAI — a six-petal knot rosette (three rotated blades), in currentColor.
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <g
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path
          d="M12 4a5 5 0 0 1 5 5v6a5 5 0 0 1-10 0V9a5 5 0 0 1 5-5z"
          transform="rotate(0 12 12)"
        />
        <path
          d="M12 4a5 5 0 0 1 5 5v6a5 5 0 0 1-10 0V9a5 5 0 0 1 5-5z"
          transform="rotate(60 12 12)"
        />
        <path
          d="M12 4a5 5 0 0 1 5 5v6a5 5 0 0 1-10 0V9a5 5 0 0 1 5-5z"
          transform="rotate(120 12 12)"
        />
      </g>
    </svg>
  );
}

function GeminiGlyph({ className }: GlyphProps) {
  // Google Gemini — a four-point spark with concave sides, blue->purple.
  const gid = useId();
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#4796E3" />
          <stop offset="1" stopColor="#9168C0" />
        </linearGradient>
      </defs>
      <path
        fill={`url(#${gid})`}
        d="M12 2c.6 5.2 3.8 8.4 9 9-5.2.6-8.4 3.8-9 9-.6-5.2-3.8-8.4-9-9 5.2-.6 8.4-3.8 9-9z"
      />
    </svg>
  );
}

function OllamaGlyph({ className }: GlyphProps) {
  // Ollama — a simple llama silhouette, monochrome.
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <g fill={OLLAMA_INK}>
        <path d="M6.5 3c-1 0-1.6 1-1.4 2l.5 3.2C4.7 9.4 4 11 4 12.8V19a1.4 1.4 0 0 0 2.8 0v-3.2c0-.5.7-.5.7 0V19a1.4 1.4 0 0 0 2.8 0v-2.4h3.4V19a1.4 1.4 0 0 0 2.8 0v-3.2c0-.5.7-.5.7 0V19a1.4 1.4 0 0 0 2.8 0v-6.2c0-1.8-.7-3.4-2.1-4.6L20.9 5c.2-1-.4-2-1.4-2-.7 0-1.3.5-1.5 1.2l-.5 2.3c-.9-.3-1.9-.5-3-.5s-2.1.2-3 .5L11 4.2C10.8 3.5 10.2 3 9.5 3z" />
        <circle cx="9.4" cy="10.4" r="1" fill="#111" />
        <circle cx="14.6" cy="10.4" r="1" fill="#111" />
      </g>
    </svg>
  );
}

/** The AI-curator card shows whichever provider is configured, else the fallback (no-AI mode). */
export function ProviderGlyph({
  provider,
  className,
  fallback = null,
}: {
  provider: string;
  className?: string;
  fallback?: ReactNode;
}) {
  switch (provider) {
    case "anthropic":
      return <ClaudeGlyph className={className} />;
    case "openai":
      return <OpenAiGlyph className={className} />;
    case "google":
      return <GeminiGlyph className={className} />;
    case "ollama":
      return <OllamaGlyph className={className} />;
    default:
      return <>{fallback}</>;
  }
}
