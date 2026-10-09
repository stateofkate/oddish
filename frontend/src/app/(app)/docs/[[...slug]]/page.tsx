import { notFound } from "next/navigation";
import { DocsView } from "@/components/docs-view";
import { docContent, docPages } from "@/lib/docs";

export default async function DocsPage({
  params,
}: {
  params: Promise<{ slug?: string[] }>;
}) {
  const pages = docPages();
  const slug = (await params).slug?.join("/") ?? pages[0]?.slug;
  if (!pages.some((p) => p.slug === slug)) notFound();
  return <DocsView pages={pages} slug={slug} content={docContent(slug)} />;
}
