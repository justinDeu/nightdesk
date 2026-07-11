// Composer autocomplete seed parsed out of the transcript's server_info event.
// Kept out of AgentComposer.tsx so AgentScreen can import it without pulling
// the (lazy, tiptap-heavy) composer chunk in eagerly.

/** One slash-invokable entry (command or skill) from the runner's server_info. */
export interface CommandInfo {
  name: string;
  description?: string;
}

export interface ServerCommands {
  commands: CommandInfo[];
  skills: CommandInfo[];
}

/**
 * Normalizes a server_info `commands`/`skills` payload. Live runners send a
 * list of {name, description, argumentHint} objects; older ones sent plain
 * strings. Anything else is dropped. Deduped by name (live runners repeat
 * entries when a skill is registered in two scopes); first entry wins.
 */
export function normalizeCommandList(raw: unknown): CommandInfo[] {
  if (!Array.isArray(raw)) return [];
  const byName = new Map<string, CommandInfo>();
  for (const entry of raw) {
    let item: CommandInfo | null = null;
    if (typeof entry === "string") {
      if (entry) item = { name: entry };
    } else if (entry && typeof entry === "object") {
      const o = entry as { name?: unknown; description?: unknown };
      if (typeof o.name === "string" && o.name) {
        item = {
          name: o.name,
          description: typeof o.description === "string" && o.description ? o.description : undefined,
        };
      }
    }
    if (item && !byName.has(item.name)) byName.set(item.name, item);
  }
  return [...byName.values()];
}
