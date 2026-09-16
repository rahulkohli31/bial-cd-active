import * as React from "react"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { X } from "lucide-react"

import { cn } from "@/lib/utils"

/**
 * shadcn/ui `dialog`, copied from the registry with the alias set trimmed to the five names
 * this portal mounts — recorded here because the next `npx shadcn@latest add` listing
 * `dialog` in `registryDependencies` restores the rest as an unexplained diff, the way
 * `button.tsx` beside it records its own departures.
 *
 * Trigger, close alias, and footer were vendored whole, never reached — both consumers drive
 * the dialog from `open`/`onOpenChange` state and dismiss via the corner control
 * `DialogContent` renders. Portal/overlay stay, only unexported (`DialogContent` composes
 * both). A future consumer IS expected: `UnsavedWorkGuard.tsx` names this as the upgrade
 * path for its focus trap — re-add a trigger together with that caller, not on spec.
 */

const Dialog = DialogPrimitive.Root

const DialogPortal = DialogPrimitive.Portal

const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-50 bg-black/80  data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
      className
    )}
    {...props}
  />
))
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName

/**
 * TWO ADDITIONS TO THE UPSTREAM SHAPE, both because upstream renders the overlay and close
 * button internally where a caller cannot reach them: `overlayClassName` lets the design's
 * softened overlay (`bg-slate-900/15`, 3px blur) override upstream's `bg-black/80` without
 * editing the vendored default (which would change every dialog in the product), and
 * `hideClose` skips Radix's own X where a dialog already has its own Cancel and X. Everything
 * else is upstream verbatim — `role="dialog"`, `aria-modal`, a focus trap, Escape, scroll lock.
 */
/**
 * WHERE FOCUS GOES WHEN A DIALOG IS UNMOUNTED RATHER THAN CLOSED.
 *
 * Radix restores focus to whatever was focused before the content mounted — but the restore runs
 * inside `FocusScope`'s own cleanup, and every dialog in this portal is rendered CONDITIONALLY:
 * `{deleting && <ProjectDeleteDialog …/>}`, `{settingsFor && <AppSettingsDialog …/>}`. Pressing
 * Escape calls `onOpenChange(false)`, the parent sets its state to `null`, and React deletes the
 * whole subtree — `Dialog`, `FocusScope` and all — in the same commit. There is no closing state
 * for the restore to run in, so it never runs.
 *
 * MEASURED, NOT REASONED. In the portal container, opening a dialog from a row control and
 * pressing Escape left `document.activeElement === document.body` while the control that opened
 * it was still the same node, still connected and still focusable. A keyboard is then at the top of the document
 * with no idea where it came from — which is the exact strand `ProjectsPage`'s delete already
 * records and fixes by hand, and the reason `aria-disabled` is used everywhere in place of
 * `disabled` (a disabled control throws focus to the body for the same end result).
 *
 * IT IS A BACKSTOP, NOT A POLICY. It only fires when focus ended up NOWHERE — on the body, or
 * unset — and only on the next frame, so a caller that deliberately moves focus somewhere else
 * still wins: `ProjectsPage` sends focus to its heading because the row Radix captured is gone by
 * then, and this must not steal it back to a detached trigger. Fixed here rather than at five call
 * sites because it is one mechanism, and the sixth dialog would be written without it.
 */
function useFocusBackstop() {
  const openerRef = React.useRef<HTMLElement | null>(null)
  React.useEffect(() => {
    // Captured on mount, BEFORE the dialog's own autofocus moves it.
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    return () => {
      const opener = openerRef.current
      if (!opener) return
      // Next frame: React has finished detaching the dialog and any deliberate restore has run.
      requestAnimationFrame(() => {
        const landed = document.activeElement
        if (!opener.isConnected) return
        if (landed === null || landed === document.body) opener.focus()
      })
    }
  }, [])
}

const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
    overlayClassName?: string
    hideClose?: boolean
  }
>(({ className, children, overlayClassName, hideClose, ...props }, ref) => {
  useFocusBackstop()
  return (
  <DialogPortal>
    <DialogOverlay className={overlayClassName} />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        "fixed left-[50%] top-[50%] z-50 grid w-full max-w-lg translate-x-[-50%] translate-y-[-50%] gap-4 border bg-background p-6 shadow-lg duration-200 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 data-[state=closed]:slide-out-to-left-1/2 data-[state=closed]:slide-out-to-top-[48%] data-[state=open]:slide-in-from-left-1/2 data-[state=open]:slide-in-from-top-[48%] sm:rounded-lg",
        className
      )}
      {...props}
    >
      {children}
      {!hideClose && (
        <DialogPrimitive.Close className="absolute right-4 top-4 rounded-sm opacity-70 ring-offset-background transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:pointer-events-none data-[state=open]:bg-muted data-[state=open]:text-muted-foreground">
          <X className="h-4 w-4" />
          <span className="sr-only">Close</span>
        </DialogPrimitive.Close>
      )}
    </DialogPrimitive.Content>
  </DialogPortal>
  )
})
DialogContent.displayName = DialogPrimitive.Content.displayName

const DialogHeader = ({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn(
      "flex flex-col space-y-1.5 text-center sm:text-left",
      className
    )}
    {...props}
  />
)
DialogHeader.displayName = "DialogHeader"

const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn(
      "text-lg font-semibold leading-none tracking-tight",
      className
    )}
    {...props}
  />
))
DialogTitle.displayName = DialogPrimitive.Title.displayName

const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn("text-sm text-muted-foreground", className)}
    {...props}
  />
))
DialogDescription.displayName = DialogPrimitive.Description.displayName

export {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
}
