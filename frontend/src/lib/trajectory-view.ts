import type {
  ContentPart,
  MessageContent,
  ObservationContent,
  TrajectoryStep,
} from "./types";

/**
 * Turns recorded ATIF steps into what the trajectory viewer shows: the
 * command a tool call ran, and the text its outputs contain. Everything here
 * is derived from the step and never replaces it; the viewer keeps the raw
 * step JSON one click away.
 */

/** A block of code or output. `copyText` holds the exact recorded text when
 *  `code` was reformatted (indented JSON). */
export interface CodeView {
  code: string;
  language: string;
  copyText?: string;
  /** Short facts about the block, e.g. "exit 1", "0.4s", "timeout 120000". */
  meta: string[];
}

export type OutputView =
  | ({ kind: "code" } & CodeView)
  | { kind: "image"; source: NonNullable<ContentPart["source"]> };

export interface ToolView {
  name: string;
  input: CodeView | null;
  outputs: OutputView[];
  /** Facts about the whole call, e.g. the code-mode script's wall time. */
  meta: string[];
}

// Shell tools, by the name each producer records, and the argument holding
// the command: terminus-2, claude-code, codex, gemini-cli, openhands.
const COMMAND_FIELDS: Record<string, string> = {
  bash_command: "keystrokes",
  Bash: "command",
  bash: "command",
  shell: "command",
  exec_command: "cmd",
  run_shell_command: "command",
  execute_bash: "command",
};

// Codex code mode runs a JavaScript program (`exec`) that calls the other
// tools; `apply_patch` takes a patch as its whole input.
const SOURCE_TOOLS: Record<string, string> = {
  exec: "javascript",
  apply_patch: "diff",
};

type ToolArgs = Record<string, unknown> | string;

/** All readable text in a message or observation; image parts carry none. */
export function contentText(
  content: MessageContent | ObservationContent
): string {
  if (content == null) return "";
  if (typeof content === "string") return content;
  return content
    .filter((part) => part.type === "text")
    .map((part) => part.text ?? "")
    .join("\n");
}

function firstLine(text: string): string {
  return text.trim().split("\n")[0]?.trim() ?? "";
}

function commandText(value: unknown): string | null {
  if (typeof value === "string") return value.replace(/\n+$/, "") || null;
  // codex records `["bash", "-lc", "<script>"]` for some shell calls.
  if (Array.isArray(value) && value.every((v) => typeof v === "string")) {
    const [shell, flag, script] = value;
    return value.length === 3 && /sh$/.test(shell) && /^-\w*c$/.test(flag)
      ? script
      : value.join(" ") || null;
  }
  return null;
}

function isScalar(value: unknown): value is string | number | boolean {
  return ["string", "number", "boolean"].includes(typeof value);
}

/** The input to show for one call: its command, patch or script when the
 *  tool is known and the remaining arguments are scalars, else the JSON. */
export function toolInput(name: string, args: ToolArgs): CodeView | null {
  const field = Object.hasOwn(COMMAND_FIELDS, name)
    ? COMMAND_FIELDS[name]
    : Object.hasOwn(SOURCE_TOOLS, name)
      ? "input"
      : null;
  if (typeof args === "string") {
    if (!args.trim()) return null;
    const language = field === "input" ? SOURCE_TOOLS[name] : "text";
    return { code: args, language, meta: [] };
  }
  const code = field ? commandText(args[field]) : null;
  const rest = Object.entries(args).filter(([key]) => key !== field);
  if (code && rest.every(([, value]) => isScalar(value))) {
    return {
      code,
      language: field === "input" ? SOURCE_TOOLS[name] : "bash",
      meta: rest.map(([key, value]) => `${key} ${value}`),
    };
  }
  if (!Object.keys(args).length) return null;
  return { code: JSON.stringify(args, null, 2), language: "json", meta: [] };
}

