import { api } from "./client";

export interface WorktreeNamePreviewRequest {
  source_path: string;
  name?: string | null;
  path?: string | null;
  base_ref?: string | null;
}

export interface WorktreeNamePreview {
  path: string;
  source: string;
  base_ref: string | null;
  base_ref_status: string | null;
}

/** Live preview of the resolved worktree path/branch for a template
 *  (POST /api/v1/preview/worktree-name). Debounce callers. */
export const worktreePreviewApi = {
  preview: (body: WorktreeNamePreviewRequest) =>
    api.post<WorktreeNamePreview>("/api/v1/preview/worktree-name", { body }),
};
