import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { RowTemplateGallery } from "@/components/rows/row-template-gallery";
import { RowEditor } from "@/components/rows/row-editor";
import type * as ApiModule from "@/lib/api";
import { blankInput } from "@/lib/collections";
import { ROW_TEMPLATES, findRowTemplate } from "@/lib/row-templates";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      createCollection: vi.fn(() => Promise.resolve({ id: 1 })),
      getSettings: () => Promise.resolve({}),
      getLibraries: () => Promise.resolve([]),
      getImageProvider: () =>
        Promise.resolve({ capable: false, provider: "", reason: "" }),
    },
  };
});

function renderGallery(onPick = vi.fn()) {
  render(<RowTemplateGallery open onPick={onPick} onClose={() => {}} />);
  return onPick;
}

describe("ROW_TEMPLATES", () => {
  it("only sets fields the row input actually has", () => {
    // A template that names a field the API doesn't accept would 422 on save with no clue why.
    const allowed = new Set(Object.keys(blankInput()));
    for (const template of ROW_TEMPLATES) {
      for (const key of Object.keys(template.values)) {
        expect(allowed, `${template.id} sets unknown field "${key}"`).toContain(
          key,
        );
      }
    }
  });

  it("gives every template a unique id and something to say about itself", () => {
    const ids = ROW_TEMPLATES.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const template of ROW_TEMPLATES) {
      expect(template.highlights.length).toBeGreaterThan(0);
      expect(template.blurb.length).toBeGreaterThan(0);
    }
  });

  it("keeps a {top_seed} row down to the one watch it names", () => {
    // The whole point of the template: at the default budget the row names one watch and fills
    // itself from the other 29, so the title claims something the contents don't honour.
    const template = findRowTemplate("because-you-watched");
    expect(template?.values.name_template).toContain("{top_seed}");
    expect(template?.values.max_seeds).toBe(1);
    // A single watch is a movie OR a show, so a "both" row at 1 seed leaves half of it empty.
    expect(template?.values.media).not.toBe("both");
  });
});

describe("RowTemplateGallery", () => {
  it("offers every template plus a way to skip them", async () => {
    const onPick = renderGallery();

    for (const template of ROW_TEMPLATES) {
      expect(screen.getByText(template.title)).toBeInTheDocument();
    }

    await userEvent.click(
      screen.getByRole("button", { name: /Start from scratch/i }),
    );
    expect(onPick).toHaveBeenCalledWith(null);
  });

  it("hands back the template that was clicked", async () => {
    const onPick = renderGallery();

    await userEvent.click(
      screen.getByRole("button", { name: /Comfort rewatch/i }),
    );

    expect(onPick).toHaveBeenCalledWith(
      expect.objectContaining({ id: "comfort-rewatch" }),
    );
  });
});

describe("RowEditor seeded from a template", () => {
  function renderEditor(templateId: string) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <RowEditor
            collection={null}
            template={findRowTemplate(templateId) ?? null}
            users={[]}
            onClose={() => {}}
          />
        </QueryClientProvider>
      </MemoryRouter>,
    );
  }

  it("prefills the fields the template sets", () => {
    renderEditor("comfort-rewatch");

    expect(screen.getByLabelText(/^Name$/i)).toHaveValue("Comfort rewatch");
    // watched_pct 1 → the slider is shown (not inheriting) and reads 100%.
    expect(
      screen.getByRole("slider", {
        name: /Maximum share of the row that may be already-watched/i,
      }),
    ).toHaveValue("100");
  });

  it("says which template it started from, so prefilled fields aren't a mystery", () => {
    renderEditor("fresh-finds");

    expect(screen.getByText(/Started from/)).toBeInTheDocument();
    expect(screen.getByText(/🌱 Fresh finds/)).toBeInTheDocument();
  });

  it("says nothing about templates when editing from scratch", () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <RowEditor collection={null} users={[]} onClose={() => {}} />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    expect(screen.queryByText(/Started from/)).toBeNull();
    expect(screen.getByLabelText(/^Name$/i)).toHaveValue("");
  });
});
