import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        // Self-hosted (@fontsource, imported in main.tsx): Shortlist runs on LANs with no internet,
        // so a CDN font would silently fall back to the system face on exactly those installs.
        // Quoted INSIDE the string: Tailwind emits names verbatim, and an unquoted family whose words
        // include a bare number ("Source Sans 3") is invalid CSS — the browser drops the whole
        // declaration and falls back to its default serif.
        sans: ['"Source Sans 3 Variable"', '"Segoe UI"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      colors: {
        border: {
          DEFAULT: "hsl(var(--border))",
          strong: "hsl(var(--border-strong))",
        },
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
          // `text-destructive-text` — the AA-safe red for WORDS. See index.css for why the fill
          // colour cannot serve both jobs.
          text: "hsl(var(--destructive-text))",
        },
        success: {
          DEFAULT: "hsl(var(--success))",
          foreground: "hsl(var(--success-foreground))",
        },
        warning: {
          DEFAULT: "hsl(var(--warning))",
          foreground: "hsl(var(--warning-foreground))",
        },
        plex: {
          DEFAULT: "hsl(var(--plex))",
          foreground: "hsl(var(--plex-foreground))",
        },
        support: "hsl(var(--support))",
        elevated: {
          DEFAULT: "hsl(var(--elevated))",
          foreground: "hsl(var(--elevated-foreground))",
        },
        // The selected-state surface (`lib/selected.ts`): one step above `elevated`, so a chosen
        // nav item, tab or chip reads as raised rather than filled amber.
        raised: "hsl(var(--raised))",
        "faint-foreground": "hsl(var(--faint-foreground))",
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
      },
      borderRadius: {
        xl: "calc(var(--radius) + 4px)",
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      boxShadow: {
        // Depth on a dark canvas comes from a soft drop plus a hairline top highlight — plain
        // black shadows are invisible here.
        elevated:
          "0 1px 0 0 hsl(0 0% 100% / 0.04) inset, 0 8px 24px -12px hsl(240 40% 2% / 0.7)",
        glow: "0 0 0 1px hsl(var(--primary) / 0.25), 0 8px 30px -8px hsl(var(--primary) / 0.35)",
        // The 2px amber edge that marks a selected item: along the bottom for horizontal controls
        // (segments, chips), along the left for vertical lists (the nav rail, settings sub-nav).
        "selected-x": "inset 0 -2px 0 0 hsl(var(--primary))",
        "selected-y": "inset 2px 0 0 0 hsl(var(--primary))",
      },
      keyframes: {
        // Welcome-step mock: the Picked-for-You row appearing on a Plex Home.
        "row-in": {
          "0%": { opacity: "0", transform: "translateY(14px)" },
          "12%": { opacity: "1", transform: "translateY(0)" },
          "88%": { opacity: "1", transform: "translateY(0)" },
          "100%": { opacity: "0", transform: "translateY(14px)" },
        },
        "fade-in": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        // The mobile nav drawer sliding in from the left edge.
        "slide-in-left": {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(0)" },
        },
        // Indeterminate progress: a sliver sweeps left-to-right while an opaque call is in flight.
        "progress-indeterminate": {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(400%)" },
        },
      },
      animation: {
        "row-in": "row-in 7s ease-in-out infinite",
        "fade-in": "fade-in 0.3s ease-out",
        "slide-in-left": "slide-in-left 0.2s ease-out",
        "progress-indeterminate":
          "progress-indeterminate 1.2s ease-in-out infinite",
      },
    },
  },
  plugins: [animate],
} satisfies Config;
