import type { ConnectionTestResult } from "@/lib/types";

/**
 * A failed connection test in plain English.
 *
 * The server reports an exception as `Type: message` ("ConnectError: All connection attempts
 * failed"), which says what broke inside the code and nothing about what to do. The three failures
 * people actually hit — nothing answering at the address, a refused key, a slow answer — get a
 * sentence with the next step; anything else is left as the server wrote it, because guessing at an
 * unfamiliar error is worse than showing it.
 */
export function plainTestMessage(result: ConnectionTestResult, service: string, address?: string): string {
  if (result.ok) return result.message;
  const text = result.message;
  const at = address ? ` at ${address}` : "";
  if (/\b(401|403)\b|unauthori[sz]ed|forbidden|invalid api key|invalid token/i.test(text))
    return `${service} refused the key${at} — check it, then Test`;
  if (/timeout|timed out/i.test(text)) return `${service} did not answer in time${at} — check the address, then Test`;
  if (/connect|connection|refused|resolve|name or service|no route|unreachable/i.test(text))
    return `Can’t reach ${service}${at} — check the address, then Test`;
  return text;
}
