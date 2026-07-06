import { useState } from "react";
import {
  AlertTriangle,
  Check,
  Copy,
  Inbox,
  MoreHorizontal,
  Play,
  Plus,
  Trash2,
} from "lucide-react";
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardFooter,
  CardHeader,
  Dialog,
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  EmptyState,
  Field,
  IconButton,
  Input,
  Kbd,
  Select,
  Spinner,
  StatusPill,
  Textarea,
  Tooltip,
  toast,
} from "@/ui";
import type { BadgeTone } from "@/ui/Badge";
import type { StatusKind } from "@/ui/StatusPill";
import type { ButtonVariant } from "@/ui/Button";
import { cn } from "@/lib/cn";

function Section({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <section className="mb-10">
      <div className="mb-3 border-b border-ink-700 pb-2">
        <h2 className="font-display text-sm font-semibold uppercase tracking-wide text-lamp">
          {title}
        </h2>
        {note && <p className="mt-0.5 text-xs text-moon-600">{note}</p>}
      </div>
      <div className="flex flex-wrap items-start gap-4">{children}</div>
    </section>
  );
}

const STATUSES: StatusKind[] = [
  "draft",
  "queued",
  "running",
  "review",
  "archived",
  "success",
  "failed",
];
const BADGE_TONES: BadgeTone[] = [
  "neutral",
  "lamp",
  "review",
  "queued",
  "success",
  "failed",
  "draft",
];
const BUTTON_VARIANTS: ButtonVariant[] = ["primary", "ghost", "subtle", "danger"];

const SWATCHES = [
  ["ink-950", "bg-ink-950"],
  ["ink-900", "bg-ink-900"],
  ["ink-800", "bg-ink-800"],
  ["ink-700", "bg-ink-700"],
  ["moon-100", "bg-moon-100"],
  ["moon-400", "bg-moon-400"],
  ["moon-600", "bg-moon-600"],
  ["lamp", "bg-lamp"],
  ["dawn", "bg-dawn"],
  ["review", "bg-review"],
  ["queued", "bg-queued"],
  ["success", "bg-success"],
  ["failed", "bg-failed"],
];

