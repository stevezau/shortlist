import { useSearchParams } from "react-router";

import { ChangesOnPlex } from "@/components/activity/changes-on-plex";
import { PageHeader } from "@/components/page-header";
import { TabPanel, Tabs } from "@/components/ui/tabs";
import { JobHistoryPanel, JobsPanel } from "@/pages/jobs";
import { LogsPanel } from "@/pages/logs";

type ActivityTab = "jobs" | "history" | "log" | "changes";

const TABS: readonly { value: ActivityTab; label: string }[] = [
  { value: "jobs", label: "Jobs" },
  { value: "history", label: "Job history" },
  { value: "log", label: "Log" },
  { value: "changes", label: "Changes on Plex" },
];

/**
 * Which tab an address opens. The job history was a "Jobs | Activity" switch INSIDE the Jobs tab
 * (`?tab=jobs&view=activity`), a second row of tabs under the first; it is a tab of the page now, and
 * the old address — in bookmarks, and what /jobs?tab=activity redirects to — still opens it.
 */
function tabFor(params: URLSearchParams): ActivityTab {
  const tab = params.get("tab");
  if (tab === "jobs" && params.get("view") === "activity") return "history";
  return TABS.some((option) => option.value === tab) ? (tab as ActivityTab) : "jobs";
}

/**
 * What Shortlist is doing, what it did, and what it changed on Plex: the background jobs, their run
 * history, the live log, and the audit trail of every Plex write. The tab is in `?tab=` so a link can
 * open any one; /jobs and /logs redirect here, and anything that is not a known tab is Jobs rather
 * than an empty page.
 */
export function ActivityPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = tabFor(searchParams);

  return (
    <div>
      <PageHeader
        title="Activity"
        subtitle="What Shortlist is doing, what it did, and what it changed on Plex."
      />
      <Tabs<ActivityTab>
        id="activity"
        ariaLabel="Activity"
        value={tab}
        // A fresh address per tab: one tab's `filter` means nothing on another.
        onChange={(next) => setSearchParams({ tab: next }, { replace: true })}
        options={TABS}
        className="mb-5"
      />
      <TabPanel id="activity" value={tab}>
        {tab === "log" ? (
          <LogsPanel />
        ) : tab === "history" ? (
          <JobHistoryPanel />
        ) : tab === "changes" ? (
          <ChangesOnPlex />
        ) : (
          <JobsPanel />
        )}
      </TabPanel>
    </div>
  );
}
