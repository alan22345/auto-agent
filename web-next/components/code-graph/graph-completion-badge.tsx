"use client";

import { Loader2 } from "lucide-react";
import type { RepoGraphProgressData } from "@/types/api";

export function GraphCompletionBadge({
  progress,
}: {
  progress: RepoGraphProgressData;
}) {
  if (progress.is_complete) {
    return (
      <span className="inline-flex items-center rounded-full bg-green-50 px-3 py-1 text-xs font-medium text-green-700 ring-1 ring-inset ring-green-600/20">
        Complete · {progress.total} files
      </span>
    );
  }
  if (progress.status === "running") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-3 py-1 text-xs font-medium text-amber-800 ring-1 ring-inset ring-amber-600/30">
        <Loader2 className="h-3 w-3 animate-spin" />
        Analyzing · {progress.processed} / {progress.total} files
        {progress.last_file ? ` (file: ${progress.last_file})` : ""}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-zinc-100 px-3 py-1 text-xs font-medium text-zinc-700 ring-1 ring-inset ring-zinc-500/30">
      Partial · {progress.processed} / {progress.total} files. Click Refresh to resume.
    </span>
  );
}
