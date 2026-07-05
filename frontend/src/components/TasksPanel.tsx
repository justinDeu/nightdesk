import { useState } from "react";
import { CheckCircle2, ChevronRight, Circle, ListChecks, Loader2 } from "lucide-react";
import type { TodoItem } from "@/lib/transcript";
import { cn } from "@/lib/cn";

/** Current agent task list (latest state) — the "what's the plan / where are we"
 *  view, visible without scrolling the transcript. */
export function TasksPanel({ todos, className }: { todos: TodoItem[]; className?: string }) {
  const [open, setOpen] = useState(true);
  if (todos.length === 0) return null;

  const done = todos.filter((t) => t.status === "completed").length;
  const active = todos.find((t) => t.status === "in_progress");

  return (
    <div className={cn("rounded-card border border-ink-700 bg-ink-900 shadow-[var(--shadow-raised)]", className)}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        aria-expanded={open}
      >
        <ChevronRight size={13} className={cn("shrink-0 text-moon-600 transition-transform", open && "rotate-90")} />
        <ListChecks size={13} className="shrink-0 text-lamp" />
        <span className="text-[11px] font-semibold uppercase tracking-wide text-moon-400">Tasks</span>
        <span className="font-mono text-[11px] tabular-nums text-moon-600">
          {done}/{todos.length}
        </span>
        {!open && active && (
          <span className="min-w-0 flex-1 truncate text-[12px] text-moon-100">
            {active.activeForm || active.label}
          </span>
        )}
      </button>
      {open && (
        <ul className="max-h-56 overflow-y-auto overscroll-contain border-t border-ink-700/60 px-2 py-1.5">
          {todos.map((t) => (
            <TodoRow key={t.id} todo={t} />
          ))}
        </ul>
      )}
    </div>
  );
}

export function TodoRow({ todo }: { todo: TodoItem }) {
  const label = todo.status === "in_progress" ? todo.activeForm || todo.label : todo.label;
  return (
    <li className="flex items-start gap-2 px-1 py-1 text-[12px] leading-snug">
      {todo.status === "completed" ? (
        <CheckCircle2 size={13} className="mt-0.5 shrink-0 text-success" />
      ) : todo.status === "in_progress" ? (
        <Loader2 size={13} className="mt-0.5 shrink-0 text-lamp motion-safe:animate-spin" />
      ) : (
        <Circle size={13} className="mt-0.5 shrink-0 text-moon-600" />
      )}
      <span
        className={cn(
          todo.status === "completed" && "text-moon-600 line-through",
          todo.status === "in_progress" && "text-moon-100",
          todo.status === "pending" && "text-moon-400",
        )}
      >
        {label}
      </span>
    </li>
  );
}
