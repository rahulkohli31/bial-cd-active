"use client";

import type { CSSProperties } from "react";
import { Toaster as Sonner, type ToasterProps } from "sonner";

// Toast host. Kept theme-agnostic (no next-themes dependency) — it reads the app's own
// CSS-variable design tokens so it matches light/dark automatically.
function Toaster(props: ToasterProps) {
  return (
    <Sonner
      className="toaster group"
      style={
        {
          "--normal-bg": "var(--popover)",
          "--normal-text": "var(--popover-foreground)",
          "--normal-border": "var(--border)",
        } as CSSProperties
      }
      {...props}
    />
  );
}

export { Toaster };
