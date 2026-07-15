import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { BialErrorCapture } from "@/components/bial/error-capture";
import { Toaster } from "@/components/ui/sonner";
import type { BialConfig } from "@/lib/bial-data";

// `next dev` renders dynamically, so process.env is read at REQUEST time. force-dynamic also
// keeps the runtime read correct if the app is ever `next build && next start` (harmless in dev).
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "BIAL App",
  description: "A BIAL citizen-developer app.",
};

/**
 * Runtime identity read server-side (C6 §4 / C9): the injected env-vars survive the supervisor
 * child-env scrub (D5). Handed to <BialErrorCapture/> which publishes them to window.__BIAL_CONFIG
 * for lib/bial-data.ts — so client components can fetch the data-service directly with X-App-Key
 * (an accepted, app_id-scoped credential exposure, C9 §5). The write-capable BIAL_BLOB_SAS is NOT
 * published here — it stays server-side (read from process.env in Route Handlers / Server Actions).
 */
function readBialConfig(): BialConfig {
  return {
    appId: process.env.BIAL_APP_ID ?? "",
    appKey: process.env.BIAL_APP_CREDENTIAL ?? "",
    baseUrl: process.env.BIAL_DATA_BASE_URL ?? "",
    portalOrigin: process.env.BIAL_PORTAL_ORIGIN ?? "",
  };
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        {/* Publishes window.__BIAL_CONFIG + installs window.onerror/unhandledrejection/console capture. */}
        <BialErrorCapture config={readBialConfig()} />
        {children}
        <Toaster />
      </body>
    </html>
  );
}
