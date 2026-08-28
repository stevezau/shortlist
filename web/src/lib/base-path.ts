declare global {
  interface Window {
    __SHORTLIST_BASE_PATH__?: string;
  }
}

/** Prefix the app is served under, injected into the shell by the server. */
export function readBasePath(source: Pick<Window, "__SHORTLIST_BASE_PATH__"> | undefined): string {
  const raw = source?.__SHORTLIST_BASE_PATH__;
  if (typeof raw !== "string") return "";
  const trimmed = raw.trim();
  if (!trimmed || trimmed === "/") return "";
  const withLeading = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return withLeading.replace(/\/+$/, "");
}

export const basePath = readBasePath(typeof window === "undefined" ? undefined : window);
