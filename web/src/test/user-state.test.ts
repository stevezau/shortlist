import { describe, expect, it } from "vitest";

import { userState } from "@/lib/user-state";
import type { User } from "@/lib/types";

const user = (patch: Partial<User>) =>
  ({ enabled: true, restricted: false, restriction_profile: "", prefs: {}, ...patch }) as User;

describe("userState", () => {
  it("is Off for a restricted account with a profile, as the engine skips it", () => {
    expect(userState(user({ restricted: true, restriction_profile: "older_kid" }))).toBe("off");
  });

  it("is not Off for a profile on an account plex.tv does not report restricted: the engine still builds its rows", () => {
    expect(userState(user({ restricted: false, restriction_profile: "older_kid" }))).toBe("on");
  });

  it("is Off when switched off, and Paused only while enabled", () => {
    expect(userState(user({ enabled: false }))).toBe("off");
    expect(userState(user({ prefs: { paused: true } }))).toBe("paused");
  });
});