function scriptTitle(source: string): string | null {
  // Header text only; the body always shows the script itself.
  const commands = Array.from(
    source.matchAll(/\bcmd\s*:\s*("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')/g),
    (match) => tryJson(match[1]) ?? match[1].slice(1, -1)
  );
  if (commands.length && typeof commands[0] === "string") {
    const more = commands.length > 1 ? ` (+${commands.length - 1} more)` : "";
    return firstLine(commands[0]) + more;
  }
  const tools = new Set(
    Array.from(source.matchAll(/\btools\.(\w+)\(/g), (match) => match[1])
  );
  return tools.size ? [...tools].join(", ") : null;
}

/** One line naming what a call did, for collapsed step headers. */
export function toolTitle(name: string, args: ToolArgs): string {
  // Unknown tools are titled by name without serializing their arguments.
  const known =
    Object.hasOwn(COMMAND_FIELDS, name) || Object.hasOwn(SOURCE_TOOLS, name);
  const input = known ? toolInput(name, args) : null;
  if (input?.language === "bash") return firstLine(input.code);
  if (input?.language === "diff") {
    const files = Array.from(
      input.code.matchAll(/^\*\*\* (?:Add|Update|Delete) File: (.+)$/gm),
      (match) => match[1].split("/").pop()
    );
    if (files.length) return `Edit ${files.join(", ")}`;
  }
  if (input?.language === "javascript") return scriptTitle(input.code) ?? name;
  return name || "tool";
}

function tryJson(text: string): unknown {
  // Past this size a parse would stall the tab; the text shows as-is.
  if (text.length > 1_000_000 || !/^\s*["[{]/.test(text)) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** A message that is one complete JSON object or array, indented; else null. */
export function indentedJson(text: string): string | null {
  const value = tryJson(text);
  return typeof value === "object" && value !== null
    ? JSON.stringify(value, null, 2)
    : null;
}

type Json = Record<string, unknown>;
const isObject = (v: unknown): v is Json =>
  typeof v === "object" && v !== null && !Array.isArray(v);

// {type: "input_text", text}: codex tool-output blocks inside a string.
const isTextBlock = (v: unknown): v is { text: string } =>
  isObject(v) &&
  ["text", "input_text", "output_text"].includes(v.type as string) &&
  typeof v.text === "string";

// {i, status, value | reason}: one entry of a code-mode Promise.allSettled.
const isSettled = (v: unknown): v is Json =>
  isObject(v) &&
  Number.isInteger(v.i) &&
  (v.status === "fulfilled" || v.status === "rejected");

// {output, wall_time_seconds, exit_code?, session_id?}: codex exec_command.
const isProcess = (v: unknown): v is Json & { output: string } =>
  isObject(v) &&
  typeof v.output === "string" &&
  typeof v.wall_time_seconds === "number";

function processMeta(process: Json): string[] {
  const meta = [`${(process.wall_time_seconds as number).toFixed(1)}s`];
  if (process.exit_code != null) meta.unshift(`exit ${process.exit_code}`);
  else if (process.session_id != null)
    meta.unshift(`session ${process.session_id} running`);
  return meta;
}

function textView(text: string, meta: string[]): OutputView {
  const language = text.includes("\x1b[") ? "ansi" : "text";
  return { kind: "code", code: text, language, meta };
}

/**
 * Peels the wrappers codex puts around command output (text blocks, settled
 * promises, process records) and indents JSON. `envelopes` is off for a
 * process's own stdout, so JSON a command printed is shown, not unwrapped.
 */
function unwrap(
  value: unknown,
  meta: string[],
  envelopes = true
): OutputView[] {
  const original = typeof value === "string" ? value : undefined;
  if (original !== undefined) {
    const parsed = tryJson(original);
    if (parsed === undefined || typeof parsed === "string")
      return [textView(original, meta)];
    value = parsed;
  }
  if (envelopes) {
    const items = Array.isArray(value) ? value : [value];
    if (items.length && items.every(isTextBlock))
      return items.flatMap((block) => unwrap(block.text, meta));
    if (items.length && items.every(isSettled))
      return items.flatMap((item) =>
        unwrap(item.status === "fulfilled" ? item.value : item.reason, [
          ...meta,
          `call ${(item.i as number) + 1}`,
          ...(item.status === "rejected" ? ["rejected"] : []),
        ])
      );
    if (isProcess(value))
      return unwrap(value.output, [...meta, ...processMeta(value)], false);
  }
  if (typeof value === "string") return [textView(value, meta)];
  const code = JSON.stringify(value, null, 2) ?? String(value);
  return [{ kind: "code", code, language: "json", copyText: original, meta }];
}

const EXEC_WRAPPER =
  /^Script completed\nWall time ([\d.]+) seconds\nOutput:\s*$/;

/** Output views for one observation, plus facts about the whole call. */
export function outputViews(content: ObservationContent): {
  outputs: OutputView[];
  meta: string[];
} {
  const parts: ContentPart[] =
    typeof content === "string"
      ? [{ type: "text", text: content }]
      : (content ?? []);
  const outputs: OutputView[] = [];
  const meta: string[] = [];
  for (const part of parts) {
    const wrapper = EXEC_WRAPPER.exec(part.text ?? "");
    if (part.type === "image" && part.source)
      outputs.push({ kind: "image", source: part.source });
    else if (wrapper) meta.push(`script ${wrapper[1]}s`);
    else outputs.push(...unwrap(part.text ?? "", []));
  }
  return { outputs, meta };
}

/**
 * Terminus rejects a response that is not valid JSON and records the reason
 * as an observation, with no tool call. Matches only that runner diagnostic.
 */
export function stepRejection(
  step: TrajectoryStep
): { reason: string; diagnostic: string } | null {
  if (step.source !== "agent" || step.tool_calls?.length) return null;
  for (const result of step.observation?.results ?? []) {
    const diagnostic = contentText(result.content).trim();
    if (!diagnostic.startsWith("Previous response had parsing errors:"))
      continue;
    const error = /^ERROR: (.*)$/m.exec(diagnostic)?.[1] ?? "Invalid response";
    return { reason: error.split(" | Content preview:")[0], diagnostic };
  }
  return null;
}

export interface StepView {
  rejection: ReturnType<typeof stepRejection>;
  /** False when the message only repeats the one command shown below it. */
  showMessage: boolean;
  tools: ToolView[];
  /** Outputs whose `source_call_id` matches no call in this step. */
  unlinked: OutputView[];
}

export function stepView(step: TrajectoryStep): StepView {
  const calls = step.tool_calls ?? [];
  const results = step.observation?.results ?? [];
  const rejection = stepRejection(step);
  const tools = calls.map((call): ToolView => {
    const views = results
      .filter((result) => result.source_call_id === call.tool_call_id)
      .map((result) => outputViews(result.content));
    return {
      name: call.function_name,
      input: toolInput(call.function_name, call.arguments),
      outputs: views.flatMap((view) => view.outputs),
      meta: views.flatMap((view) => view.meta),
    };
  });
  const ids = new Set(calls.map((call) => call.tool_call_id));
  const unlinked = rejection
    ? []
    : results
        .filter((result) => !ids.has(result.source_call_id ?? ""))
        .flatMap((result) => outputViews(result.content).outputs);
  const only = tools.length === 1 ? tools[0].input : null;
  const echo = only?.language === "bash" && `Executed command: ${only.code}`;
  return {
    rejection,
    showMessage: !rejection && contentText(step.message).trim() !== echo,
    tools,
    unlinked,
  };
}

/**
 * One line describing a step, for its collapsed header. Commands come first:
 * they say what the step did. Otherwise fall through the step's text in the
 * order a reader wants it. Producers disagree about where prose goes: codex
 * fills `message` every step, claude-code on 58% of them, gemini-cli on ~0.4%
 * (it routes prose into `reasoning_content`).
 */
export function stepTitle(step: TrajectoryStep): string | null {
  const rejection = stepRejection(step);
  if (rejection) return rejection.reason;
  const calls = step.tool_calls ?? [];
  if (calls.length)
    return calls
      .map((call) => toolTitle(call.function_name, call.arguments))
      .join(" · ");
  const texts: (MessageContent | ObservationContent)[] = [
    step.message,
    step.reasoning_content,
    ...(step.observation?.results ?? []).map((result) => result.content),
  ];
  for (const text of texts) {
    const full = contentText(text);
    if (text === step.message && indentedJson(full)) return "JSON response";
    const line =
      taggedContext(full)?.label ?? firstLine(full).replace(/^#{1,6}\s+/, "");
    if (line) return line;
  }
  return null;
}

/** `<tag>body</tag>` wrapping a whole message, as codex records context. */
export function taggedContext(
  text: string
): { label: string; body: string; fields: [string, string][] | null } | null {
  const match = /^<([a-z][\w-]*)>([\s\S]*)<\/\1>$/.exec(text.trim());
  if (!match || match[2].includes(`</${match[1]}>`)) return null;
  // Flat `<key>value</key>` children read as fields; anything else as text.
  const fields: [string, string][] = [];
  const field = /\s*<([a-z][\w-]*)>([^<]*)<\/\1>\s*/y;
  let end = 0;
  for (let m; (m = field.exec(match[2])); end = field.lastIndex)
    fields.push([m[1].replaceAll("_", " "), m[2].trim()]);
  return {
    label: match[1].replaceAll("_", " "),
    body: match[2].trim(),
    fields: fields.length && end === match[2].length ? fields : null,
  };
}
