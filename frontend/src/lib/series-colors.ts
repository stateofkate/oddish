// Colors are assigned deterministically per key name so a given model/user
// keeps the same hue across renders and charts, rather than by position,
// which made the color depend on rank. Greedy collision avoidance keeps the
// keys of one chart distinct while the palette has free slots.

function hashKey(key: string): number {
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return hash;
}

export function stableSeriesColors(
  keys: string[],
  palette: readonly string[]
): Record<string, string> {
  const result: Record<string, string> = {};
  const used = new Set<number>();
  const pending: string[] = [];
  for (const key of keys) {
    const pref = hashKey(key) % palette.length;
    if (used.has(pref)) {
      pending.push(key);
    } else {
      used.add(pref);
      result[key] = palette[pref];
    }
  }
  for (const key of pending) {
    let slot = hashKey(key) % palette.length;
    if (used.size < palette.length) {
      for (let step = 0; step < palette.length; step += 1) {
        const candidate = (slot + step) % palette.length;
        if (!used.has(candidate)) {
          slot = candidate;
          break;
        }
      }
    }
    used.add(slot);
    result[key] = palette[slot];
  }
  return result;
}
