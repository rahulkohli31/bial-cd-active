"use client";

/**
 * BialErrorCapture — the C7 client-error-capture injection point (C6 §5), and the runtime-config
 * bootstrap.
 *
 * Imported FIRST in app/layout.tsx (above the app tree) so it installs before any feature code.
 * Two jobs:
 *   1. Publish the injected runtime identity to `window.__BIAL_CONFIG` (see lib/bial-config.ts).
 *      The `config` prop is read server-side from process.env by the layout (the BIAL_* env-vars
 *      survive the supervisor child-env scrub — D5) and handed across the RSC boundary; we set
 *      the global during the RENDER phase (not an effect) so it is in place before any CHILD
 *      effect runs — React commits effects child-first but runs renders parent-first, so a
 *      render-phase assignment here always beats a screen's initial fetch effect. Only non-secret
 *      labels travel this way: the app's data now lives in its own database, reached with Drizzle
 *      from SERVER code (db/index.ts), so the browser holds no data credential at all.
 *   2. Capture window `error`, `unhandledrejection`, and console.error/warn, relaying each to the
 *      parent frame via postMessage with an EXPLICIT targetOrigin (the portal origin — NEVER '*';
 *      C8 §3). Portal origin comes from the injected config (falling back to document.referrer's
 *      origin); if neither is known we DO NOT post (fail closed).
 *
 * THE RECEIVING LEGS ARE ALL BUILT NOW (U13). This block said "the capture side is authored now
 * but consumed by NOBODY … Wave-1 additions", which stopped being true when the ingest shipped
 * and would tell a reader that deleting the relay costs nothing. The whole chain:
 *   1. this shim posts `bial:client-error` to the framing parent;
 *   2. `portal/src/utils/clientErrorRelay.ts` validates the origin and POSTs it to
 *      `/api/build-sessions/projects/{projectId}/client-error` (mounted on the portal frame by
 *      `components/chat/ConversationSurface.tsx`);
 *   3. the control plane serves that at `POST /v1/build-sessions/projects/{project_id}/client-error`
 *      and parks the report against the app (`services/orchestrator/client_errors.park_client_error`);
 *   4. the next verify drains it into the self-heal diagnostic
 *      (`selfheal.the_call_is_coming_from_inside_the_house`) — the class of failure where every
 *      server-side check is green and the app is dead in the browser anyway.
 * Emit; a reply is still not part of the contract — the report travels one way.
 */

import { useEffect } from "react";
import type { BialConfig } from "@/lib/bial-config";

type ClientError = {
  type: "bial:client-error";
  source: "window.onerror" | "unhandledrejection" | "console.error" | "console.warn";
  title: string;
  stack: string;
  ts: number;
};

function resolvePortalOrigin(): string | null {
  const injected = typeof window !== "undefined" ? window.__BIAL_CONFIG?.portalOrigin : undefined;
  if (injected) return injected;
  // Fall back to the framing parent's origin (the portal) when config has not landed yet.
  if (typeof document !== "undefined" && document.referrer) {
    try {
      return new URL(document.referrer).origin;
    } catch {
      return null;
    }
  }
  return null;
}

function relay(payload: Omit<ClientError, "type" | "ts">): void {
  if (typeof window === "undefined" || window.parent === window) return; // not framed → no relay target
  const targetOrigin = resolvePortalOrigin();
  if (!targetOrigin) return; // fail closed — never post to '*'
  const message: ClientError = { type: "bial:client-error", ts: Date.now(), ...payload };
  window.parent.postMessage(message, targetOrigin);
}

export function BialErrorCapture({ config }: { config: BialConfig }) {
  // Render-phase, idempotent global publish — see the header note on effect ordering.
  if (typeof window !== "undefined" && !window.__BIAL_CONFIG) {
    window.__BIAL_CONFIG = config;
  }

  useEffect(() => {
    const onError = (e: ErrorEvent) => {
      relay({
        source: "window.onerror",
        title: e.message || "Uncaught error",
        stack: e.error instanceof Error ? (e.error.stack ?? "") : String(e.error ?? ""),
      });
    };

    const onRejection = (e: PromiseRejectionEvent) => {
      const reason = e.reason;
      relay({
        source: "unhandledrejection",
        title: reason instanceof Error ? reason.message : "Unhandled promise rejection",
        stack: reason instanceof Error ? (reason.stack ?? "") : String(reason ?? ""),
      });
    };

    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);

    const origError = console.error;
    const origWarn = console.warn;
    console.error = (...args: unknown[]) => {
      relay({ source: "console.error", title: args.map(String).join(" ").slice(0, 500), stack: "" });
      origError.apply(console, args);
    };
    console.warn = (...args: unknown[]) => {
      relay({ source: "console.warn", title: args.map(String).join(" ").slice(0, 500), stack: "" });
      origWarn.apply(console, args);
    };

    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
      console.error = origError;
      console.warn = origWarn;
    };
  }, []);

  return null;
}
