---
globs: "web/**/*.{ts,tsx,css}"
---

# Frontend Conventions (React + TypeScript)

- TypeScript strict mode; `any` is banned (use `unknown` + narrowing)
- Function components + hooks only; components small and single-purpose
- Server state via TanStack Query; no global state store unless a real cross-page need appears
- API types are **generated from the OpenAPI schema** (`pnpm -C web gen:api`) — never hand-write
  request/response types
- UI primitives from shadcn/ui; style with Tailwind tokens only (no arbitrary hex values — extend
  the theme instead)
- Every data view handles all four states: loading (skeleton), error (message + retry), empty
  (explains why + what to do), success
- Live progress uses the shared SSE hook (`lib/sse.ts`) — one EventSource per page, not per widget
- Accessibility: real `<button>`/`<label>` elements, visible `:focus-visible` state, respect
  `prefers-reduced-motion`
- Copy follows the design doc's voice: plain English, controls say exactly what happens, errors say
  what went wrong and how to fix it — never raw error codes
- Logic in hooks/utils gets vitest coverage; components with branching UI get testing-library tests
