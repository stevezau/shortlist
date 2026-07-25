import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        border: "hsl(var(--border))",
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
        elevated: {
          DEFAULT: "hsl(var(--elevated))",
          foreground: "hsl(var(--elevated-foreground))",
        },
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
