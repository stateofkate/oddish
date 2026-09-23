"use client";

import { Fragment, useMemo } from "react";
import { CodeBlock } from "@/components/code-block";
import { MarkdownRenderer } from "@/components/renderers/markdown-renderer";
import { indentedJson, taggedContext } from "@/lib/trajectory-view";

function Markdown({ text }: { text: string }) {
  return <MarkdownRenderer content={text} className="p-0" />;
}

/** Agent or user text: tagged context as a labeled section, a complete JSON
 *  document as indented JSON, anything else as Markdown. */
export function MessageText({ text }: { text: string }) {
  const context = useMemo(() => taggedContext(text), [text]);
  const json = useMemo(
    () => (context ? null : indentedJson(text)),
    [context, text]
  );
  if (!text.trim()) return null;
  if (json)
    return (
      <CodeBlock
        code={json}
        copyText={text}
        language="json"
        collapseAfter={20}
      />
    );
  if (!context) return <Markdown text={text} />;
  return (
    <section className="bg-muted/30 min-w-0 rounded-md border px-3 py-2">
      <h5 className="text-muted-foreground mb-1.5 text-[11px] font-medium tracking-wide uppercase">
        {context.label}
      </h5>
      {context.fields ? (
        <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1 text-xs">
          {context.fields.map(([key, value], index) => (
            <Fragment key={index}>
              <dt className="text-muted-foreground">{key}</dt>
              <dd className="font-mono wrap-break-word">{value}</dd>
            </Fragment>
          ))}
        </dl>
      ) : (
        <Markdown text={context.body} />
      )}
    </section>
  );
}
