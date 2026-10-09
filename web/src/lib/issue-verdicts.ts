/** A check's finding: whether it is a fault, and the sentence that says so. */
export type Verdict = { bad: boolean; text: string };

type CheckData = Record<string, unknown>;

/** Typed readers over a check's untyped result: a list of strings, and a list's length. */
function readers(data: CheckData) {
  const list = (key: string): string[] => (Array.isArray(data[key]) ? (data[key] as string[]) : []);
  const count = (key: string): number => (Array.isArray(data[key]) ? (data[key] as unknown[]).length : 0);
  return { list, count };
}

function verdictTitle(data: CheckData): Verdict | null {
  const { list } = readers(data);
  // `flagged_detail` names the TITLE as well as the person. A search is a substring match, so it
  // can span a whole franchise — "was given this title" would be ambiguous about which.
  const detail = list("flagged_detail");
  const capped = data.capped
    ? " Only the first matches were read, so narrow the search to be sure."
    : "";
  return detail.length
    ? {
        bad: true,
        text: `Given even though the row is set to show nothing they've watched: ${detail.join(", ")}.${capped}`,
      }
    : {
        bad: false,
        text: `Nothing unexpected — everyone who was given a matching title is allowed to see it.${capped}`,
      };
}

function verdictPerson(data: CheckData): Verdict | null {
  const { list } = readers(data);
  const never = list("never_read");
  return never.length
    ? {
        bad: true,
        text: `We have never been able to read ${never.length === 1 ? "one library" : `${never.length} libraries`} for this person, so anything they watched there is invisible to Shortlist.`,
      }
    : { bad: false, text: "We can read every library for this person." };
}

function verdictConnection(data: CheckData): Verdict | null {
  const { list } = readers(data);
  const problems = list("problems");
  return problems.length
    ? { bad: true, text: `We cannot fully read: ${problems.join(", ")}.` }
    : { bad: false, text: "Everyone's history can be read." };
}

function verdictDrift(data: CheckData): Verdict | null {
  const { count } = readers(data);
  if (data.error) return { bad: true, text: String(data.error) };
  const missing = count("missing_on_plex");
  const orphans = count("orphans_on_plex");
  return missing || orphans
    ? {
        bad: true,
        text: `${missing} row(s) missing from Plex, ${orphans} unexpected collection(s) on it.`,
      }
    : {
        bad: false,
        text: "Everything we recorded exists on the server, and nothing extra.",
      };
}

function verdictSettingsHistory(data: CheckData): Verdict | null {
  return data.change_after_last_build
    ? {
        bad: true,
        text: "A setting changed after the last build, so it has not taken effect yet.",
      }
    : {
        bad: false,
        text: "Every setting change has been applied by a build since.",
      };
}

function verdictJobs(data: CheckData): Verdict | null {
  return Number(data.failed ?? 0) > 0
    ? {
        bad: true,
        text: `${String(data.failed)} background job(s) failed.`,
      }
    : { bad: false, text: "No failed background work." };
}

function verdictDatabase(data: CheckData): Verdict | null {
  const { list } = readers(data);
  const missing = list("missing_tables");
  return missing.length
    ? { bad: true, text: `The database is missing: ${missing.join(", ")}.` }
    : {
        bad: false,
        text: `Schema is at ${String(data.head)} and complete.`,
      };
}

function verdictMissing(data: CheckData): Verdict | null {
  return data.verdict ? { bad: true, text: String(data.verdict) } : null;
}

function verdictSharing(data: CheckData): Verdict | null {
  const { list } = readers(data);
  if (data.error) return { bad: true, text: String(data.error) };
  // The highest-stakes verdict on the page: someone can see a row that is not theirs.
  const leaking = list("missing_excludes_for");
  return leaking.length
    ? {
        bad: true,
        text: `${leaking.join(", ")} can see at least one row that isn't theirs. Run Shortlist again — every run re-merges the exclusions — then check here again.`,
      }
    : {
        bad: false,
        text: "Every row is hidden from everyone it doesn't belong to.",
      };
}

