import { useEffect } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useProjectAttention } from "@/api/projects";

/**
 * /projects — no longer an index page. The sidebar group + project strip own
 * picking, so /projects redirects to the highest-attention project's Overview
 * (the attention rollup is display-ordered, so the first row is highest
 * attention), or to Desk when no projects exist.
 *
 * docs/design/project-control-plane.md §Chrome: "/projects index page: deleted."
 */
export function ProjectsRedirect() {
  const navigate = useNavigate();
  const attention = useProjectAttention();
  const projects = attention.data;

  useEffect(() => {
    if (attention.isLoading) return; // wait for the rollup to resolve
    if (projects && projects.length > 0) {
      navigate({
        to: "/projects/$id",
        params: { id: projects[0].id },
        search: { tab: "overview" },
        replace: true,
      });
    } else {
      navigate({ to: "/", replace: true });
    }
  }, [projects, attention.isLoading, navigate]);

  return (
    <div className="grid h-full place-items-center text-sm text-moon-600">Loading projects…</div>
  );
}
