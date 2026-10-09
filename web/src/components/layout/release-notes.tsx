import Markdown, { type Components } from "react-markdown";

const sectionHeading = "text-sm font-semibold text-foreground";

// `node` is react-markdown's syntax-tree node, passed to every override. Spread onto a DOM element it
// would be rendered as an attribute, so each override drops it. Every heading level renders as one
// small heading: release bodies use `###` for their sections, and the dialog's title sits above them.
const heading: Components["h3"] = ({ node: _node, ...props }) => (
  <h3 className={sectionHeading} {...props} />
);

const NOTES: Components = {
  h1: heading,
  h2: heading,
  h3: heading,
  h4: ({ node: _node, ...props }) => (
    <h4 className={sectionHeading} {...props} />
  ),
  p: ({ node: _node, ...props }) => <p {...props} />,
  ul: ({ node: _node, ...props }) => (
    <ul className="list-disc space-y-2 pl-5" {...props} />
  ),
  ol: ({ node: _node, ...props }) => (
    <ol className="list-decimal space-y-2 pl-5" {...props} />
  ),
  li: ({ node: _node, ...props }) => (
    <li className="space-y-2 pl-1" {...props} />
  ),
  strong: ({ node: _node, ...props }) => (
    <strong className="font-semibold text-foreground" {...props} />
  ),
  code: ({ node: _node, ...props }) => (
    <code
      className="rounded bg-muted px-1 py-0.5 font-mono text-xs text-foreground"
      {...props}
    />
  ),
  a: ({ node: _node, ...props }) => (
    <a
      {...props}
      target="_blank"
      rel="noopener noreferrer"
      className="text-primary underline-offset-4 hover:underline"
    />
  ),
};

/**
 * One release's markdown body, as formatted text.
 *
 * react-markdown renders no raw HTML and blanks `javascript:` links by default. That is all the trust
 * a GitHub release body should get inside the owner's session: anyone with write access to the repo
 * can edit one. Do not add `rehype-raw`.
 */
export function ReleaseNotes({ markdown }: { markdown: string }) {
  return <Markdown components={NOTES}>{markdown}</Markdown>;
}
