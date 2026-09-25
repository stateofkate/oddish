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

/** Puts an app href under the /orgs/{slug} prefix the page is on, if any. */
export function useOrgPath() {
  const slug = parseOrgSlug(usePathname());
  return (href: string) => withOrgSlug(href, slug);
}
