/** Collapse repeated titles into "title ×N", keeping the order each title was first seen. */
export function groupTitles(titles: string[]): string[] {
  const counts = new Map<string, number>();
  for (const title of titles) counts.set(title, (counts.get(title) ?? 0) + 1);
  return [...counts].map(([title, count]) => (count > 1 ? `${title} ×${count}` : title));
}

type CollectionGroup = { library: string; people: { person: string; titles: string[] }[] };

/** Group the collections an uninstall deletes as library, then person, then their rows, each in the
 *  order first seen. A title repeated within one person's list collapses to "title ×N". */
export function groupCollections(items: { library: string; person: string; title: string }[]): CollectionGroup[] {
  const libraries = new Map<string, Map<string, string[]>>();
  for (const { library, person, title } of items) {
    const people = libraries.get(library) ?? new Map<string, string[]>();
    people.set(person, [...(people.get(person) ?? []), title]);
    libraries.set(library, people);
  }
  return [...libraries].map(([library, people]) => ({
    library,
    people: [...people].map(([person, titles]) => ({ person, titles: groupTitles(titles) })),
  }));
}
