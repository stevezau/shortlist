import { describe, expect, it } from "vitest";

import { AI_KIND_META } from "@/lib/row-kind-meta";
import {
  AI_TEMPLATE_GROUP,
  AI_TEMPLATES,
  findRowTemplate,
  GALLERY_GROUPS,
  ROW_TEMPLATE_GROUPS,
  ROW_TEMPLATES,
} from "@/lib/row-templates";
import { blankInput } from "@/lib/collections";

describe("the Describe a row template", () => {
  const template = findRowTemplate("describe-a-row");

  it("is the one AI template, found by id so /rows/new?template=describe-a-row opens it", () => {
    expect(AI_TEMPLATES.map((t) => t.id)).toEqual(["describe-a-row"]);
    expect(template).toBe(AI_TEMPLATES[0]);
    expect(template?.kind).toBe("ai");
  });

  it("makes an enabled per-person row named after its theme, live like every other row", () => {
    expect(template?.values).toMatchObject({
      name: "{theme_emoji} {theme}",
      build: "per_person",
      enabled: true,
    });
    expect(template?.highlights.join(" ")).not.toMatch(/off until/i);
  });

  it("turns release-date weighting off explicitly rather than inheriting the global default", () => {
    expect(template?.values.recency).toBe(0);
  });

  it("sets only fields the row input has", () => {
    const allowed = new Set(Object.keys(blankInput()));
    for (const key of Object.keys(template?.values ?? {})) expect(allowed).toContain(key);
  });

  it("does not add AI Picks, which needs the explore mode of a later phase", () => {
    expect([...ROW_TEMPLATES, ...AI_TEMPLATES].map((t) => t.title)).not.toContain("AI Picks");
  });

  it("leaves the six kinds' own groups as they were and adds an AI group after them", () => {
    expect(ROW_TEMPLATE_GROUPS).toHaveLength(6);
    expect(GALLERY_GROUPS).toEqual([...ROW_TEMPLATE_GROUPS, AI_TEMPLATE_GROUP]);
    expect(AI_TEMPLATE_GROUP).toEqual({
      kind: "ai",
      heading: AI_KIND_META.title,
      description: AI_KIND_META.description,
    });
  });
});