function verdictSurfaces(data: CheckData): Verdict | null {
  const { count } = readers(data);
  if (data.error) return { bad: true, text: String(data.error) };
  const home = count("on_owner_home");
  const shelf = count("on_owner_shelf");
  const unlabelled = count("unlabelled");
  // An UNLABELLED row of ours is the worst finding this check has, and it was being read past
  // entirely: no `label!=` exclude can hide a row that carries no label, so it is visible to
  // everyone, and `sweep_broken_rows` deletes it as an orphan. The server already calls it a
  // BUG in the copy text below — the banner above it must not say the opposite.
  const ours = count("rows");
  if (unlabelled) {
    // Rule 4's own lesson, applied to the reading rather than to a delete: if NOT ONE of our
    // rows reads as labelled, that is a label read that failed, not a server full of orphans.
    // Reporting it as N separate leaks would send someone hunting rows that are probably fine.
    return unlabelled === ours && ours > 1
      ? {
          bad: true,
          text: `Not one of your ${ours} rows reads as having a label, which is far more likely to be a failed read from Plex than ${ours} unlabelled rows. Check the connection and run this again before acting on it.`,
        }
      : {
          bad: true,
          text: `${unlabelled} of our collections carry NO label. Nothing can hide an unlabelled row — every person on your server can see ${unlabelled === 1 ? "it" : "them"}. The copy text below names ${unlabelled === 1 ? "it" : "them"}; a run relabels and re-hides them.`,
        };
  }
  // An empty answer is not proof (plex-safety rule 4). `owned_row_surfaces` attaches an `error`
  // to a row whose hub read raised and emits NO surface flags for it, so a server where every
  // read failed produces exactly the same empty `on_owner_home` as a server where nothing is
  // wrong. Saying "everything is where it should be" off the back of reads that never happened
  // is the one thing this check must never do.
  const rows = (Array.isArray(data.rows) ? data.rows : []) as {
    error?: string;
  }[];
  const unread = rows.filter((row) => row.error).length;
  if (unread) {
    return {
      bad: true,
      text: `${unread} of ${rows.length} rows could not be read from Plex, so this cannot say where they are showing. Fix the connection and check again — an empty answer here is not the same as a clean one.`,
    };
  }
  if (home) {
    return {
      bad: true,
      text: `${home} row${home === 1 ? "" : "s"} belonging to someone else ${home === 1 ? "is" : "are"} on your own Home screen. No setting makes that correct — the copy text below names them.`,
    };
  }
  // A CONSEQUENCE, not a fault: the Recommended shelf is one flag per collection and the owner
  // has no filter, so a row shown on friends' library shelves lands on the owner's too. The fix
  // is a settings change, which is why this reads as an explanation.
  if (shelf) {
    return {
      bad: false,
      text: `Nothing is on your Home screen that shouldn't be. ${shelf} row${shelf === 1 ? "" : "s"} of other people's do appear on your Recommended shelf — that is a Plex limitation of showing rows on friends' library shelves, not a fault, and turning that placement off is what removes them.`,
    };
  }
  return {
    bad: false,
    text: "Every row is showing exactly where it should, including on your own Home screen.",
  };
}

function verdictLibraries(data: CheckData): Verdict | null {
  const { count } = readers(data);
  if (data.error) return { bad: true, text: String(data.error) };
  const found = count("libraries");
  return found
    ? {
        bad: false,
        text: `Shortlist can see ${found} ${found === 1 ? "library" : "libraries"}.`,
      }
    : {
        bad: true,
        text: "Shortlist can see no libraries at all, so it has nothing to recommend from.",
      };
}

function verdictRows(data: CheckData): Verdict | null {
  const rows = (Array.isArray(data.rows) ? data.rows : []) as {
    enabled?: boolean;
  }[];
  const on = rows.filter((row) => row.enabled).length;
  return rows.length === 0
    ? {
        bad: true,
        text: "There are no rows at all, so nothing can be built.",
      }
    : on === 0
      ? {
          bad: true,
          text: `All ${rows.length} rows are switched off, so no run will build anything.`,
        }
      : {
          bad: false,
          text: `${on} of ${rows.length} rows are on. The value each setting actually uses is below — "global" means it is inherited, "row" means this row overrides it.`,
        };
}

function verdictRowSchedule(data: CheckData): Verdict | null {
  // The single most common "it's broken" that isn't: the setting IS applied, the row simply has
  // not rebuilt since. Which is why this states the wait rather than just tabulating dates.
  const rows = (Array.isArray(data.rows) ? data.rows : []) as {
    slug?: string;
    enabled?: boolean;
    due?: boolean;
    never_built?: boolean;
    idle_hold_days?: number;
    rebuild_every_days?: number;
  }[];
  const live = rows.filter((row) => row.enabled);
  const never = live.filter((row) => row.never_built).map((r) => r.slug);
  const waiting = live.filter((row) => !row.never_built && !row.due);
  if (live.length === 0)
    return { bad: true, text: "No row is switched on." };
  if (never.length) {
    return {
      bad: true,
      text: `${never.join(", ")} ${never.length === 1 ? "has" : "have"} never been built, so nothing you set on ${never.length === 1 ? "it" : "them"} has reached Plex yet. Run it from Rows.`,
    };
  }
  // "Due" stops meaning "will rebuild" once a hold is set: a due row belonging to someone who
  // has watched nothing waits anyway. Promising the change has landed would send the operator
  // away from the one thing actually holding their row.
  // The server's own `live_hold` predicate. `idle_hold_days > 0` alone is not enough: a hold at
  // or below the cadence, or on a frozen row, can never fire — and the block rendered directly
  // below this line says so, so claiming a row "may still be held" contradicts it on screen.
  const held = live.some(
    (row) =>
      (row.idle_hold_days ?? 0) > (row.rebuild_every_days ?? 0) &&
      (row.rebuild_every_days ?? 0) > 0,
  )
    ? " A row whose owner has watched nothing since it was built may still be held — the hold column says which."
    : "";
  return waiting.length
    ? {
        bad: false,
        text: `${waiting.length} ${waiting.length === 1 ? "row is" : "rows are"} not due to refresh yet — a setting you changed does not reach a row until it does. The table below says when.${held}`,
      }
    : {
        bad: false,
        text: `Every row is due to refresh, so the next run will pick up anything you have changed.${held}`,
      };
}

