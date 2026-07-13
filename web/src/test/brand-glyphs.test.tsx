import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProviderGlyph } from "@/components/brand-glyphs";

/** The AI-curator card renders whichever provider is configured, else a fallback (no-AI mode). */
describe("ProviderGlyph", () => {
  it.each(["anthropic", "openai", "google", "ollama"])(
    "renders a mark for the %s provider",
    (provider) => {
      const { container } = render(<ProviderGlyph provider={provider} />);
      expect(container.querySelector("svg")).not.toBeNull();
    },
  );

  it("renders the fallback for an unknown provider", () => {
    // The previous implementation used `<Glyph/> ?? fallback`, which never fell back because a JSX
    // element is always truthy. Guard that regression: an unknown provider must show the fallback.
    const { getByTestId } = render(
      <ProviderGlyph
        provider="something-else"
        fallback={<span data-testid="fallback" />}
      />,
    );
    expect(getByTestId("fallback")).toBeInTheDocument();
  });

  it("renders the fallback for the empty no-AI provider", () => {
    const { getByTestId } = render(
      <ProviderGlyph provider="" fallback={<span data-testid="fallback" />} />,
    );
    expect(getByTestId("fallback")).toBeInTheDocument();
  });

  it("renders nothing when an unknown provider has no fallback", () => {
    const { container } = render(<ProviderGlyph provider="none" />);
    expect(container.querySelector("svg")).toBeNull();
  });
});
