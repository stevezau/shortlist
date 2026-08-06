import {
  encode,
  hasHome,
  hasLibrary,
  othersCount,
  ownerName,
  placementSummary,
} from "@/lib/placement";
import { Switch } from "@/components/ui/switch";
import { WatchingAccountLink } from "@/components/watching-account-link";
import type { Placement, User } from "@/lib/types";

/** A stable, valid HTML id fragment from a switch's label — "Owner Library Recommended" →
 *  "owner-library-recommended". The label is human copy with spaces, which is not a legal `id` on
 *  its own; `aria-describedby` silently resolves to nothing if fed the raw label. */
function slugify(label: string): string {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "");
}

/** Collapsed by default — the grid should read on its own, and this is here for the "wait, who sees
 *  what?" moment. Native <details> so it is keyboard- and screen-reader-accessible with no library. */
export function PlacementHelp({ isShared }: { isShared: boolean }) {
  return (
    <details className="group border-t pt-3">
      <summary className="cursor-pointer text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        How does this work?
      </summary>
      <div className="mt-3 space-y-3 text-xs text-muted-foreground">
        {isShared ? (
          <p>
            Everyone sees this <strong>same</strong> row &mdash; it&rsquo;s
            built from titles several people have watched, so there&rsquo;s
            nothing to keep apart.
          </p>
        ) : (
          <p>
            Everyone gets their <strong>own</strong> row. Plex keeps them apart
            by labelling each one and hiding everyone else&rsquo;s label from
            each person&rsquo;s share &mdash; so a friend only ever sees theirs.
          </p>
        )}
        <div className="space-y-2">
          <p>
            This decides <strong className="text-foreground">where</strong> the
            row appears, not who gets one &mdash; that&rsquo;s{" "}
            <em>Who gets it?</em> above.
          </p>
          <p>
            <strong className="text-foreground">Just me</strong> &mdash; the
            Plex account that owns this server: your own row, on your own
            screens.
          </p>
          <p>
            <strong className="text-foreground">Everyone else</strong> &mdash;
            people you&rsquo;ve shared with, plus Plex Home members. Plex treats
            them all the same here, and each sees only their own row.
          </p>
        </div>
        <div className="space-y-2">
          <p>
            <strong className="text-foreground">Recommended shelf</strong> puts
            a row on the shelf Plex shows at the top of that library.
          </p>
          <p>
            <strong className="text-foreground">Home screen</strong> puts it on
            a Home screen &mdash; yours in the first column, theirs in the
            second. Plex keeps those two apart, so your Home only ever shows
            your row.
          </p>
        </div>
        <p>
          Turn <strong>them all off</strong> and the row is still built and
          still private. It just doesn&rsquo;t claim a shelf &mdash;
          you&rsquo;ll find it in the library&rsquo;s Collections tab.
        </p>
        <p>
          The one thing Plex can&rsquo;t do: hide other people&rsquo;s rows from{" "}
          <strong className="text-foreground">your</strong> Recommended shelf.
          Hiding works through each person&rsquo;s share, and you don&rsquo;t
          have a share with yourself.
        </p>
      </div>
    </details>
  );
}

/**
 * "Where it shows" as a surface x audience grid. The two columns are real, not cosmetic: every
 * person gets their OWN Plex collection, so each of Plex's three flags is set per collection —
 * `promotedToRecommended` on the owner's vs on everyone else's, plus the two Home flags Plex
 * already splits by audience (`promotedToOwnHome` is owner-only; `promotedToSharedHome` covers
 * shared AND managed users — https://support.plex.tv/articles/manage-recommendations/).
 *
 * A SHARED row is the exception: one collection for everyone, so there is only one
 * `promotedToRecommended` to set and the Recommended pair collapses to a single control.
 */
