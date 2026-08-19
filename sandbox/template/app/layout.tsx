import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { BialErrorCapture } from "@/components/bial/error-capture";
import { Toaster } from "@/components/ui/sonner";
import type { BialConfig } from "@/lib/bial-config";
import { BialIdentityProvider } from "@/lib/bial-identity-client";

// `next dev` renders dynamically, so process.env is read at REQUEST time. force-dynamic also
// keeps the runtime read correct if the app is ever `next build && next start` (harmless in dev).
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "BIAL App",
  description: "A BIAL citizen-developer app.",
};

/**
 * Runtime identity read server-side (C6 §4 / C9): the injected env-vars survive the supervisor
 * child-env scrub (D5). Handed to <BialErrorCapture/>, which publishes them to
 * window.__BIAL_CONFIG so the error relay knows which origin to post to.
 *
 * ONLY non-secret labels are published. The app's data credentials — BIAL_DATABASE_URL and the
 * write-capable BIAL_BLOB_SAS — stay server-side (read from process.env in Route Handlers,
 * Server Actions, and db/index.ts) and must never appear in this object.
 */
function readBialConfig(): BialConfig {
  return {
    appId: process.env.BIAL_APP_ID ?? "",
    portalOrigin: process.env.BIAL_PORTAL_ORIGIN ?? "",
  };
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        {/* Publishes window.__BIAL_CONFIG + installs window.onerror/unhandledrejection/console capture. */}
        <BialErrorCapture config={readBialConfig()} />
        {/* Issue #92: the preview-plane identity handshake. A no-op when not framed. */}
        <BialIdentityProvider>{children}</BialIdentityProvider>
        <Toaster />
      </body>
    </html>
  );
}
