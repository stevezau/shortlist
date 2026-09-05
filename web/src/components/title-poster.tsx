import { Clapperboard } from "lucide-react";
import { useState } from "react";

import { apiUrl } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * A title's artwork at a fixed size, with a same-size placeholder for everything that can go wrong.
 *
 * Two sources, because the two lists show different kinds of title. An INBOX title is not on the
 * server yet, so its only artwork is TMDB's. A delivered PICK is in the library by construction, so
 * its artwork is the one the owner actually has in Plex — including whatever Kometa or TMM put
 * there. Passing a `ratingKey` uses the PMS proxy; passing a `posterPath` uses TMDB's CDN.
 *
 * The placeholder is the same box as the image, never smaller: a list that swaps a 58x87 poster for
 * a 24px icon reflows every time one title lacks art.
 */
export function TitlePoster({
  ratingKey,
  posterPath,
  className,
}: {
  /** A delivered pick's Plex ratingKey. `0` means the pipeline never matched it to a library item. */
  ratingKey?: number | null;
  /** An inbox title's TMDB poster path. */
  posterPath?: string | null;
  className?: string;
}) {
  // Both sources are things this app cannot check at render time: TMDB's CDN is a third-party host
  // behind whatever network the browser is on, and the PMS proxy 404s a ratingKey that went stale
  // when a title was removed and re-added. Both fail at LOAD time, long after the value looked fine,
  // so falling back on error is what keeps that a tidy tile instead of a broken-image icon per row.
  const [failed, setFailed] = useState(false);

  const src = ratingKey
    ? apiUrl(`/api/picks/${ratingKey}/poster`)
    : posterPath
      ? // `w154` is the smallest TMDB bucket that still looks sharp at this size on a 2x display, so
        // a 40-title inbox costs a few hundred KB rather than megabytes.
        `https://image.tmdb.org/t/p/w154${posterPath}`
      : null;

  const box = cn(
    // 58x87 is 2:3, the size the request inbox already uses. 40x60 below `sm` is what makes 320px
    // work: it leaves a 236px text column once the card padding and gap are paid for.
    "h-[60px] w-[40px] shrink-0 rounded border sm:h-[87px] sm:w-[58px]",
    className,
  );

  if (!src || failed) {
    return (
      <div
        className={cn(box, "flex items-center justify-center bg-muted")}
        aria-hidden="true"
      >
        <Clapperboard className="h-4 w-4 text-muted-foreground/60 sm:h-5 sm:w-5" />
      </div>
    );
  }
  return (
    <img
      src={src}
      // Decorative: the title is right beside it as real text, so announcing it twice is noise.
      alt=""
      loading="lazy"
      decoding="async"
      width={58}
      height={87}
      onError={() => setFailed(true)}
      className={cn(box, "object-cover")}
    />
  );
}
