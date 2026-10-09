import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { BackLink } from "@/components/back-link";
import { PageHeader } from "@/components/page-header";
import { PeopleBrowser } from "@/components/rows/people-browser";
import { RowName } from "@/components/rows/row-name";
import { RowPlexCard } from "@/components/rows/row-plex-card";
import { reachedUsers } from "@/components/rows/row-facts";
import { TemplateVarsHint } from "@/components/rows/template-vars-hint";
import { Segmented } from "@/components/segmented";
import { UserAvatar } from "@/components/user-avatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { apiErrorMessage } from "@/lib/api";
import { blankInput, OVER_TIME_DEFAULTS } from "@/lib/collections";
import { describeCron } from "@/lib/cron";
import {
  useRequestRowSources,
  useSaveCollection,
  useUsers,
} from "@/lib/queries";
import {
  CONNECTIONS_SETTINGS,
  NO_REQUEST_SOURCE,
  noRequestSource,
} from "@/lib/row-kinds";
import {
  AI_TEMPLATES,
  findRowTemplate,
  GALLERY_GROUPS,
  ROW_TEMPLATES,
  type RowTemplate,
} from "@/lib/row-templates";
import { selectedClass } from "@/lib/selected";
import type { CollectionInput, User } from "@/lib/types";
import { cn } from "@/lib/utils";

const ALL_TEMPLATES = [...ROW_TEMPLATES, ...AI_TEMPLATES];

/** The templates of one kind, in the gallery's order. */
function templatesOfKind(kind: string): RowTemplate[] {
  return ALL_TEMPLATES.filter((template) => template.kind === kind);
}

function templateName(template: RowTemplate): string {
  return template.values.name ?? template.title;
}

function personName(user: User): string {
  return user.display_name || user.username;
}

/** Step 1: the kinds of row, one tile each. */
function KindTiles({
  selectedKind,
  onPick,
}: {
  selectedKind: string;
  onPick: (kind: string) => void;
}) {
  return (
    <div
      className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"
      role="group"
      aria-label="Kinds of row"
    >
      {GALLERY_GROUPS.map((group) => {
        const first = templatesOfKind(group.kind)[0];
        if (!first) return null;
        const active = group.kind === selectedKind;
        return (
          <button
            key={group.kind}
            type="button"
            aria-pressed={active}
            onClick={() => onPick(group.kind)}
            className={cn(
              "rounded-xl border bg-card p-4 text-left shadow-elevated transition-colors hover:bg-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none",
              active && selectedClass,
            )}
          >
            <span className="flex items-start justify-between gap-2">
              <span className="font-medium">{group.heading}</span>
              <Badge variant="outline" className="shrink-0 whitespace-nowrap font-normal text-muted-foreground">
                {first.values.build === "shared" ? "shared" : "per person"}
              </Badge>
            </span>
            <span className="mt-1 block text-sm text-muted-foreground">{group.description}</span>
          </button>
        );
      })}
    </div>
  );
}

/** Step 3: everyone, or a hand-picked few. */
function WhoGetsIt({
  users,
  audience,
  chosen,
  reach,
  onChange,
}: {
  users: User[];
  audience: CollectionInput["audience"];
  chosen: number[];
  reach: User[];
  onChange: (patch: Pick<CollectionInput, "audience" | "audience_user_ids">) => void;
}) {
  const names = reach.map(personName);
  return (
    <div className="rounded-xl border bg-card p-4 shadow-elevated">
      <Segmented
        ariaLabel="Who gets this row"
        value={audience}
        onChange={(next) => onChange({ audience: next, audience_user_ids: chosen })}
        options={[
          { value: "everyone", label: `Everyone with a row (${reachedUsers({ audience: "everyone", audience_user_ids: [] }, users).length})` },
          { value: "subset", label: "Choose people" },
        ]}
      />
      {audience === "everyone" ? (
        <p className="mt-3 text-sm text-muted-foreground">
          {names.length === 0
            ? "No one is enabled yet. Enable people on the Users page."
            : `${names.join(", ")}. People you enable later get it automatically.`}
        </p>
      ) : (
        <div className="mt-3 space-y-2">
          <p className="text-sm text-muted-foreground">
            {chosen.length === 0
              ? "Nobody chosen. Pick at least one person."
              : `${chosen.length} of ${users.length} ${users.length === 1 ? "person" : "people"} chosen. Disabled or paused people get no copy until they are enabled and unpaused.`}
          </p>
          <PeopleBrowser users={users}>
            {(visible) => (
              <ul aria-label="People" className="divide-y">
                {visible.map((user) => (
                  <li key={user.id} className="flex min-w-0 items-center justify-between gap-3 py-2">
                    <span className="flex min-w-0 items-center gap-2 text-sm">
                      <UserAvatar name={user.username} size="sm" />
                      <span className="min-w-0 break-words">{personName(user)}</span>
                    </span>
                    <Switch
                      checked={chosen.includes(user.id)}
                      onCheckedChange={(checked) =>
                        onChange({
                          audience: "subset",
                          audience_user_ids: checked
                            ? [...chosen, user.id]
                            : chosen.filter((id) => id !== user.id),
                        })
                      }
                      aria-label={user.username}
                    />
                  </li>
                ))}
              </ul>
            )}
          </PeopleBrowser>
        </div>
      )}
    </div>
  );
}