function verdictFunnel(data: CheckData): Verdict | null {
  const stages = (Array.isArray(data.stages) ? data.stages : []) as {
    pool?: string;
    pooled?: number;
  }[];
  const delivered = Number(data.delivered ?? 0);
  if (!data.run_id) {
    return {
      bad: true,
      text: "This person has never had a row built, so there is no funnel to walk. Run a row for them first.",
    };
  }
  if (stages.length === 0) {
    return {
      bad: true,
      text: `Their last run recorded no candidates at all — nothing was even considered, so the row could only ever be empty. ${delivered} titles were delivered.`,
    };
  }
  const pooled = stages.reduce((n, s) => n + Number(s.pooled ?? 0), 0);
  return {
    bad: delivered === 0,
    text:
      delivered === 0
        ? `${pooled} candidates were gathered and NONE survived to the row. The stage that ate them names itself in the counts below.`
        : `${pooled} candidates gathered, ${delivered} delivered. Every title that didn't make it is accounted for below.`,
  };
}

function verdictAi(data: CheckData): Verdict | null {
  if (data.error) return { bad: true, text: String(data.error) };
  const provider = String(data.provider ?? "none");
  if (provider === "none") {
    return {
      bad: false,
      text: "No AI curator is configured — picks are chosen by ranking alone, which is a valid setup rather than a fault.",
    };
  }
  if (!data.run_id) {
    return {
      bad: true,
      text: `${provider} is configured, but nothing has been built for this person yet, so it has never been asked anything.`,
    };
  }
  const tokens = Number(data.llm_tokens ?? 0);
  return tokens === 0
    ? {
        bad: true,
        text: `${provider} is configured but spent no tokens on this person's last run — it was never actually called.`,
      }
    : {
        bad: false,
        text: `${provider} ran for this person and spent ${tokens} tokens. The per-step breakdown is below.`,
      };
}

function verdictPick(data: CheckData): Verdict | null {
  const { count } = readers(data);
  const picks = count("picks");
  return picks
    ? {
        bad: false,
        text: `Found in ${picks} ${picks === 1 ? "row" : "rows"} for this person — the seed and the source behind each are below.`,
      }
    : {
        bad: true,
        text: "This person was never given that title, so there is nothing to explain. Try “Why is this NOT in their row?” instead.",
      };
}

function verdictErrors(data: CheckData): Verdict | null {
  const lines = (Array.isArray(data.lines) ? data.lines : []) as {
    level?: string;
  }[];
  // WARNING is not a fault — a healthy server logs them. Only an ERROR earns the red panel, or
  // every install would open on a scary red box saying nothing is wrong.
  const errors = lines.filter((l) =>
    String(l.level ?? "").startsWith("ERROR"),
  ).length;
  const total = Number(data.total_matched ?? lines.length);
  if (total === 0) {
    return {
      bad: false,
      text: "Nothing has been logged at WARNING or above — no errors, no warnings.",
    };
  }
  return errors
    ? {
        bad: true,
        text: `${errors} error${errors === 1 ? "" : "s"} in the recent log, out of ${total} lines at WARNING or above.`,
      }
    : {
        bad: false,
        text: `${total} warning${total === 1 ? "" : "s"} and no errors. Warnings are normal; the lines are below if you want to read them.`,
      };
}

