import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { TooltipProvider } from "@/ui/Tooltip";
import { Toaster } from "@/ui/Toast";
import { ConfirmHost } from "@/ui/confirm";
import { queryClient } from "@/lib/queryClient";
import { router } from "@/router";
import "@/styles/theme.css";

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("#root not found");

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={250} skipDelayDuration={300}>
        <RouterProvider router={router} />
        <Toaster />
        <ConfirmHost />
      </TooltipProvider>
    </QueryClientProvider>
  </StrictMode>,
);
