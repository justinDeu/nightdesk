import { CircleDot, GitMerge } from "lucide-react";
import type { ReactNode } from "react";
import { Badge } from "@/ui/Badge";
import type { BadgeTone } from "@/ui/Badge";
import { Tooltip } from "@/ui/Tooltip";
import type { ExternalLinkKind, ExternalLinkOut, ExternalLinkRole } from "@/api/types";

/** State → chip tone, per kind (§7): MR merged reads jade, closed-without-merge
 *  reads failed, everything open reads amber. Issues chip amber/neutral. */
export function externalStateTone(kind: ExternalLinkKind, state: string | null): BadgeTone {
  const s = (state ?? "").toLowerCase();
  if (kind === "merge_request") {
    if (s === "merged") return "success";
    if (s === "closed") return "failed";
    return "review";
  }
  if (s === "closed") return "neutral";
  return "review";
}

export function externalStateLabel(state: string | null): string {
  return state ?? "unknown";
}

const ROLE_LABEL: Record<ExternalLinkRole, string> = {
  fixes: "fixes",
  references: "references",
  produced_mr: "MR",
  imported_from: "imported",
};

export function roleLabel(role: ExternalLinkRole): string {
  return ROLE_LABEL[role] ?? role;
}

export function externalKindGlyph(kind: ExternalLinkKind): ReactNode {
  return kind === "merge_request" ? <GitMerge size={12} /> : <CircleDot size={12} />;
}

export function externalRefLabel(kind: ExternalLinkKind, iid: string): string {
  return `${kind === "merge_request" ? "!" : "#"}${iid}`;
}

/** A compact merge-request chip for the BoardCard meta row: only the highest-
 *  signal state chips a card (produced_mr), with tone by MR state. Presentational
 *  — the board wires it once a batched external-link feed exists (§8 v2). */
export function MrChip({ link }: { link: ExternalLinkOut }) {
  const tone = externalStateTone("merge_request", link.state);
  const branch = (link.state_detail?.target_branch as string | undefined) ?? "";
  return (
    <Tooltip content={`${link.title || "merge request"} · ${externalStateLabel(link.state)}${branch ? ` → ${branch}` : ""}`}>
      <span className="inline-flex">
        <Badge tone={tone} mono dot>
          <GitMerge size={9} /> {externalRefLabel("merge_request", link.external_iid)}
        </Badge>
      </span>
    </Tooltip>
  );
}
