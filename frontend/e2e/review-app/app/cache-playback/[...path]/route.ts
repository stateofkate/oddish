import { readFile } from "node:fs/promises";
import path from "node:path";
import playbackIndex from "../../../../fixtures/robot-playback-index.json";

// Real HTTP responses: Playwright routing would disable the browser cache.
let revision = "cache-v1";
export async function POST(request: Request) {
  revision = await request.text();
  return new Response(null, { status: 204 });
}

export async function GET(_request: Request, context: { params: Promise<{ path: string[] }> }) {
  const file = (await context.params).path.at(-1)!;
  const headers = { "Cache-Control": "private, max-age=300, stale-while-revalidate=60" };
  if (file === `${revision}.json`)
    return Response.json({ ...playbackIndex, revision }, { headers: { "Cache-Control": "no-store" } });
  if (file === "outcome.json")
    return Response.json({ checks: Object.fromEntries(playbackIndex.manifest.checks.map(check => [check.key, revision === "cache-v2"])) }, { headers });
  const fixture = file === "recording.rrd"
    ? "robot-embedded-recording.rrd"
    : revision === "cache-v2" && file === "o0.jpg" ? "o1.jpg" : file;
  if (!["robot-embedded-recording.rrd", "o0.jpg", "o1.jpg", "o2.jpg", "o3.jpg"].includes(fixture))
    return new Response(null, { status: 404 });
  let bytes = await readFile(path.resolve("../fixtures", fixture));
  // Concatenated .rrd streams are one valid recording with different bytes.
  if (file === "recording.rrd" && revision === "cache-v2") bytes = Buffer.concat([bytes, bytes]);
  return new Response(bytes, { headers: { ...headers, "Content-Type": file.endsWith(".jpg") ? "image/jpeg" : "application/octet-stream" } });
}
