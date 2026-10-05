import { PLACEHOLDERS, THEME_PLACEHOLDERS } from "@/lib/placeholders";

/**
 * The placeholders a row name or poster text can carry (`lib/placeholders.ts`).
 *
 * Shown wherever one of those fields is edited. Without it the fields look like plain text boxes,
 * so nobody discovers that a per-person row can say each person's own name.
 */
export function TemplateVarsHint({ seasonal = false, themed = false }: { seasonal?: boolean; themed?: boolean }) {
  // The season placeholders only mean something on a row that follows seasons. Anywhere else a name using
  // them is refused, and poster text renders them blank, so offering them there offers nothing.
  // The theme placeholders are an AI row's alone, and refused anywhere else.
  const variables = [...PLACEHOLDERS.filter((p) => seasonal || !p.seasonal), ...(themed ? THEME_PLACEHOLDERS : [])];
  return (
    <p className="text-sm text-muted-foreground">
      Use{" "}
      {variables.map((v, i) => (
        <span key={v.token}>
          {i > 0 ? (i === variables.length - 1 ? ", or " : ", ") : ""}
          <span className="font-mono">{v.token}</span> for {v.meaning}
        </span>
      ))}
      .
    </p>
  );
}
