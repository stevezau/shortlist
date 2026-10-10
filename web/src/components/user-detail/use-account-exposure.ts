import { rowsNotHidden } from "@/lib/privacy-attention";
import { usePrivacyStatus } from "@/lib/queries";
import type { User } from "@/lib/types";

/**
 * How many rows this account can see that are not theirs, with the evidence it rests on.
 *
 * `live` is true when the live privacy reading covers this person; `exposed` is then its count. When
 * it does not, the run's own count (`lastRun`) is the only evidence left, so callers label it.
 */
export function useAccountExposure(user: User) {
  const privacy = usePrivacyStatus();
  const account = privacy.data?.accounts?.find((a) => a.user_id === user.id);
  const live = Boolean(account && privacy.data && !privacy.data.error && !privacy.data.rows_error);
  const exposed = account && privacy.data && live ? rowsNotHidden(account, privacy.data) : 0;
  return { live, exposed, lastRun: user.unhidden_rows, isPending: privacy.isPending };
}