function verdictRuns(data: CheckData): Verdict | null {
  const runs = (Array.isArray(data.runs) ? data.runs : []) as {
    id?: number;
    status?: string;
    failed?: unknown[];
  }[];
  if (runs.length === 0) {
    return {
      bad: true,
      text: "Nothing has ever been built. Run all rows once from Runs, then come back.",
    };
  }
  const broken = runs.filter((r) => r.status === "error");
  const people = runs.reduce((n, r) => n + (r.failed?.length ?? 0), 0);
  if (broken.length) {
    return {
      bad: true,
      text: `${broken.length} of the last ${runs.length} runs failed outright.`,
    };
  }
  return people
    ? {
        bad: true,
        text: `Every recent run finished, but ${people} ${people === 1 ? "person was" : "people were"} skipped with an error inside them — named below.`,
      }
    : {
        bad: false,
        text: `The last ${runs.length} ${runs.length === 1 ? "run" : "runs"} finished with nobody failing.`,
      };
}

function verdictClocks(data: CheckData): Verdict | null {
  const { count } = readers(data);
  const offset = Number(data.offset_hours ?? 0);
  const tz = String(data.tz ?? "");
  const fires = count("scheduled");
  // Worth stating out loud whatever the answer: every timestamp in the database is UTC while
  // every line in the log is local, and reading one as the other inverts the order of events.
  const where = tz
    ? `${tz} (UTC${offset >= 0 ? "+" : ""}${offset})`
    : `UTC${offset >= 0 ? "+" : ""}${offset}`;
  return fires
    ? {
        bad: false,
        text: `This server runs on ${where}, and ${fires} scheduled ${fires === 1 ? "job is" : "jobs are"} due at the times below. The database stores UTC, so a log line and a database timestamp for the same event are ${offset} hours apart.`,
      }
    : {
        bad: true,
        text: `This server runs on ${where}, but NOTHING is scheduled to fire — so nothing will run on its own.`,
      };
}

function verdictConfig(data: CheckData): Verdict | null {
  const settings = (Array.isArray(data.settings) ? data.settings : []) as {
    key?: string;
    env_set?: boolean;
    has_value?: boolean;
  }[];
  // The whole point of this check: an env var that no longer decides anything, still set, still
  // believed. Naming those is the finding; the rest of the table is reference.
  const ignored = settings
    .filter((s) => s.env_set && s.has_value)
    .map((s) => s.key);
  return ignored.length
    ? {
        bad: false,
        text: `${ignored.length} setting${ignored.length === 1 ? " is" : "s are"} set BOTH by an environment variable and in the database — the database wins, and the variable now changes nothing: ${ignored.slice(0, 6).join(", ")}${ignored.length > 6 ? "…" : ""}.`,
      }
    : {
        bad: false,
        text: "Every setting comes from the database. No environment variable is competing with one.",
      };
}

function verdictTimeline(data: CheckData): Verdict | null {
  const { count } = readers(data);
  const entries = count("entries");
  return entries
    ? {
        bad: false,
        text: `${entries} things happened, newest first, in your local time.`,
      }
    : {
        bad: true,
        text: "Nothing is recorded at all — no runs, no jobs, no changes. On a working server that is itself the finding.",
      };
}

function verdictReadAs(data: CheckData): Verdict | null {
  const code = Number(data.status_code ?? 0);
  if (!code) return null;
  return code >= 400
    ? {
        bad: true,
        text: `Plex answered HTTP ${code} to this person's own token — so this is what Shortlist gets when it reads for them, and it is why their history may be missing.`,
      }
    : {
        bad: false,
        text: `Plex answered HTTP ${code} to this person's own token. Their raw reply is below — this is exactly what Shortlist sees.`,
      };
}

/**
 * The verdict sentence for a result, derived from the fields the server already flags.
 *
 * Every check reports its finding as DATA (`flagged`, `never_read`, `problems`, …) rather than
 * leaving the page to re-derive it from a table — so this stays a lookup, and the page and the copy
 * text can never disagree about whether something is wrong.
 */
const VERDICTS: Record<string, (data: CheckData) => Verdict | null> = {
  "title": verdictTitle,
  "person": verdictPerson,
  "connection": verdictConnection,
  "drift": verdictDrift,
  "settings-history": verdictSettingsHistory,
  "jobs": verdictJobs,
  "database": verdictDatabase,
  "missing": verdictMissing,
  "sharing": verdictSharing,
  "surfaces": verdictSurfaces,
  "libraries": verdictLibraries,
  "rows": verdictRows,
  "row-schedule": verdictRowSchedule,
  "funnel": verdictFunnel,
  "ai": verdictAi,
  "pick": verdictPick,
  "errors": verdictErrors,
  "runs": verdictRuns,
  "clocks": verdictClocks,
  "config": verdictConfig,
  "timeline": verdictTimeline,
  "read-as": verdictReadAs,
};

export function verdictFor(id: string, data: CheckData): Verdict | null {
  return VERDICTS[id]?.(data) ?? null;
}
