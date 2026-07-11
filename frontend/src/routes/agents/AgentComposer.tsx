import { useEffect, useMemo, useRef, useState } from "react";
import { Editor, EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Mention from "@tiptap/extension-mention";
import { Extension } from "@tiptap/core";
import type { Range } from "@tiptap/core";
import Suggestion from "@tiptap/suggestion";
import type { SuggestionProps, SuggestionKeyDownProps } from "@tiptap/suggestion";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { ArrowUp, FileCode2, Hash, Terminal } from "lucide-react";
import { Button } from "@/ui/Button";
import { Kbd } from "@/ui/Kbd";
import { fsApi } from "@/api/fs";
import { cn } from "@/lib/cn";

import type { ServerCommands } from "./serverInfo";

export type { ServerCommands } from "./serverInfo";

interface PopupItem {
  /** what gets inserted (raw), e.g. "/plan", a file path, or a ticket id */
  value: string;
  label: string;
  /** secondary line: command description, file path, or ticket meta */
  hint?: string;
}

interface PopupState {
  kind: "slash" | "mention";
  items: PopupItem[];
  command: (item: PopupItem) => void;
  rect: DOMRect | null;
}

// --- live highlight decoration -------------------------------------------------
// Colors /commands and `code` spans as you type. @-mentions are already chips
// (mention nodes), so they need no decoration.
const highlightKey = new PluginKey("agent-composer-highlight");

const HighlightExtension = Extension.create({
  name: "agentComposerHighlight",
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: highlightKey,
        props: {
          decorations(state) {
            const decos: Decoration[] = [];
            const push = (from: number, to: number, cls: string) =>
              decos.push(Decoration.inline(from, to, { class: cls }));
            state.doc.descendants((node, pos) => {
              if (!node.isText || !node.text) return;
              const text = node.text;
              // /command tokens (start of the text node or after whitespace)
              for (const m of text.matchAll(/(^|\s)(\/[A-Za-z0-9:_-]+)/g)) {
                const start = pos + (m.index ?? 0) + m[1].length;
                push(start, start + m[2].length, "text-lamp font-medium");
              }
              // `code` spans
              for (const m of text.matchAll(/`[^`\n]+`/g)) {
                const start = pos + (m.index ?? 0);
                push(start, start + m[0].length, "font-mono text-dawn");
              }
            });
            return DecorationSet.create(state.doc, decos);
          },
        },
      }),
    ];
  },
});

/**
 * The agent composer: a tiptap editor with `/` slash-command autocomplete
 * (commands + skills, seeded from the runner's server_info), `@` file mentions
 * (fs/suggest), and live highlighting. Enter sends
 * (Shift+Enter for a newline); sends always enqueue (the composer stays
 * enabled while streaming).
 * Code-split to the agents route (lazy-imported by AgentScreen).
 */
export function AgentComposer({
  onSend,
  server,
  sourcePath,
  streaming,
  waking,
  wakesOnSend,
  sending,
}: {
  onSend: (text: string) => Promise<void>;
  server: ServerCommands;
  sourcePath: string;
  /** a turn is streaming — sends queue behind it */
  streaming: boolean;
  /** the runtime is booting after a wake */
  waking?: boolean;
  /** cold agent: the first send wakes it */
  wakesOnSend?: boolean;
  sending: boolean;
}) {
  const [popup, setPopup] = useState<PopupState | null>(null);
  const [cursor, setCursor] = useState(0);
  const cursorRef = useRef(0);
  const popupRef = useRef<PopupState | null>(null);
  popupRef.current = popup;
  const setSel = (i: number) => {
    cursorRef.current = i;
    setCursor(i);
  };

  // Live context the suggestion `items` callbacks read (extensions are built
  // once; these refs keep the data fresh across renders).
  const serverRef = useRef(server);
  serverRef.current = server;
  const sourceRef = useRef(sourcePath);
  sourceRef.current = sourcePath;

  const onSendRef = useRef(onSend);
  onSendRef.current = onSend;

  const extensions = useMemo(() => {
    // A shared render() that drives the React popup. `kind` distinguishes the
    // two triggers so the popup can label rows.
    const makeRender = (kind: PopupState["kind"]) => () => ({
      onStart: (props: SuggestionProps<PopupItem>) => {
        setSel(0);
        setPopup({
          kind,
          items: props.items,
          command: (item) => props.command(item),
          rect: props.clientRect?.() ?? null,
        });
      },
      onUpdate: (props: SuggestionProps<PopupItem>) => {
        setSel(0);
        setPopup({
          kind,
          items: props.items,
          command: (item) => props.command(item),
          rect: props.clientRect?.() ?? null,
        });
      },
      onKeyDown: (props: SuggestionKeyDownProps) => {
        const p = popupRef.current;
        if (!p || p.items.length === 0) return false;
        if (props.event.key === "ArrowDown") {
          setSel((cursorRef.current + 1) % p.items.length);
          return true;
        }
        if (props.event.key === "ArrowUp") {
          setSel((cursorRef.current - 1 + p.items.length) % p.items.length);
          return true;
        }
        if (props.event.key === "Enter" || props.event.key === "Tab") {
          p.command(p.items[cursorRef.current]);
          return true;
        }
        if (props.event.key === "Escape") {
          setPopup(null);
          return true;
        }
        return false;
      },
      onExit: () => setPopup(null),
    });

    const slash = Extension.create({
      name: "slashCommands",
      addProseMirrorPlugins() {
        return [
          Suggestion<PopupItem>({
            editor: this.editor,
            char: "/",
            // Only trigger at the very start of a block (a leading slash line).
            allowSpaces: false,
            startOfLine: true,
            items: ({ query }): PopupItem[] => {
              const q = query.toLowerCase();
              const { commands, skills } = serverRef.current;
              return [...commands, ...skills]
                .filter((c) => c.name.toLowerCase().includes(q))
                .slice(0, 20)
                .map((c) => ({ value: `/${c.name}`, label: `/${c.name}`, hint: c.description }));
            },
            command: ({ editor, range, props }: { editor: Editor; range: Range; props: PopupItem }) => {
              // Insert the raw "/name " text — custom commands expand headlessly.
              editor.chain().focus().insertContentAt(range, `${props.value} `).run();
            },
            render: makeRender("slash"),
          }),
        ];
      },
    });

    return [
      StarterKit.configure({
        heading: false,
        blockquote: false,
        horizontalRule: false,
        bulletList: false,
        orderedList: false,
        listItem: false,
        codeBlock: false,
      }),
      HighlightExtension,
      slash,
      Mention.configure({
        HTMLAttributes: {
          class: "rounded-control bg-dawn/15 px-1 py-0.5 font-mono text-[12px] text-dawn",
        },
        renderText: ({ node }) => `@${node.attrs.id}`,
        suggestion: {
          char: "@",
          items: async ({ query }): Promise<PopupItem[]> => {
            const prefix = query.startsWith("/")
              ? query
              : `${sourceRef.current.replace(/\/$/, "")}/${query}`;
            try {
              const res = await fsApi.suggest(prefix, 12);
              return res.matches.slice(0, 12).map((m) => ({
                value: m,
                label: m.split("/").filter(Boolean).pop() ?? m,
                hint: m,
              }));
            } catch {
              return [];
            }
          },
          command: ({
            editor,
            range,
            props,
          }: {
            editor: Editor;
            range: Range;
            props: { value?: string; label?: string | null };
          }) => {
            const value = props.value ?? "";
            editor
              .chain()
              .focus()
              .insertContentAt(range, [
                { type: "mention", attrs: { id: value, label: props.label ?? value } },
                { type: "text", text: " " },
              ])
              .run();
          },
          render: makeRender("mention"),
        },
      }),
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const editor = useEditor({
    extensions,
    editorProps: {
      attributes: {
        class:
          "prose-none min-h-[64px] max-h-56 overflow-y-auto px-3 py-2.5 text-[13px] leading-relaxed text-moon-100 focus:outline-none",
        "aria-label": "Message",
      },
      handleKeyDown: (_view, event) => {
        // Enter sends (Cmd/Ctrl+Enter too); Shift+Enter inserts a newline.
        // Never send while a suggestion popup is capturing or mid-IME composition.
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !popupRef.current) {
          event.preventDefault();
          void doSend();
          return true;
        }
        return false;
      },
    },
  });

  const doSend = async () => {
    if (!editor) return;
    const text = editor.getText().trim();
    if (!text || sending) return;
    editor.commands.clearContent();
    editor.commands.focus();
    try {
      await onSendRef.current(text);
    } catch {
      // The caller toasts; restore the text so it isn't lost.
      editor.commands.setContent(text);
    }
  };

  const isEmpty = editor?.isEmpty ?? true;

  const hint = waking
    ? "Waking the agent…"
    : streaming
      ? "Agent is working — your message will queue"
      : wakesOnSend
        ? "Agent is cold — sending wakes it"
        : undefined;

  return (
    <div className="relative">
      {popup && popup.items.length > 0 && (
        <SuggestionPopup popup={popup} cursor={cursor} onPick={(i) => popup.command(popup.items[i])} />
      )}
      <div className="rounded-card border border-ink-700 bg-ink-950 focus-within:border-lamp">
        <EditorContent editor={editor} />
        <div className="flex items-center gap-2 border-t border-ink-700/60 px-2.5 py-1.5">
          <span className="flex items-center gap-1 text-[10px] text-moon-600">
            <Terminal size={11} /> /
            <span className="ml-1.5 flex items-center gap-1">
              <Hash size={11} /> @
            </span>
          </span>
          {hint && <span className="text-[11px] text-dawn">{hint}</span>}
          <span className="ml-auto hidden items-center gap-1 text-[10px] text-moon-600 sm:flex">
            <Kbd>↵</Kbd> send · <Kbd>⇧↵</Kbd> newline
          </span>
          <Button
            size="sm"
            variant="primary"
            leadingIcon={<ArrowUp size={13} />}
            loading={sending}
            disabled={isEmpty}
            onClick={doSend}
          >
            {streaming ? "Queue" : wakesOnSend ? "Wake & send" : "Send"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function SuggestionPopup({
  popup,
  cursor,
  onPick,
}: {
  popup: PopupState;
  cursor: number;
  onPick: (index: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // Keep the active row in view as the user arrows through.
  useEffect(() => {
    const el = ref.current?.querySelector<HTMLElement>(`[data-idx="${cursor}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  return (
    <div
      ref={ref}
      className="absolute bottom-full left-0 z-30 mb-2 max-h-56 w-80 overflow-auto rounded-card border border-ink-700 bg-ink-800 p-1 shadow-[var(--shadow-pop)]"
    >
      {popup.items.map((item, i) => (
        <button
          key={`${item.value}-${i}`}
          data-idx={i}
          type="button"
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(i);
          }}
          className={cn(
            "flex w-full items-start gap-2 rounded-control px-2 py-1.5 text-left",
            i === cursor ? "bg-ink-700 text-moon-100" : "text-moon-300 hover:bg-ink-700/60",
          )}
        >
          {popup.kind === "slash" ? (
            <Terminal size={12} className="mt-0.5 shrink-0 text-lamp" />
          ) : (
            <FileCode2 size={12} className="mt-0.5 shrink-0 text-dawn" />
          )}
          <span className="min-w-0 flex-1">
            <span className="block truncate font-mono text-[12px]">{item.label}</span>
            {item.hint && (
              <span className="block truncate text-[10px] text-moon-600">{item.hint}</span>
            )}
          </span>
        </button>
      ))}
    </div>
  );
}
