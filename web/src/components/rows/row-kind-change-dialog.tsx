import { useId, useState } from "react";
import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { SEASON, SEASON_EMOJI, TOP_SEED } from "@/lib/placeholders";
import {
  DEFAULT_ROW_NAME_SETTINGS,
  renameProblem,
  type KindChange,
  type KindChangeRename,
} from "@/lib/row-kinds";

/**
 * "Change this row to X?" — shown before a SAVED row switches kind (design §4.1).
 *
 * Everything it says comes from `describeKindChange`, the same module that decides what the switch
 * writes, so the dialog cannot promise one thing while the patch does another. Nothing changes
 * until "Change it"; closing or cancelling leaves the row exactly as it was.
 *
 * Mounted only while open, so each switch starts from the proposal rather than a stale draft.
 */
export function RowKindChangeDialog({
  describe,
  onCancel,
  onConfirm,
}: {
  /** `describeKindChange` for this switch, under the name chosen here (null: no new name). Called
   *  again as that choice changes, since the row's name decides some of what the switch does. */
  describe: (renameTo: string | null) => KindChange;
  onCancel: () => void;
  /** The name to hand to the rename screen after saving, or null to keep the current one. */
  onConfirm: (renameTo: string | null) => void;
}) {
  // Radix returns focus only to a Dialog.Trigger, and this dialog opens from a radio's change, so
  // without this closing it drops focus on the page body. Read while rendering, before the dialog
  // moves focus into itself.
  const [returnFocus] = useState(() =>
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );
  const [rename] = useState(() => describe(null).rename);
  const [renaming, setRenaming] = useState(rename?.required ?? false);
  const [name, setName] = useState(rename?.proposed ?? "");
  const trimmed = name.trim();
  const change = describe(renaming ? trimmed : null);
  // A required rename exists because the old name can't stay; a new one that keeps what made it
  // impossible, or names a watch the new kind doesn't follow, would undo the switch or be refused by
  // the server.
  const problem = renaming ? renameProblem(name, rename?.mustNotUse ?? []) : null;

  return (
    <Dialog open onOpenChange={(open) => !open && onCancel()}>
      <DialogContent
        className="max-h-[90vh] overflow-y-auto"
        onCloseAutoFocus={(event) => {
          if (!returnFocus?.isConnected) return;
          event.preventDefault();
          returnFocus.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle>{change.title}</DialogTitle>
          <DialogDescription>This will:</DialogDescription>
        </DialogHeader>
        <ul
          aria-label="What this changes"
          className="list-disc space-y-1 pl-5 text-sm"
        >
          {change.lines.length > 0 ? (
            change.lines.map((line) => <li key={line}>{line}</li>)
          ) : (
            <li>Change nothing else about the row.</li>
          )}
        </ul>
        <p className="rounded-md bg-muted/60 p-3 text-sm">{change.plexNote}</p>

        {rename && (
          <RenameField
            afterSave={change.renameNote ?? ""}
            rename={rename}
            renaming={renaming}
            onRenaming={setRenaming}
            name={name}
            onName={setName}
            problem={problem}
          />
        )}
        {change.renameInSettings && (
          <SettingsRenameNote rename={change.renameInSettings} />
        )}

        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            disabled={problem !== null}
            onClick={() => onConfirm(renaming ? trimmed : null)}
          >
            Change it
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RenameField({
  afterSave,
  rename,
  renaming,
  onRenaming,
  name,
  onName,
  problem,
}: {
  /** Where the new name reaches Plex once saved. */
  afterSave: string;
  rename: KindChangeRename;
  renaming: boolean;
  onRenaming: (renaming: boolean) => void;
  name: string;
  onName: (name: string) => void;
  problem: string | null;
}) {
  const id = useId();
  return (
    <div className="space-y-2 border-t pt-4">
      {rename.required ? (
        <RequiredRenameReason because={rename.because} />
      ) : (
        <div className="flex items-start justify-between gap-4">
          <Label htmlFor={`${id}-switch`} className="leading-snug">
            Name it after their latest watch too
          </Label>
          <Switch
            id={`${id}-switch`}
            checked={renaming}
            onCheckedChange={onRenaming}
          />
        </div>
      )}
      {renaming && (
        <>
          <Label htmlFor={id}>New name</Label>
          <Input id={id} value={name} onChange={(e) => onName(e.target.value)} />
          {problem ? (
            <p role="alert" className="text-sm text-destructive-text">
              {problem}
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">{afterSave}</p>
          )}
        </>
      )}
    </div>
  );
}

/** Why the old name can't stay, from the placeholders that make it impossible. */
function RequiredRenameReason({ because }: { because: string[] }) {
  const seed = because.includes(TOP_SEED);
  const season = because.some((token) => token !== TOP_SEED);
  return (
    <p className="text-sm">
      {seed && season ? (
        <>
          Its name follows one watch with <code>{TOP_SEED}</code> and uses the
          season, and this kind follows neither, so it needs a new one.
        </>
      ) : season ? (
        <>
          Its name uses the season (<code>{SEASON}</code> or{" "}
          <code>{SEASON_EMOJI}</code>), which only a seasonal row can fill, so it
          needs a new one.
        </>
      ) : (
        <>
          Its name follows one watch with <code>{TOP_SEED}</code>, which this kind
          doesn&rsquo;t, so it needs a new one.
        </>
      )}
    </p>
  );
}

/** The default row's name is the global `row.name_template`, so the rename happens in Settings. */
function SettingsRenameNote({ rename }: { rename: KindChangeRename }) {
  return (
    <p className="border-t pt-4 text-sm">
      {rename.required ? (
        <>
          This is the default row, and its name still follows one watch with{" "}
          <code>{TOP_SEED}</code>, so it will keep reading as a Because you
          watched row until you change it, for example to &ldquo;
          {rename.proposed}&rdquo;.
        </>
      ) : (
        <>
          This is the default row. To name it after their latest watch, change
          its name to something like &ldquo;{rename.proposed}&rdquo;.
        </>
      )}{" "}
      Its name lives in Settings:{" "}
      <Link
        to={DEFAULT_ROW_NAME_SETTINGS}
        className="underline underline-offset-2 hover:text-foreground"
      >
        change it in Settings › Row defaults
      </Link>
      .
    </p>
  );
}