export function PlacementToggles({
  placement,
  placementFriends,
  isShared,
  users,
  onChange,
}: {
  placement: Placement;
  placementFriends: Placement;
  isShared: boolean;
  users: User[];
  onChange: (placement: Placement, placementFriends: Placement) => void;
}) {
  const owner = ownerName(users);
  const others = othersCount(users);
  const ownerLibrary = hasLibrary(placement);
  const ownerHome = hasHome(placement);
  const friendsLibrary = hasLibrary(placementFriends);
  const friendsHome = hasHome(placementFriends);
  const allOff = placement === "off" && placementFriends === "off";

  // A shared row is ONE Plex collection, so its single `promotedToRecommended` flag cannot be split
  // by audience the way the two Home flags can — the engine ORs the pair (RowSpec.show_library).
  // Collapse it into one control rather than drawing two switches that silently move together.
  const sharedLibrary = ownerLibrary || friendsLibrary;
  const setSharedLibrary = (v: boolean) =>
    onChange(encode(v, ownerHome), encode(v, friendsHome));

  const cell = (
    checked: boolean,
    label: string,
    onToggle: (v: boolean) => void,
    /** Why this cell can't be set. Dims it and explains on hover, instead of the grid changing shape
     *  between row types — a control that moves or vanishes is harder to learn than one that stays
     *  put and says why it's unavailable. Disabled cells stay reachable: a native `disabled` switch
     *  drops out of the tab order entirely, so its explanation (and `aria-describedby`) would never
     *  be reached by keyboard or screen reader. */
    unavailable?: string,
  ) => {
    const describedBy = unavailable ? `${slugify(label)}-why` : undefined;
    return (
      <div className="flex justify-center">
        <Switch
          aria-label={label}
          checked={checked}
          onCheckedChange={unavailable ? () => {} : onToggle}
          aria-disabled={unavailable ? true : undefined}
          title={unavailable}
          aria-describedby={describedBy}
          className={unavailable ? "cursor-not-allowed opacity-40" : undefined}
        />
        {unavailable && (
          <span id={describedBy} className="sr-only">
            {unavailable}
          </span>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-3 rounded-md border p-4">
      {/* The two audience columns are EQUAL fixed widths, not `auto`. On a shared row the Recommended
          control is one switch spanning both (Plex has a single promotedToRecommended flag per
          collection, so it cannot differ by audience) — and with auto columns "Everyone else · 49
          other people" is far wider than "Just me · S_FLIX", so the centred switch drifted under the
          right-hand header and read as applying to everyone-but-you. */}
      <div className="grid grid-cols-[minmax(0,1fr)_7.5rem_7.5rem] items-center gap-x-6 gap-y-3">
        <span />
        <div className="text-center">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Just me
          </p>
          {owner && (
            <p className="text-[11px] font-normal normal-case text-muted-foreground/70">
              {owner}
            </p>
          )}
        </div>
        <div className="text-center">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Everyone else
          </p>
          {users.length > 0 && (
            <p className="text-[11px] font-normal normal-case text-muted-foreground/70">
              {others === 1 ? "1 other person" : `${others} other people`}
            </p>
          )}
        </div>

        <p className="text-sm">Recommended shelf</p>
        {isShared ? (
          // The grid keeps its shape for every row type. A shared row is ONE Plex collection with a
          // single `promotedToRecommended` flag, so "on for me, off for them" cannot be expressed —
          // the per-person cell is shown at its true value but dimmed, with the reason on hover,
          // rather than the row silently becoming a different control.
          <>
            {cell(
              sharedLibrary,
              "Owner Library Recommended",
              () => {},
              "A shared row is a single collection on Plex, so its Recommended shelf setting can't differ per person — it follows Everyone else.",
            )}
            {cell(
              friendsLibrary,
              "Friends Library Recommended",
              setSharedLibrary,
            )}
          </>
        ) : (
          <>
            {cell(ownerLibrary, "Owner Library Recommended", (v) =>
              onChange(encode(v, ownerHome), placementFriends),
            )}
            {cell(friendsLibrary, "Friends Library Recommended", (v) =>
              onChange(placement, encode(v, friendsHome)),
            )}
          </>
        )}

        <p className="text-sm">Home screen</p>
        {cell(ownerHome, "Owner Home", (v) =>
          onChange(encode(ownerLibrary, v), placementFriends),
        )}
        {cell(friendsHome, "Friends' Home", (v) =>
          onChange(placement, encode(friendsLibrary, v)),
        )}
      </div>

      {isShared && (
        <p className="text-xs text-muted-foreground">
          Everyone shares this one row, so its Recommended shelf setting applies
          to all of you at once.
        </p>
      )}

      {!allOff && (
        <p className="rounded-md bg-muted/50 p-3 text-sm">
          {placementSummary(
            ownerLibrary,
            ownerHome,
            friendsLibrary,
            friendsHome,
            isShared,
          )}
        </p>
      )}

      {!isShared && (
        <p className="rounded-md border border-dashed bg-muted/30 p-3 text-xs text-muted-foreground">
          The library&rsquo;s{" "}
          <strong className="text-foreground">Collections tab</strong> lists
          every collection on the server, so from your admin account
          you&rsquo;ll see one row there per person. Everyone else sees only
          their own row, wherever you&rsquo;ve placed it, plus their own
          Collections tab.
        </p>
      )}

      {friendsLibrary && !isShared && (
        <p className="rounded-md border border-dashed bg-muted/30 p-3 text-xs text-muted-foreground">
          {ownerLibrary
            ? "Everyone else’s rows show on your Recommended shelf too."
            : "Your row is off this shelf, but everyone else’s rows still show on your Recommended shelf."}{" "}
          Plex keeps each row private through the share you gave that person
          &mdash; and you own the server, so you have no share of your own for
          it to hide anything behind. Turn off{" "}
          <strong className="text-foreground">
            Everyone else &rarr; Recommended shelf
          </strong>{" "}
          to clear them from it, or keep the shelf and move your own watching to
          a separate account. <WatchingAccountLink />
        </p>
      )}

      {allOff && (
        <p className="rounded-md border border-dashed bg-muted/30 p-3 text-xs text-muted-foreground">
          This row won&rsquo;t appear on any Home screen or Recommended shelf.
          It&rsquo;s still built and kept private &mdash; you&rsquo;ll find it
          under the library&rsquo;s Collections tab.
        </p>
      )}

      <PlacementHelp isShared={isShared} />
    </div>
  );
}
