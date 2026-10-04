import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { RowTemplateGallery } from "@/components/rows/row-template-gallery";
import type * as ApiModule from "@/lib/api";
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

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getRequestRowSources: () =>
        Promise.resolve({ overseerr: "connected", radarr: "off", sonarr: "off", complete: true, problems: [] }),
    },
  };
});

function renderGallery(onPick = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </MemoryRouter>
    );
  }
  render(<RowTemplateGallery open onPick={onPick} onClose={vi.fn()} />, { wrapper: Wrapper });
  return onPick;
}

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

describe("the gallery's AI filter", () => {
  it("shows Describe a row among all templates", () => {
    renderGallery();

    expect(screen.getByRole("button", { name: /^Describe a row.+/ })).toBeInTheDocument();
  });

  it("narrows to the AI template under its own chip", async () => {
    renderGallery();

    await userEvent.click(screen.getByRole("button", { name: "AI" }));

    expect(screen.getByRole("button", { name: /^Describe a row.+/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Picked for You.+/ })).not.toBeInTheDocument();
  });

  it("hides it under the other chips", async () => {
    renderGallery();

    await userEvent.click(screen.getByRole("button", { name: "Discover" }));

    expect(screen.queryByRole("button", { name: /^Describe a row.+/ })).not.toBeInTheDocument();
  });

  it("hands the template to the editor on Use template", async () => {
    const onPick = renderGallery();

    await userEvent.click(screen.getByRole("button", { name: /^Describe a row.+/ }));
    await userEvent.click(screen.getByRole("button", { name: /Use template/i }));

    expect(onPick).toHaveBeenCalledExactlyOnceWith(findRowTemplate("describe-a-row"));
  });
});
