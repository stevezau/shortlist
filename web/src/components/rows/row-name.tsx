import { LIBRARY_NAME, PLACEHOLDER_EXACT, PLACEHOLDER_SPLIT } from "@/lib/placeholders";

/**
 * A row's configured name, rendered honestly wherever the app names a row.
 *
 * A row is configured as a TEMPLATE — "✨ {library_name} Picked for You" — and becomes one
 * collection per library, so there is no single rendered name to show. Printing the raw template
 * reads as a substitution that failed; stripping the token turned "📬 {library_name} you asked for"
 * into "📬 you asked for". So: where the caller knows the library (a per-library card or section),
 * `libraryName` fills it, and any placeholder left is drawn as a chip that says what it is.
 */
export function RowName({
  name,
  libraryName,
  className = "font-medium",
}: {
  name: string;
  /** The one library this rendering is about, when there is one. */
  libraryName?: string;
  className?: string;
}) {
  // The engine collapses the gap a filled token leaves, so match it — never "📬  you asked for".
  const filled =
    libraryName === undefined
      ? name
      : name.replaceAll(LIBRARY_NAME, libraryName).replace(/\s+/g, " ").trim();
  const parts = filled.split(PLACEHOLDER_SPLIT);
  return (
    <span className={className}>
      {parts.map((part, i) =>
        PLACEHOLDER_EXACT.test(part) ? (
          <span
            key={i}
            className="mx-0.5 rounded bg-muted px-1 py-0.5 text-xs font-normal text-muted-foreground"
          >
            {part.slice(1, -1).replace(/_/g, " ")}
          </span>
        ) : (
          part
        ),
      )}
    </span>
  );
}
