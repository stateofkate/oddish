"use client";

import Link from "next/link";
import { MarkdownRenderer } from "@/components/renderers/markdown-renderer";
import type { DocPage } from "@/lib/docs";
import { useIsCustomerOrg, useIsViewerOrg } from "@/lib/use-org-access";
import { useOrgHref } from "@/lib/use-org-href";

// Guides for Abundant-only pages (Deliveries, Admin), which customer orgs never reach.
const HIDDEN_FROM_CUSTOMERS = new Set([
  "guides/deliveries",
  "guides/qa-goldens",
]);
// A view-only org runs nothing, so it gets setup, terms and its shared data.
const VIEWER_PAGES = new Set([
  "getting-started",
  "concepts",
  "shared/use-shared-data",
]);

export function DocsView({
  pages,
  slug,
  content,
}: {
  pages: DocPage[];
  slug: string;
  content: string;
}) {
  const viewer = useIsViewerOrg();
  const customer = useIsCustomerOrg();
  const orgHref = useOrgHref();
  if (viewer === undefined || customer === undefined) return null;
  // ponytail: hides pages, not secrets; every page here is customer-safe.
  const visible = pages.filter((p) =>
    viewer
      ? VIEWER_PAGES.has(p.slug)
      : !customer || !HIDDEN_FROM_CUSTOMERS.has(p.slug)
  );
  const slugs = new Set(visible.map((p) => p.slug));
  const shown = slugs.has(slug);
  // Links to pages this org can't open keep their text and lose the link.
  const body = content.replace(
    /\[([^\]]*)\]\(\/docs\/([^)#]+)(#[^)]*)?\)/g,
    (link, text: string, target: string) => (slugs.has(target) ? link : text)
  );

  return (
    <div className="flex flex-col gap-6 sm:flex-row">
      <nav className="flex shrink-0 flex-col gap-0.5 text-sm sm:w-56">
        {visible.map((page) => (
          <Link
            key={page.slug}
            href={orgHref(`/docs/${page.slug}`)}
            data-active={page.slug === slug}
            className="text-muted-foreground hover:bg-muted hover:text-foreground data-[active=true]:bg-muted data-[active=true]:text-foreground rounded-md px-2 py-1.5"
          >
            {page.title}
          </Link>
        ))}
      </nav>
      <article className="max-w-3xl min-w-0 flex-1">
        {shown ? (
          <MarkdownRenderer content={body} fileName={slug} />
        ) : (
          <p className="text-muted-foreground text-sm">Page not found.</p>
        )}
      </article>
    </div>
  );
}
