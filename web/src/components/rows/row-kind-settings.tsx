import type { ReactNode } from "react";
import { useId } from "react";

import { BasedOnField } from "@/components/rows/row-based-on-field";
import { ColdStartFields } from "@/components/rows/row-cold-start-field";
import { RowFillPicker } from "@/components/rows/row-kind-picker";
import { RowMaxSeedsSetting } from "@/components/rows/row-max-seeds-setting";
import { MinWatchersField } from "@/components/rows/row-min-watchers-field";
import { RowSeasonsField } from "@/components/rows/row-seasons-field";
import { TakeTurnsSetting } from "@/components/rows/row-take-turns-setting";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  FILL_META,
  followsAWatch,
  isNarrowGlobal,
  namesASeed,
  NIGHTLY_LINE,
  takeTurnsEnabled,
  type RowFill,
  type RowKindChoice,
  type RowKindContext,
  type RowSettingKey,
} from "@/lib/row-kinds";
import type {
  CollectionInput,
  SeasonStatus,
  Settings,
  User,
} from "@/lib/types";

/** What every kind block reads. `shown` and `hidden` are `visibleSettings` / `hiddenButRead`. */
export type KindBlockProps = {
  input: CollectionInput;
  set: (patch: Partial<CollectionInput>) => void;
  ctx: RowKindContext;
  shown: ReadonlySet<RowSettingKey>;
  hidden: readonly RowSettingKey[];
  settings: Settings | undefined;
  users: User[];
};

function KindBlock({ title, children }: { title: string; children: ReactNode }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="space-y-4 border-t pt-4">
      <h3 id={headingId} className="text-sm font-semibold">
        {title}
      </h3>
      {children}
    </section>
  );
}

/** "Take turns", for every fill that has it. Watch it again's sits under its fill-up settings. */
export function TakeTurns({ fill, ...props }: Omit<KindBlockProps, "users"> & { fill: RowFill }) {
  return (
    <TakeTurnsSetting
      input={props.input}
      set={props.set}
      shown={props.shown}
      hidden={props.hidden}
      enabled={takeTurnsEnabled(props.input, props.ctx)}
      hiddenWhy={
        fill === "again"
          ? "Its new picks only take turns while they match 1 or 2 watches"
          : `${FILL_META[fill].title} rows don't take turns`
      }
    />
  );
}

function ColdStart(props: KindBlockProps) {
  return (
    <ColdStartFields
      input={props.input}
      set={props.set}
      shown={props.shown}
      settings={props.settings}
      namesASeed={namesASeed(props.input, props.ctx)}
    />
  );
}

function PickedBlock(props: KindBlockProps) {
  return (
    <KindBlock title="How picks are chosen">
      {props.shown.has("max_seeds") && (
        // 3 and up: 1 or 2 would make it a Because you watched row, which is the kind picker's call.
        // A global of 1 or 2 would do the same, so following it is closed off too.
        <RowMaxSeedsSetting
          input={props.input}
          set={props.set}
          settings={props.settings}
          min={3}
          inheritBlockedReason={
            isNarrowGlobal(props.ctx)
              ? `Following the global default (${props.ctx.globalMaxSeeds}) would make this a Because you watched row.`
              : null
          }
        />
      )}
      <TakeTurns {...props} fill="picked" />
      <ColdStart {...props} />
    </KindBlock>
  );
}

function BecauseYouWatchedBlock(props: KindBlockProps) {
  return (
    <KindBlock title="Which watch it’s based on">
      {props.shown.has("based_on") && (
        <BasedOnField
          input={props.input}
          set={props.set}
          ctx={props.ctx}
          settings={props.settings}
        />
      )}
      <TakeTurns {...props} fill="byw" />
      <ColdStart {...props} />
      {followsAWatch(props.input, props.ctx) && (
        <p className="text-sm text-muted-foreground">{NIGHTLY_LINE}</p>
      )}
    </KindBlock>
  );
}

function WatchItAgainBlock(props: KindBlockProps) {
  const { input, set } = props;
  return (
    <KindBlock title="Which titles come back">
      {/* Last night's film is not an old favourite, so the default keeps a month of recent watches
          out of the shelf. */}
      {props.shown.has("rewatch_cooldown_days") && (
        <div
          data-setting="rewatch_cooldown_days"
          className="space-y-2"
        >
          <div className="flex flex-wrap items-center gap-2">
            <Label htmlFor="row-rewatch-cooldown">
              Skip titles finished in the last
            </Label>
            <Input
              id="row-rewatch-cooldown"
              type="number"
              min={0}
              max={365}
              value={input.rewatch_cooldown_days}
              onChange={(event) =>
                set({
                  rewatch_cooldown_days: Math.min(
                    365,
                    Math.max(0, Math.round(Number(event.target.value) || 0)),
                  ),
                })
              }
              className="w-20"
            />
            <span className="text-sm font-medium">days</span>
          </div>
          <p className="text-sm text-muted-foreground">
            Keeps something they watched last night out of the row. 0 lets
            anything they&rsquo;ve finished back in.
          </p>
        </div>
      )}
      <ColdStart {...props} />
      <p className="text-sm text-muted-foreground">
        If they run out of finished titles, the rest of the row is filled with
        new picks. How those are found is under What goes in it.
      </p>
    </KindBlock>
  );
}

function PopularBlock(props: KindBlockProps) {
  return (
    <KindBlock title="What counts as popular">
      {props.shown.has("min_watchers") && (
        <MinWatchersField
          input={props.input}
          set={props.set}
          users={props.users}
        />
      )}
      <ColdStart {...props} />
    </KindBlock>
  );
}

function FillBlock({ fill, ...props }: KindBlockProps & { fill: RowFill }) {
  switch (fill) {
    case "picked":
      return <PickedBlock {...props} />;
    case "byw":
      return <BecauseYouWatchedBlock {...props} />;
    case "again":
      return <WatchItAgainBlock {...props} />;
    case "popular":
      return <PopularBlock {...props} />;
  }
}

/**
 * The settings that belong to the row's kind, under the kind picker (design §5). A Seasonal row gets
 * "Which seasons" and "How it's filled" first, then exactly what that fill shows on its own — which
 * is what keeps every seasonal combination reachable.
 */
export function RowKindSettings({
  choice,
  onChooseFill,
  seasonStatus,
  ...props
}: KindBlockProps & {
  choice: RowKindChoice;
  onChooseFill: (fill: RowFill) => void;
  /** Where the SAVED row is in its calendar, while the form still matches it; else null. */
  seasonStatus: SeasonStatus | null;
}) {
  if (choice.kind !== "seasonal") return <FillBlock {...props} fill={choice.fill} />;
  const { input, set } = props;
  return (
    <>
      {props.shown.has("seasons") && (
        <div data-setting="seasons" className="border-t pt-4">
          <RowSeasonsField
            value={{
              seasons: input.seasons,
              season_lead_days: input.season_lead_days,
              season_after_days: input.season_after_days,
            }}
            onChange={set}
            schedule={input.schedule}
            name={input.name_template || input.name}
            status={seasonStatus}
          />
        </div>
      )}
      <div className="border-t pt-4">
        <RowFillPicker value={choice.fill} onChange={onChooseFill} />
      </div>
      <FillBlock {...props} fill={choice.fill} />
    </>
  );
}
