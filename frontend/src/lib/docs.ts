import path from "node:path";

/**
 * The customer guides from the repo's docs/ folders, inlined at build by
 * next.config.ts. docs.abundant.dev imports the same files; staff folders are
 * never read.
 */
const FILES: Record<string, string> = JSON.parse(
  process.env.ODDISH_DOCS_BUNDLE || "{}"
);

// URL prefix → source folder. Customer guides sit at /docs/<slug>.
const COLLECTIONS = [
  { prefix: "", folder: "customer" },
  { prefix: "shared/", folder: "shared" },
];

export type DocPage = { slug: string; title: string };

export function docPages(): DocPage[] {
  return COLLECTIONS.flatMap(({ prefix, folder }) => {
    const nav: DocPage[] = JSON.parse(
      FILES[`${folder}/navigation.json`] ?? "[]"
    );
    return nav.map((page) => ({ slug: prefix + page.slug, title: page.title }));
  });
}

/** A page's Markdown, with links to sibling .md files pointed at /docs. */
export function docContent(slug: string): string {
  const { prefix, folder } =
    COLLECTIONS.find((c) => c.prefix && slug.startsWith(c.prefix)) ??
    COLLECTIONS[0];
  const local = slug.slice(prefix.length);
  return (FILES[`${folder}/${local}.md`] ?? "").replace(
    /\]\(([^)#:]+)\.md(#[^)]*)?\)/g,
    (_, target: string, hash = "") =>
      `](/docs/${prefix}${path.posix.join(path.posix.dirname(local), target)}${hash})`
  );
}
