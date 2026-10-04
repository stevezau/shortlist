/** Collapse repeated titles into "title ×N", keeping the order each title was first seen. */
export function groupTitles(titles: string[]): string[] {
  const counts = new Map<string, number>();
  for (const title of titles) counts.set(title, (counts.get(title) ?? 0) + 1);
  return [...counts].map(([title, count]) => (count > 1 ? `${title} ×${count}` : title));
}
