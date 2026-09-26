"use client";

import { useSearchParams } from "next/navigation";
import { parseView, viewHref, type QAEvalsView } from "@/lib/qa-evals";
import { useOrgHref } from "@/lib/use-org-href";

/** The page's view from the URL, and `show`, which merges keys into it and
 * replaces the URL. `show` merges into the URL as it is when called and
 * writes it with the history API, which Next syncs into `useSearchParams`
 * without a server round trip, so quick successive changes all land. */
export function useQAEvalsView(): [
  QAEvalsView,
  (patch: Partial<QAEvalsView>) => void,
] {
  const orgHref = useOrgHref();
  const view = parseView(useSearchParams());
  const show = (patch: Partial<QAEvalsView>) => {
    const current = parseView(new URLSearchParams(window.location.search));
    window.history.replaceState(
      null,
      "",
      orgHref(viewHref({ ...current, ...patch }))
    );
  };
  return [view, show];
}
