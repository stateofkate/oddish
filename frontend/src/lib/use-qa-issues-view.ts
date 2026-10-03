import { usePathname, useSearchParams } from "next/navigation";
import { parseOrgSlug, stripOrgSlug, withOrgSlug } from "@/lib/org-path";
import { parseQaIssuesView, type QaIssuesView } from "@/lib/qa-issues";

/** The view in the URL now, when the page is at ``baseHref``. */
export function useQaIssuesView(baseHref: string): QaIssuesView | null {
  const pathname = usePathname();
  const params = useSearchParams();
  // Signed-in pages live under /orgs/{slug}; baseHref is the unprefixed route.
  if (stripOrgSlug(pathname) !== baseHref.split("?")[0]) return null;
  return parseQaIssuesView(Object.fromEntries(params));
}

const QA_VIEW_KEYS = new Set(["category", "blocking", "q", "page", "version"]);

/** Puts an app href under the /orgs/{slug} prefix the page is on, if any.
 *  An href to the current page keeps its non-QA parameters, so the QA view
 *  embedded in the experiment page leaves its filters and drawer alone.
 *  Those come from window.location at call time: the trials table and drawer
 *  write them with replaceState(history.state), which useSearchParams never
 *  sees, so call this from the click that navigates, not only while rendering. */
export function useOrgPath() {
  const pathname = usePathname();
  const rendered = useSearchParams();
  const slug = parseOrgSlug(pathname);
  return (href: string) => {
    const [path, query = ""] = href.split("?");
    if (path !== stripOrgSlug(pathname)) return withOrgSlug(href, slug);
    const params =
      typeof window === "undefined"
        ? rendered
        : new URLSearchParams(window.location.search);
    const next = new URLSearchParams(query);
    for (const [key, value] of params) {
      if (!QA_VIEW_KEYS.has(key) && !next.has(key)) next.append(key, value);
    }
    const search = next.toString();
    return withOrgSlug(search ? `${path}?${search}` : path, slug);
  };
}