export function KitchenSink() {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [checked, setChecked] = useState(true);

  return (
    <div className="min-h-screen bg-ink-950 px-8 py-8">
      <header className="mb-10">
        <div className="flex items-center gap-2.5">
          <span className="dawn-edge grid h-8 w-8 place-items-center rounded-[9px] text-ink-950">
            <span className="font-display text-sm font-bold">n</span>
          </span>
          <h1 className="font-display text-2xl font-semibold tracking-tight text-moon-100">
            Kitchen sink
          </h1>
        </div>
        <p className="mt-2 max-w-2xl text-sm text-moon-400">
          Every primitive in the dusk-console design system, in each state. This page is how the
          design gets reviewed.
        </p>
      </header>

      <Section title="Color tokens">
        <div className="flex flex-wrap gap-3">
          {SWATCHES.map(([name, bg]) => (
            <div key={name} className="flex flex-col items-center gap-1.5">
              <div className={cn("h-12 w-16 rounded-card border border-ink-700", bg)} />
              <span className="font-mono text-[11px] text-moon-400">{name}</span>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Typography" note="Space Grotesk display · Inter UI · IBM Plex Mono data">
        <div className="space-y-2">
          <p className="font-display text-3xl font-semibold text-moon-100">Fix the flaky worker heartbeat</p>
          <p className="font-sans text-sm text-moon-400">
            Inter carries all UI copy — labels, descriptions, body text — at a comfortable reading weight.
          </p>
          <p className="font-mono text-sm text-moon-100">
            run_849c42ca · $0.284 · 12.4k tok · /home/thor/fun/nightdesk
          </p>
          <p className="font-display text-5xl font-bold tabular-nums text-moon-100">
            $1.28 <span className="text-lg font-medium text-moon-400">today</span>
          </p>
        </div>
      </Section>

      <Section title="Buttons">
        {BUTTON_VARIANTS.map((v) => (
          <div key={v} className="flex flex-col gap-2">
            <Button variant={v} leadingIcon={<Play size={14} />}>
              {v}
            </Button>
            <Button variant={v} size="sm">
              small
            </Button>
            <Button variant={v} loading>
              loading
            </Button>
            <Button variant={v} disabled>
              disabled
            </Button>
          </div>
        ))}
      </Section>

      <Section title="Icon buttons" note="Tooltip-labeled; never native title=">
        <IconButton label="Run now" icon={<Play size={16} />} />
        <IconButton label="Add" icon={<Plus size={16} />} active />
        <IconButton label="Copy" icon={<Copy size={16} />} size="sm" />
        <IconButton label="Delete" icon={<Trash2 size={16} />} disabled />
        <IconButton label="More" icon={<MoreHorizontal size={16} />} />
      </Section>

      <Section title="Status pills" note="Running carries the animated dawn edge">
        {STATUSES.map((s) => (
          <StatusPill key={s} status={s} />
        ))}
      </Section>

      <Section title="Badges">
        {BADGE_TONES.map((t) => (
          <Badge key={t} tone={t} dot>
            {t}
          </Badge>
        ))}
        <Badge tone="neutral" mono>
          P2
        </Badge>
        <Badge tone="lamp" mono>
          run_now
        </Badge>
      </Section>

      <Section title="Inputs">
        <div className="w-64 space-y-4">
          <Field label="Title" hint="Space Grotesk-worthy one-liner">
            <Input placeholder="Fix the flaky test" />
          </Field>
          <Field label="Source path">
            <Input mono placeholder="/home/thor/fun/nightdesk" />
          </Field>
          <Field label="Priority">
            <Select defaultValue="0">
              <option value="0">P0 · none</option>
              <option value="1">P1 · low</option>
              <option value="2">P2 · medium</option>
            </Select>
          </Field>
          <Field label="Invalid" error="source_path must be absolute">
            <Input invalid defaultValue="relative/path" mono />
          </Field>
          <Field label="Prompt">
            <Textarea placeholder="Describe the task for the agent…" />
          </Field>
        </div>
      </Section>

      <Section title="Cards">
        <Card className="w-72">
          <CardHeader>
            <div className="flex items-center justify-between">
              <h3 className="font-display text-sm font-semibold text-moon-100">Static card</h3>
              <StatusPill status="review" />
            </div>
          </CardHeader>
          <CardBody>
            <p className="text-sm text-moon-400">Panels sit on ink-900 with a 1px ink-700 border.</p>
          </CardBody>
          <CardFooter>
            <span className="font-mono text-xs text-moon-600">updated 2m ago</span>
          </CardFooter>
        </Card>

        <Card className="w-72" running raised>
          <CardHeader>
            <div className="flex items-center justify-between">
              <h3 className="font-display text-sm font-semibold text-moon-100">Running ticket</h3>
              <StatusPill status="running" />
            </div>
          </CardHeader>
          <CardBody>
            <p className="text-sm text-moon-400">The dawn edge drifts along the top hairline.</p>
            <p className="mt-2 font-mono text-xs text-lamp">$0.14 · 3.2k tok · 01:42</p>
          </CardBody>
        </Card>

        <Card className="w-72" interactive>
          <CardBody>
            <p className="text-sm text-moon-100">Interactive card — hover me.</p>
            <p className="mt-1 text-xs text-moon-600">Raises to ink-800 on hover.</p>
          </CardBody>
        </Card>
      </Section>

      <Section title="Overlays">
        <Button variant="ghost" onClick={() => setDialogOpen(true)}>
          Open dialog
        </Button>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" trailingIcon={<MoreHorizontal size={14} />}>
              Dropdown
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent>
            <DropdownMenuLabel>Ticket actions</DropdownMenuLabel>
            <DropdownMenuItem icon={<Play size={15} />} shortcut="R">
              Run now
            </DropdownMenuItem>
            <DropdownMenuItem icon={<Copy size={15} />}>Duplicate</DropdownMenuItem>
            <DropdownMenuCheckboxItem checked={checked} onCheckedChange={setChecked}>
              Commit on finish
            </DropdownMenuCheckboxItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem icon={<Trash2 size={15} />} danger>
              Archive
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        <Tooltip content="I'm a styled tooltip">
          <Button variant="subtle">Hover for tooltip</Button>
        </Tooltip>

        <Button variant="ghost" onClick={() => toast.success("Ticket queued", { description: "run_now dispatched" })}>
          Success toast
        </Button>
        <Button
          variant="danger"
          onClick={() => toast.error("Drop failed", { description: "ticket is not in review" })}
        >
          Error toast
        </Button>
      </Section>

      <Section title="Keyboard">
        <div className="flex items-center gap-2 text-sm text-moon-400">
          Open palette
          <span className="flex gap-1">
            <Kbd>⌘</Kbd>
            <Kbd>K</Kbd>
          </span>
        </div>
        <div className="flex items-center gap-2 text-sm text-moon-400">
          Go to Desk
          <span className="flex gap-1">
            <Kbd>g</Kbd>
            <Kbd>d</Kbd>
          </span>
        </div>
      </Section>

      <Section title="Feedback states">
        <Spinner />
        <Spinner size={22} />
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <Check size={15} className="text-success" /> verified
        </div>
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <AlertTriangle size={15} className="text-warn" /> attention
        </div>
      </Section>

      <Section title="Empty state">
        <EmptyState
          className="w-full"
          icon={<Inbox size={18} />}
          title="Inbox is empty"
          description="Captured items land here for a completeness check before promotion."
          action={<Button variant="primary">Capture a task</Button>}
        />
      </Section>

      <Dialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        title="Archive ticket?"
        description="It stays searchable and can be restored later."
        footer={
          <>
            <Button variant="ghost" onClick={() => setDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                setDialogOpen(false);
                toast.success("Ticket archived");
              }}
            >
              Archive
            </Button>
          </>
        }
      >
        <p className="text-sm text-moon-400">
          This dialog demonstrates the overlay blur, pop-in motion, and focus trapping.
        </p>
      </Dialog>
    </div>
  );
}