/**
 * The add-a-row screen: pick a kind, name it, choose who gets it. Everything else starts from the
 * kind's template and is fine-tuned in the row editor once the row exists.
 *
 * `/rows/new?template=<id>` opens with that template selected. `/rows/new/full` is the full editor
 * for a row built from scratch, and where an AI row goes to write its list.
 */
export function RowNewPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const usersQuery = useUsers();
  const users = usersQuery.data ?? [];
  const save = useSaveCollection();

  const initial = findRowTemplate(params.get("template") ?? "") ?? ROW_TEMPLATES[0]!;
  const [template, setTemplate] = useState<RowTemplate>(initial);
  const [name, setName] = useState(templateName(initial));
  const [audience, setAudience] = useState<CollectionInput["audience"]>("everyone");
  const [chosen, setChosen] = useState<number[]>([]);

  const sources = useRequestRowSources("", template.values.requests_row === true);
  const needsRequestSource =
    template.values.requests_row === true && sources.data !== undefined && noRequestSource(sources.data);
  const isAi = template.kind === "ai";

  const pickTemplate = (next: RowTemplate) => {
    setTemplate(next);
    setName(templateName(next));
  };

  const input: CollectionInput = {
    ...blankInput(),
    ...template.values,
    name: name.trim(),
    audience,
    audience_user_ids: audience === "subset" ? chosen : [],
  };
  const reach = reachedUsers(input, users);
  const peopleForPreview = reach.slice(0, 3);
  const variants = templatesOfKind(template.kind);
  const schedule = describeCron(input.schedule) || "At the next scheduled run";
  const problem = !name.trim()
    ? "Give the row a name."
    : audience === "subset" && chosen.length === 0
      ? "Choose at least one person."
      : needsRequestSource
        ? NO_REQUEST_SOURCE
        : null;

  const submit = () => {
    if (isAi) {
      // An AI row needs its list written first, and that happens in the full editor.
      navigate(`/rows/new/full?template=${template.id}`);
      return;
    }
    save.mutate(
      { id: null, body: { ...input, theme_id: null, ...OVER_TIME_DEFAULTS, hub_anchor: {} } },
      { onSuccess: (created) => navigate(`/rows/${created.id}`) },
    );
  };

  return (
    <div className="space-y-4">
      <BackLink to="/rows" label="Rows" />
      <PageHeader
        title="Add a row"
        subtitle="Pick a kind, name it, choose who gets it. Nothing reaches Plex until you add it."
        className="mb-2"
      />

      <div className="grid gap-8 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-8">
          <section aria-labelledby="new-row-kind">
            <h2 id="new-row-kind" className="mb-3 text-lg font-semibold">
              <span className="mr-2 text-muted-foreground">1</span>Pick a kind of row
            </h2>
            <KindTiles selectedKind={template.kind} onPick={(kind) => pickTemplate(templatesOfKind(kind)[0]!)} />
            {variants.length > 1 && (
              <div className="mt-4 space-y-2">
                <p className="text-sm font-medium text-muted-foreground">Start from</p>
                <Segmented
                  ariaLabel="Starting point"
                  value={template.id}
                  onChange={(id) => pickTemplate(findRowTemplate(id) ?? template)}
                  options={variants.map((variant) => ({
                    value: variant.id,
                    label: `${variant.emoji} ${variant.title}`,
                  }))}
                />
                <p className="text-sm text-muted-foreground">{template.blurb}</p>
              </div>
            )}
          </section>

          <section aria-labelledby="new-row-name">
            <h2 id="new-row-name" className="mb-3 text-lg font-semibold">
              <span className="mr-2 text-muted-foreground">2</span>Name it
            </h2>
            <div className="rounded-xl border bg-card shadow-elevated">
              <div className="space-y-2 p-4">
                <Input
                  aria-label="Row name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="e.g. ✨ Hidden Gems for {user}"
                />
                <TemplateVarsHint seasonal={template.kind === "seasonal"} themed={isAi} />
              </div>
              {peopleForPreview.length > 0 && input.build !== "shared" && (
                <div className="border-t">
                  <p className="px-4 pb-1 pt-3 text-sm font-medium text-muted-foreground">What each person sees</p>
                  <ul className="divide-y text-sm">
                    {peopleForPreview.map((user) => (
                      <li key={user.id} className="flex items-center gap-4 px-4 py-2.5">
                        <span className="w-20 shrink-0 truncate text-muted-foreground">{personName(user)}</span>
                        <span className="text-muted-foreground" aria-hidden="true">→</span>
                        <RowName
                          name={name.replaceAll("{user}", personName(user))}
                          libraryName={input.media === "show" ? "TV Shows" : "Movies"}
                          className="min-w-0 [overflow-wrap:anywhere]"
                        />
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </section>

          <section aria-labelledby="new-row-who">
            <h2 id="new-row-who" className="mb-3 text-lg font-semibold">
              <span className="mr-2 text-muted-foreground">3</span>Who gets it
            </h2>
            {usersQuery.isError ? (
              <p role="alert" className="text-sm text-destructive-text">
                Couldn’t load your users.{" "}
                <button type="button" className="underline underline-offset-2" onClick={() => void usersQuery.refetch()}>
                  Try again
                </button>
              </p>
            ) : (
              <WhoGetsIt
                users={users}
                audience={audience}
                chosen={chosen}
                reach={reach}
                onChange={(patch) => {
                  setAudience(patch.audience);
                  setChosen(patch.audience_user_ids);
                }}
              />
            )}
          </section>

          {needsRequestSource && (
            <p className="rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-muted-foreground">
              {NO_REQUEST_SOURCE}{" "}
              <Link to={CONNECTIONS_SETTINGS} className="underline underline-offset-2 hover:text-foreground">
                Settings &rsaquo; Connections
              </Link>
            </p>
          )}
          {save.isError && (
            <p role="alert" className="text-sm text-destructive-text">
              {apiErrorMessage(save.error, "Couldn’t add this row. Try again.")}
            </p>
          )}

          <div className="flex flex-wrap items-center justify-between gap-4 border-t pt-5">
            <p className="max-w-md text-sm text-muted-foreground">
              {isAi
                ? "Next you write the row’s list, then add it."
                : `Builds at the next run. You can fine-tune size, sources and placement after adding it.`}{" "}
              <Link to={`/rows/new/full?template=${template.id}`} className="underline underline-offset-2 hover:text-foreground">
                Set every option yourself
              </Link>
            </p>
            <div className="flex items-center gap-2">
              <Button variant="ghost" onClick={() => navigate("/rows")}>
                Cancel
              </Button>
              <Button onClick={submit} loading={save.isPending} disabled={problem !== null}>
                {isAi ? "Continue" : "Add row"}
              </Button>
            </div>
          </div>
        </div>

        <aside className="lg:sticky lg:top-8 lg:self-start" aria-label="Preview">
          <h2 className="mb-3 text-lg font-semibold">What this row will look like</h2>
          <div className="rounded-xl border bg-card shadow-elevated">
            <div className="p-4">
              <RowPlexCard input={input} collectionId={null} hasImage={false} compact />
            </div>
            <dl className="divide-y border-t text-sm">
              <div className="flex justify-between gap-4 px-4 py-2.5">
                <dt className="text-muted-foreground">Gets it</dt>
                <dd className="text-right">
                  {reach.length === 0 ? "No one yet" : reach.map(personName).join(", ")}
                </dd>
              </div>
              <div className="flex justify-between gap-4 px-4 py-2.5">
                <dt className="text-muted-foreground">Size</dt>
                <dd className="text-right">
                  up to {input.size} titles
                  {input.media === "movie" ? " · movies" : input.media === "show" ? " · TV shows" : ""}
                </dd>
              </div>
              <div className="flex justify-between gap-4 px-4 py-2.5">
                <dt className="text-muted-foreground">Visible to</dt>
                <dd className="text-right">
                  {input.build === "shared" ? "everyone who gets it" : "only the person it is built for"}
                </dd>
              </div>
              <div className="flex justify-between gap-4 px-4 py-2.5">
                <dt className="text-muted-foreground">Runs</dt>
                <dd className="text-right">{schedule}</dd>
              </div>
            </dl>
          </div>
        </aside>
      </div>
    </div>
  );
}
