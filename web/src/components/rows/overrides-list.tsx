import { Link } from "react-router";

/**
 * The "Server defaults" list: a heading, then one line per setting the row can override. Hidden
 * while no field has put a row in it (a shared row has none).
 */
export function OverridesList({ onTarget }: { onTarget: (element: HTMLElement | null) => void }) {
  return (
    <div className="-mx-5 -mb-5 hidden has-[[data-override-row]]:block">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-t px-5 pb-2 pt-4">
        <h3 className="text-sm font-medium text-muted-foreground">Server defaults</h3>
        <span className="text-sm text-muted-foreground">
          Change them for every row in{" "}
          <Link to="/settings#recommendations" className="underline underline-offset-2 hover:text-foreground">
            Settings
          </Link>
        </span>
      </div>
      <div ref={onTarget} className="flex flex-col" />
    </div>
  );
}
