import * as React from "react"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { X } from "lucide-react"

import { cn } from "@/lib/utils"

/**
 * shadcn/ui `dialog`, copied in from the registry, with the alias set trimmed to the five names
 * this portal actually mounts — recorded here because the next `npx shadcn@latest add` of
 * anything listing `dialog` in its registryDependencies will restore the rest as an unexplained
 * diff, exactly the way `button.tsx` beside it records its own departures.
 *
 * WHAT WENT AND WHY IT WAS NOT AN OVERSIGHT. The trigger, the close alias and the footer were
 * vendored whole and never reached. Both consumers — `chat/AttachmentPreview.tsx` and
 * `__tests__/test-setup.test.tsx` — drive the dialog from `open`/`onOpenChange` state rather than
 * from a trigger, and dismiss it through the corner control `DialogContent` already renders, so a
 * footer had nothing to hold.
 *
 * THE PORTAL AND THE OVERLAY ARE STILL HERE, only unexported: `DialogContent` composes both, and
 * deleting either would take the backdrop and the layer with it.
 *
 * A FUTURE CONSUMER IS EXPECTED. `workspace/UnsavedWorkGuard.tsx` names this file as the upgrade
 * path for its focus trap. Re-adding a trigger together with a caller is the right move; re-adding
 * one on spec is what this note exists to stop.
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
 * TWO ADDITIONS TO THE UPSTREAM SHAPE, both because this design needs them at the call site
 * and upstream renders the overlay and the close button internally where a caller cannot
 * reach them:
 *
 *   `overlayClassName` — #158 §9 specifies a SOFTENED overlay (`bg-slate-900/15` with a 3px
 *     backdrop blur) rather than upstream's `bg-black/80`, and §12 names overriding it as the
 *     expected thing to do. Without this prop the only way to get there is editing the
 *     vendored default, which would change every other dialog in the product.
 *
 *   `hideClose` — the project dialogs carry their own Cancel and their own X. Rendering
 *     Radix's as well gives two close affordances in one corner.
 *
 * Everything else is upstream verbatim, including the whole point of moving onto it:
 * `role="dialog"`, `aria-modal`, a focus trap, Escape-to-close and scroll lock.
 */
const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
    overlayClassName?: string
    hideClose?: boolean
  }
>(({ className, children, overlayClassName, hideClose, ...props }, ref) => (
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
))
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
