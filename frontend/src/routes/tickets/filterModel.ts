/**
 * Linear-style token filter model for the tracker.
 *
 * A raw query string is parsed into typed chips (`status:`, `project:`,
 * `label:`, `priority:`, `profile:`) plus free text. The parsed filter both
 * drives client-side filtering and round-trips through the URL search params so
 * a filtered view is shareable and back/forward works.
 */
import type { LabelOut, ProfileOut, ProjectOut, TicketOut } from "@/api/types";
import { PRIORITY_SCALE } from "@/lib/priority";

export const FILTER_KEYS = ["status", "project", "label", "priority", "profile"] as const;
export type FilterKey = (typeof FILTER_KEYS)[number];

export interface FilterToken {
  key: FilterKey;
  value: string;
}

export interface ParsedFilter {
  tokens: FilterToken[];
  text: string;
}

const KEY_SET = new Set<string>(FILTER_KEYS);

/** Parse a raw filter string into typed tokens + residual free text. */
export function parseFilter(raw: string): ParsedFilter {
  const tokens: FilterToken[] = [];
  const words: string[] = [];
  for (const part of raw.split(/\s+/)) {
    if (!part) continue;
    const idx = part.indexOf(":");
    if (idx > 0) {
      const key = part.slice(0, idx).toLowerCase();
      const value = part.slice(idx + 1);
      if (KEY_SET.has(key) && value) {
        tokens.push({ key: key as FilterKey, value });
        continue;
      }
    }
    words.push(part);
  }
  return { tokens, text: words.join(" ") };
}

/** Serialize tokens + text back to a raw query string. */
export function serializeFilter(f: ParsedFilter): string {
  const t = f.tokens.map((tok) => `${tok.key}:${tok.value}`).join(" ");
  return [t, f.text].filter(Boolean).join(" ").trim();
}

export interface FilterContext {
  projects: ProjectOut[];
  labels: LabelOut[];
  profiles: ProfileOut[];
}

function matchProject(value: string, projects: ProjectOut[]): string | null {
  const v = value.toLowerCase();
  const hit = projects.find(
    (p) => p.slug.toLowerCase() === v || p.name.toLowerCase() === v || p.id === value,
  );
  return hit?.id ?? null;
}

function priorityValue(value: string): number | null {
  const named = PRIORITY_SCALE.find((p) => p.name === value.toLowerCase());
  if (named) return named.value;
  const short = PRIORITY_SCALE.find((p) => p.short.toLowerCase() === value.toLowerCase());
  if (short) return short.value;
  const n = Number(value);
  return Number.isInteger(n) && n >= 0 && n <= 4 ? n : null;
}

/** Apply a parsed filter to a ticket list. AND across distinct keys; OR within
 *  repeated keys (e.g. two `status:` tokens widen the status set). */
export function applyFilter(
  tickets: TicketOut[],
  parsed: ParsedFilter,
  ctx: FilterContext,
): TicketOut[] {
  const byKey = new Map<FilterKey, string[]>();
  for (const t of parsed.tokens) {
    const arr = byKey.get(t.key) ?? [];
    arr.push(t.value);
    byKey.set(t.key, arr);
  }

  const text = parsed.text.trim().toLowerCase();

  return tickets.filter((t) => {
    for (const [key, values] of byKey) {
      const ok = values.some((value) => {
        switch (key) {
          case "status":
            return t.status.toLowerCase() === value.toLowerCase();
          case "project": {
            const id = matchProject(value, ctx.projects);
            return id != null && t.project_id === id;
          }
          case "label":
            return t.labels.some((l) => l.name.toLowerCase() === value.toLowerCase());
          case "priority": {
            const pv = priorityValue(value);
            return pv != null && t.priority === pv;
          }
          case "profile": {
            const prof = ctx.profiles.find(
              (p) => p.name.toLowerCase() === value.toLowerCase() || p.id === value,
            );
            return prof != null && t.profile_id === prof.id;
          }
          default:
            return false;
        }
      });
      if (!ok) return false;
    }
    if (text) {
      const hay = `${t.title} ${t.prompt}`.toLowerCase();
      if (!hay.includes(text)) return false;
    }
    return true;
  });
}

/** Suggestions for the active partial token (`key:partial`). */
export function suggestValues(key: FilterKey, partial: string, ctx: FilterContext): string[] {
  const p = partial.toLowerCase();
  const pick = (arr: string[]) =>
    arr.filter((v) => v.toLowerCase().includes(p)).slice(0, 8);
  switch (key) {
    case "status":
      return pick(["draft", "queued", "running", "review", "archived"]);
    case "project":
      return pick(ctx.projects.map((x) => x.slug));
    case "label":
      return pick(ctx.labels.map((x) => x.name));
    case "priority":
      return pick(PRIORITY_SCALE.map((x) => x.name));
    case "profile":
      return pick(ctx.profiles.map((x) => x.name));
    default:
      return [];
  }
}
