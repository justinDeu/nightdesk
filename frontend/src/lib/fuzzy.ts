/** Minimal subsequence fuzzy matcher. Returns a score (higher = better) or
 *  null when `query` isn't a subsequence of `text`. Consecutive and
 *  word-start matches score higher, so "dsk" ranks "Desk" above "disk cache". */
export function fuzzyScore(query: string, text: string): number | null {
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (!q) return 0;
  let qi = 0;
  let score = 0;
  let prevMatch = -2;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      score += 1;
      if (ti === prevMatch + 1) score += 2; // consecutive
      if (ti === 0 || /\W|_/.test(t[ti - 1])) score += 3; // word start
      prevMatch = ti;
      qi++;
    }
  }
  return qi === q.length ? score : null;
}

export function fuzzyFilter<T>(query: string, items: T[], key: (item: T) => string): T[] {
  if (!query.trim()) return items;
  return items
    .map((item) => ({ item, score: fuzzyScore(query, key(item)) }))
    .filter((r): r is { item: T; score: number } => r.score !== null)
    .sort((a, b) => b.score - a.score)
    .map((r) => r.item);
}
