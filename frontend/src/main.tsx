import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";

import { router } from "./router";
import { shouldRetryQuery } from "./lib/api";
import "./index.css";

// One deliberate retry policy for the whole app. The TanStack default (3
// retries with exponential backoff for everything) made deterministic refusals
// — 401/403/422 — look like flaky network calls and delayed the boundary state
// the user needs to see. See `shouldRetryQuery`.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: shouldRetryQuery,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
