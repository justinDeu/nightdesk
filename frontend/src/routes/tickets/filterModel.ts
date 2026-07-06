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

/** Every filter dimension the parser knows how to type. A given surface offers
 *  a subset (see ``TICKETS_FILTER_KEYS`` / ``ARCHIVE_FILTER_KEYS``): a colon
 *  fragment whose key is outside the active subset falls through to free text,
 *  so the same parser drives both boards without one leaking the other's
 *  vocabulary. */
export const ALL_FILTER_KEYS = [
  "status",
  "project",
  "label",
  "priority",
  "profile",
  "outcome",
] as const;
export type FilterKey = (typeof ALL_FILTER_KEYS)[number];

/** The tracker board keys — unchanged from before ``outcome`` existed, so the
 *  Tickets page (which also runs ``applyFilter`` client-side) is untouched. */
export const TICKETS_FILTER_KEYS = [
  "status",
  "project",
  "label",
  "priority",
  "profile",
] as const satisfies readonly FilterKey[];

/** Archive board keys: no ``status`` (every row is archived) but adds
 *  ``outcome`` (the ticket's last run succeeded/failed). */
export const ARCHIVE_FILTER_KEYS = [
  "project",
  "label",
  "priority",
  "profile",
  "outcome",
] as const satisfies readonly FilterKey[];

/** Default subset for callers that don't pass one (the Tickets board). */
export const FILTER_KEYS = TICKETS_FILTER_KEYS;

export interface FilterToken {
  key: FilterKey;
  value: string;
}

export interface ParsedFilter {
  tokens: FilterToken[];
  text: string;
}

/** Parse a raw filter string into typed tokens + residual free text. Only
 *  colon fragments whose key is in ``keys`` become tokens; the rest is text. */
export function parseFilter(
  raw: string,
  keys: readonly FilterKey[] = FILTER_KEYS,
): ParsedFilter {
  const allowed = new Set<string>(keys);
  const tokens: FilterToken[] = [];
  const words: string[] = [];
  for (const part of raw.split(/\s+/)) {
    if (!part) continue;
    const idx = part.indexOf(":");
    if (idx > 0) {
      const key = part.slice(0, idx).toLowerCase();
      const value = part.slice(idx + 1);
      if (allowed.has(key) && value) {
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

/**
 * Normalize a filter string set *programmatically* (a restored Saved View, a
 * deep link) so the FilterBar renders it fully as pills. If it ends in a
 * complete `key:value` token with no trailing space, append one so that token
 * commits to a pill instead of landing in the input as editable draft text
 * (the FilterBar only pills a token once whitespace follows it).
 *
 * A no-op for values that already end in whitespace or in free text — a
 * trailing free-text word must stay editable. Do NOT call this on the live
 * typing round-trip: it would commit a half-typed token mid-keystroke (e.g.
 * `project:n` is a "complete" token while you're still typing `project:night`).
 */
export function commitTrailingToken(
  raw: string,
  keys: readonly FilterKey[] = FILTER_KEYS,
): string {
  if (!raw || /\s$/.test(raw)) return raw;
  const last = raw.match(/\S+$/)?.[0] ?? "";
  const idx = last.indexOf(":");
  if (idx > 0) {
    const key = last.slice(0, idx).toLowerCase();
    const val = last.slice(idx + 1);
    if (new Set<string>(keys).has(key) && val) return `${raw} `;
  }
  return raw;
}

/** Server-side query shape a parsed filter resolves to. Names are resolved to
 *  ids here (client-side, where the project/profile lists live) so the API only
 *  ever filters by id/exact value. Mirrors the query params `GET
 *  /api/v1/tickets` accepts. */
export interface ServerFilterParams {
  project_id?: string;
  profile_id?: string;
  priority?: number;
  label?: string;
  outcome?: "succeeded" | "failed";
  q?: string;
}

/**
 * Translate a parsed filter into server query params (the Archive page's path —
 * server-side filtering that composes with limit/offset paging, unlike the
 * Tickets board which filters `applyFilter` client-side over an already-loaded
 * page). Each dimension is single-valued (last token wins) and AND-combined; a
 * token that resolves to nothing (unknown project/profile/priority) is dropped
 * rather than sent, so a typo never silently empties the list server-side.
 */
export function filterToQuery(parsed: ParsedFilter, ctx: FilterContext): ServerFilterParams {
  const out: ServerFilterParams = {};
  for (const tok of parsed.tokens) {
    switch (tok.key) {
      case "project": {
        const v = tok.value.toLowerCase();
        if (v === "none" || v === "null") {
          out.project_id = "null";
          break;
        }
        const id = matchProject(tok.value, ctx.projects);
        if (id) out.project_id = id;
        break;
      }
      case "profile": {
        const prof = ctx.profiles.find(
          (p) => p.name.toLowerCase() === tok.value.toLowerCase() || p.id === tok.value,
        );
        if (prof) out.profile_id = prof.id;
        break;
      }
      case "priority": {
        const pv = priorityValue(tok.value);
        if (pv != null) out.priority = pv;
        break;
      }
      case "label": {
        // Send the raw value; the server matches label name (case-insensitive)
        // or id, so no client-side id resolution is needed.
        out.label = tok.value;
        break;
      }
      case "outcome": {
        const v = tok.value.toLowerCase();
        if (v === "succeeded" || v === "success" || v === "ok") out.outcome = "succeeded";
        else if (v === "failed" || v === "fail" || v === "error") out.outcome = "failed";
        break;
      }
      case "status":
        // Not a dimension the Archive exposes (every row is archived).
        break;
    }
  }
  const text = parsed.text.trim();
  if (text) out.q = text;
  return out;
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
    case "outcome":
      return pick(["succeeded", "failed"]);
    default:
      return [];
  }
}
