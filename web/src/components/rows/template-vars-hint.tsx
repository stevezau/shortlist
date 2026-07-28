import { renderRowName } from "@/lib/format";

const VARIABLES = [
  { token: "{user}", meaning: "the person’s name" },
  { token: "{library_name}", meaning: "the Plex library the row lands in" },
  { token: "{top_seed}", meaning: "a title they recently watched" },
] as const;

/**
 * The three placeholders a row name or poster text can carry — `render_row_name` in
 * engine/delivery.py is the substituting side, and this list must stay in step with it.
 *
 * Shown wherever one of those fields is edited. Without it the fields look like plain text boxes,
 * so nobody discovers that a per-person row can say each person's own name.
 */
export function TemplateVarsHint() {
  return (
    <p className="text-sm text-muted-foreground">
      Use{" "}
      {VARIABLES.map((v, i) => (
        <span key={v.token}>
          {i > 0 ? (i === VARIABLES.length - 1 ? ", or " : ", ") : ""}
          <span className="font-mono">{v.token}</span> for {v.meaning}
        </span>
      ))}
      .
    </p>
  );
}

/**
 * The hint plus a worked example of what the template becomes on Plex, for the fields where the
 * result is a title people will actually read (a row's name). Filled with sample values — the real
 * ones differ per person and per library, which is the whole point of the placeholders.
 */
export function TemplateVarsHintWithPreview({
  template,
}: {
  template: string;
}) {
  const hasVariable = VARIABLES.some((v) => template.includes(v.token));
  return (
    <div className="space-y-1">
      <TemplateVarsHint />
      {hasVariable && (
        <p className="text-sm text-muted-foreground">
          Sarah, browsing Movies, would see{" "}
          <span className="font-medium text-foreground">
            “{renderRowName(template)}”
          </span>
          .
        </p>
      )}
    </div>
  );
}
