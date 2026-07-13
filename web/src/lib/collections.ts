import type { Collection, CollectionInput, User } from "@/lib/types";

/** A fresh row definition with sensible defaults, for the "Add a row" editor. */
export function blankInput(): CollectionInput {
  return {
    name: "",
    build: "per_person",
    audience: "everyone",
    audience_user_ids: [],
    enabled: true,
    size: 15,
    media: "both",
    sort_order: 0,
    name_template: "",
    min_watchers: 2,
    prompt: { tone: "balanced", guidance: "", template: "" },
  };
}

/** Project a saved collection onto the editable input shape the editor and PATCH share. */
export function toInput(collection: Collection): CollectionInput {
  return {
    name: collection.name,
    build: collection.build,
    audience: collection.audience,
    audience_user_ids: collection.audience_user_ids,
    enabled: collection.enabled,
    size: collection.size,
    media: collection.media,
    sort_order: collection.sort_order,
    name_template: collection.name_template,
    min_watchers: collection.min_watchers,
    prompt: {
      tone: collection.prompt.tone ?? "balanced",
      guidance: collection.prompt.guidance ?? "",
      template: collection.prompt.template ?? "",
    },
  };
}

/** One-line "who sees this row" summary for a row card. */
export function audienceSummary(collection: Collection, users: User[]): string {
  if (collection.audience === "everyone") return "Everyone";
  const names = collection.audience_user_ids
    .map((id) => users.find((u) => u.id === id)?.username)
    .filter(Boolean);
  if (names.length === 0) return "No one yet";
  return names.length <= 2
    ? names.join(" & ")
    : `${names.slice(0, 2).join(", ")} +${names.length - 2}`;
}
