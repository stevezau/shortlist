import { useEffect, useState } from "react";
import { useLocation } from "react-router";

import { SETTINGS_SECTIONS, settingsSectionForHash } from "@/components/settings/sections";

/** Follow the section being read, even when the URL still names a previously clicked anchor. */
export function useActiveSettingsSection() {
  const { pathname, hash } = useLocation();
  const [active, setActive] = useState(() => settingsSectionForHash(hash));

  useEffect(() => {
    if (pathname !== "/settings") return;
    let frame = 0;
    const update = () => {
      const sections = SETTINGS_SECTIONS.map(({ id }) => document.getElementById(id)).filter((node): node is HTMLElement => Boolean(node));
      if (!sections.length) return;
      const readingLine = window.matchMedia?.("(max-width: 767px)").matches ? 160 : 120;
      let current = sections[0]!;
      for (const section of sections) {
        if (section.getBoundingClientRect().top <= readingLine) current = section;
        else break;
      }
      // The final short section may never reach the reading line before the page ends.
      if (document.documentElement.scrollHeight > window.innerHeight && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) current = sections.at(-1)!;
      setActive(current.id);
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(update); };
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
    const observeSections = () => {
      for (const { id } of SETTINGS_SECTIONS) {
        const section = document.getElementById(id);
        if (section) resize?.observe(section);
      }
      schedule();
    };
    const mounted = new MutationObserver(observeSections);
    mounted.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    observeSections();
    return () => {
      cancelAnimationFrame(frame);
      mounted.disconnect();
      resize?.disconnect();
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, [pathname]);

  return active;
}
