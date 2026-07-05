import { Toaster as SonnerToaster, toast } from "sonner";

/** Toast host. Styled to the dusk-console tokens; sonner handles stacking,
 *  timing, and reduced-motion. Mount once near the app root. */
export function Toaster() {
  return (
    <SonnerToaster
      position="bottom-right"
      gap={8}
      offset={16}
      toastOptions={{
        classNames: {
          toast:
            "!bg-ink-800 !border !border-ink-700 !text-moon-100 !rounded-card !shadow-[var(--shadow-pop)] !font-sans !text-sm",
          title: "!text-moon-100 !font-medium",
          description: "!text-moon-400",
          actionButton: "!bg-lamp !text-ink-950 !rounded-control !font-semibold",
          cancelButton: "!bg-ink-700 !text-moon-100 !rounded-control",
          success: "!text-success",
          error: "!text-failed",
          closeButton: "!bg-ink-800 !border-ink-700 !text-moon-400",
        },
      }}
    />
  );
}

export { toast };
