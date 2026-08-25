import { useState, useRef, useCallback, useEffect, useMemo } from 'react'
import type { ChangeEvent, DragEvent as ReactDragEvent } from 'react'
import { validateAttachmentFiles, fileToBase64, newAttachmentId, resolveMediaType, textAttachmentBytes, MAX_FILES_PER_MESSAGE } from '../utils/attachmentInput'
import type { PendingAttachment } from '../utils/attachmentInput'

/** The four drag handlers a composer spreads onto its drop target. Kept as one object so a
 *  consumer cannot wire three of the four and silently break the depth count. */
export interface ComposerDragHandlers {
  onDragEnter: (e: ReactDragEvent<HTMLElement>) => void
  onDragOver: (e: ReactDragEvent<HTMLElement>) => void
  onDragLeave: (e: ReactDragEvent<HTMLElement>) => void
  onDrop: (e: ReactDragEvent<HTMLElement>) => void
}

/** How long the drop highlight survives with no `dragover` before it is treated as stranded.
 *  Comfortably above the spec's 350ms drag-and-drop loop interval, so a user holding still
 *  over the composer can never blink it off, while a genuinely stuck highlight self-clears
 *  fast enough to read as responsive rather than broken. */
const DRAG_IDLE_MS = 1000

export interface UsePendingAttachmentsResult {
  pendingAttachments: PendingAttachment[]
  handleFiles: (incoming: File[]) => Promise<void>
  handleFileSelect: (e: ChangeEvent<HTMLInputElement>) => Promise<void>
  removePending: (id: string) => void
  /** Clears for a CHAT SWITCH — also supersedes any in-flight read, whose bytes belong to
   *  the chat being left. Use `clearPendingAfterSend` on the send path instead. */
  clearPending: () => void
  /** Clears for a SEND — leaves in-flight reads alone so they land in the now-empty
   *  composer and stage for the next message rather than vanishing silently. */
  clearPendingAfterSend: () => void
  /** Puts a previously-cleared batch back (a failed send that already cleared the
   *  composer) — MERGES with whatever is currently staged (deduped by id), since the
   *  composer stays live during the failing send and the user may have attached
   *  something new in the meantime. Clamped to the per-message cap; the RESTORED batch is
   *  what gets truncated, and a toast fires when it does. */
  restorePending: (items: PendingAttachment[]) => void
  attachToast: string | null
  showAttachToast: (msg: string) => void
  /** True while a FILE drag is over the composer — drives the drop-target highlight. */
  draggingFiles: boolean
  dragHandlers: ComposerDragHandlers
}

/**
 * Shared composer state for image/PDF attachments (used by ChatPage and
 * BuilderPage). Owns the pending-attachment list (each item carries transient
 * base64 until the message is sent) and a short-lived validation/cap toast.
 * Each page renders the toast + preview row in its own style; the behaviour
 * lives here so the two composers can't drift.
 */
export function usePendingAttachments(): UsePendingAttachmentsResult {
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([])
  const [attachToast, setAttachToast] = useState<string | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => {
    if (toastTimer.current) clearTimeout(toastTimer.current)
  }, [])

  const showAttachToast = useCallback((msg: string) => {
    setAttachToast(msg)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setAttachToast(null), 3500)
  }, [])

  // Bumped by clearPending() — the CHAT-SWITCH clear, and only that one. A read started
  // against the OLD chat that resolves after the switch has nowhere honest to land, so
  // handleFiles checks this hasn't moved before committing. Without it, a file dropped just
  // before navigating to another chat could land its bytes in the NEW chat's composer once
  // the FileReader finally resolves.
  //
  // Deliberately NOT bumped by clearPendingAfterSend(): a send stays in the same chat, so a
  // read still in flight when the user hits Enter has somewhere perfectly honest to land —
  // the now-empty composer, staged for their next message. Discarding it there (which is
  // what a shared clear did) drops a large PDF silently, with no chip, no toast and no log,
  // leaving the user believing it was attached when the model never saw it.
  const generationRef = useRef(0)

  // Mirrors pendingAttachments, but updated SYNCHRONOUSLY by every mutator below instead of
  // via an effect. Two concurrent handleFiles calls can both resolve in the same tick, and
  // reading `pendingAttachments` (or a variable assigned inside the setState updater) to
  // compute the per-message cap is unreliable there: React only eagerly re-runs an updater
  // function for the FIRST call to a given setter within a batch, so the second call's
  // overflow computation would silently see stale state. Every mutator reads this ref,
  // computes the next array, writes it back here, THEN calls setPendingAttachments with the
  // plain array — so correctness never depends on batching order.
  const pendingRef = useRef<PendingAttachment[]>([])

  // Shared by the file-input picker AND drag-and-drop — one validation/read path so
  // the two entry points can't drift.
  const handleFiles = useCallback(
    async (incoming: File[]) => {
      if (incoming.length === 0) return
      // Pass the bytes of text files already pending so the text budget is
      // enforced cumulatively across picks, not reset per selection.
      const result = validateAttachmentFiles(incoming, pendingAttachments.length, textAttachmentBytes(pendingAttachments))
      if ('error' in result) {
        showAttachToast(result.error)
        return
      }
      const generation = generationRef.current
      // allSettled, not all: one unreadable file (a mid-read permission error, a corrupt
      // blob) must not discard the rest of an otherwise-good batch behind one generic toast.
      const settled = await Promise.allSettled(
        incoming.map(async (file): Promise<PendingAttachment> => ({
          id: newAttachmentId(),
          name: file.name,
          // Resolve so an OS-mislabeled CSV stores its canonical text/csv type
          // — the same type the validator allowed it under (Decision 3).
          mediaType: resolveMediaType(file),
          size: file.size,
          base64: await fileToBase64(file),
        })),
      )
      if (generationRef.current !== generation) return // superseded by a chat switch mid-read

      const read: PendingAttachment[] = []
      const failedNames: string[] = []
      settled.forEach((r, i) => (r.status === 'fulfilled' ? read.push(r.value) : failedNames.push(incoming[i].name)))

      if (read.length > 0) {
        // Computed from pendingRef, not from React state or an updater's `prev` — see the
        // ref's own comment. Re-checked here, not just by the upfront validate above: two
        // concurrent drops (e.g. two 3-file drops in the same async window) both validate
        // against the same pre-drop count and would otherwise both pass even though their
        // COMBINED total exceeds the per-message cap.
        const room = Math.max(0, MAX_FILES_PER_MESSAGE - pendingRef.current.length)
        const overflow = Math.max(0, read.length - room)
        const accepted = read.slice(0, room)
        if (accepted.length > 0) {
          pendingRef.current = [...pendingRef.current, ...accepted]
          setPendingAttachments(pendingRef.current)
        }
        if (overflow > 0) showAttachToast(`You can attach at most ${MAX_FILES_PER_MESSAGE} files per message.`)
      }
      if (failedNames.length > 0) {
        showAttachToast(
          failedNames.length === 1
            ? `Could not read "${failedNames[0]}".`
            : `Could not read ${failedNames.length} of the selected files.`,
        )
      }
    },
    [pendingAttachments, showAttachToast],
  )

  const handleFileSelect = useCallback(
    async (e: ChangeEvent<HTMLInputElement>) => {
      const incoming = Array.from(e.target.files || [])
      e.target.value = '' // allow re-selecting the same file later
      await handleFiles(incoming)
    },
    [handleFiles],
  )

  const removePending = useCallback((id: string) => {
    pendingRef.current = pendingRef.current.filter((a) => a.id !== id)
    setPendingAttachments(pendingRef.current)
  }, [])

  /** The CHAT-SWITCH clear: empties the composer AND supersedes any read still in flight,
   *  because those bytes belong to the chat being left. */
  const clearPending = useCallback(() => {
    generationRef.current += 1
    pendingRef.current = []
    setPendingAttachments([])
  }, [])

  /** The SEND clear: empties the composer but leaves the generation alone, so a read that
   *  was still in flight when the message went out lands in the now-empty composer and is
   *  staged for the next one — rather than vanishing with no feedback. */
  const clearPendingAfterSend = useCallback(() => {
    pendingRef.current = []
    setPendingAttachments([])
  }, [])

  const restorePending = useCallback((items: PendingAttachment[]) => {
    // Merge rather than replace: the composer stays live while the send that's being
    // restored-from was failing, so the user may already have staged something new — a
    // plain replace would discard it.
    const seen = new Set(pendingRef.current.map((a) => a.id))
    const fresh = items.filter((a) => !seen.has(a.id))
    // Clamped to the SAME per-message cap handleFiles re-checks at commit time. Without it
    // the merge is an unbounded append: the composer stays live through the failing send
    // (no spinner — ChatPage sets `generating` only after the upload resolves), so the user
    // can stage a second full batch that validated against an empty list, and the restore
    // then stacks the original batch on top. That compounds across repeated failures
    // (5 → 10 → 15), and at the per-user storage cap EVERY upload fails, so it's the steady
    // state rather than a one-off. `validateAttachmentFiles` is the sole enforcement point
    // for this cap and never runs again on the send path, so an over-cap list sends as-is.
    const room = Math.max(0, MAX_FILES_PER_MESSAGE - pendingRef.current.length)
    // Truncate the RESTORED batch, not what the user just staged: they can still see and
    // re-pick the files they chose a moment ago, but have no idea what the restore dropped.
    if (fresh.length > room) showAttachToast(`You can attach at most ${MAX_FILES_PER_MESSAGE} files per message.`)
    pendingRef.current = [...pendingRef.current, ...fresh.slice(0, room)]
    setPendingAttachments(pendingRef.current)
  }, [showAttachToast])

  // ── Drop-target feedback ────────────────────────────────────────────────────────────
  // Without it the composer advertises "drop them anywhere in the composer" (the attach
  // button's own title) while showing nothing that says WHERE — and a drop landing a few
  // pixels outside the target falls through to the browser's default handler, which
  // navigates the tab to the file. That reads as "the feature is broken" rather than "you
  // missed", so the target has to be visible while a drag is in flight.
  //
  // Depth-counted, not a bare boolean: dragenter/dragleave fire for EVERY descendant the
  // pointer crosses (the textarea, the attach button, the send button). Entering a child
  // fires that child's dragenter BEFORE the parent's dragleave for the element just left,
  // so a bare boolean would flicker the highlight off mid-drag. Incrementing on enter and
  // decrementing on leave means only the leave that returns the depth to 0 exits the target.
  const [dragDepth, setDragDepth] = useState(0)
  const draggingFiles = dragDepth > 0

  // A drag can end WITHOUT a drop or a balancing dragleave ever reaching the target: Escape
  // cancels it, it drops somewhere else on the page, or the pointer leaves the browser window
  // — and in Firefox that last case delivers no dragleave AT ALL (Mozilla bug 656164). Any of
  // them would strand the depth above zero and pin the highlight on for the rest of the
  // session, so the window is the backstop.
  //
  // The heartbeat, not `dragleave`, is what makes this reliable. Listening for a window-level
  // dragleave cannot fix the Firefox case (the whole bug is that none arrives) and is
  // ambiguous anyway: dragleave bubbles, so a leave from our own children reaches the window
  // too and its relatedTarget is not dependably set. `dragover`, by contrast, is guaranteed —
  // the HTML drag-and-drop processing model re-fires it on the current target at least every
  // 350ms for as long as a drag is live over the document. So its ABSENCE is the signal: no
  // dragover for IDLE_MS means the drag is gone, whatever ended it. `drop`/`dragend` still
  // clear immediately where they do fire, so the timeout is only ever the fallback.
  useEffect(() => {
    if (!draggingFiles) return undefined
    const clear = () => setDragDepth(0)
    let idle = setTimeout(clear, DRAG_IDLE_MS)
    const beat = () => {
      clearTimeout(idle)
      idle = setTimeout(clear, DRAG_IDLE_MS)
    }
    window.addEventListener('dragover', beat)
    window.addEventListener('dragend', clear)
    window.addEventListener('drop', clear)
    return () => {
      clearTimeout(idle)
      window.removeEventListener('dragover', beat)
      window.removeEventListener('dragend', clear)
      window.removeEventListener('drop', clear)
    }
  }, [draggingFiles])

  const dragHandlers = useMemo<ComposerDragHandlers>(() => {
    // `types` (not `files`) — the spec exposes no file LIST during dragover/dragenter, only
    // the kinds being carried, so this is the one signal available before the drop. Every
    // handler below gates on it, so a non-file drag (selected text, a link) is left ENTIRELY
    // alone: no highlight, no preventDefault, and therefore no drop event on us — it falls
    // through to the browser's native handling instead of being silently swallowed.
    const carriesFiles = (e: ReactDragEvent<HTMLElement>) =>
      e.dataTransfer?.types.includes('Files') ?? false

    return {
      onDragEnter: (e) => {
        if (!carriesFiles(e)) return
        e.preventDefault()
        setDragDepth((d) => d + 1)
      },
      // preventDefault on dragover is what makes this a valid drop target at all; withholding
      // it for non-file drags is precisely what hands them back to the browser.
      onDragOver: (e) => {
        if (!carriesFiles(e)) return
        e.preventDefault()
      },
      onDragLeave: (e) => {
        if (!carriesFiles(e)) return
        setDragDepth((d) => Math.max(0, d - 1))
      },
      onDrop: (e) => {
        if (!carriesFiles(e)) return
        e.preventDefault()
        // Reset to 0 rather than decrement: the drop consumes the drag outright, so the
        // matching dragleave never arrives and a decrement would strand the count above
        // zero, leaving the highlight stuck on until the next full enter/leave cycle.
        setDragDepth(0)
        void handleFiles(Array.from(e.dataTransfer?.files ?? []))
      },
    }
  }, [handleFiles])

  return {
    pendingAttachments,
    handleFiles,
    handleFileSelect,
    removePending,
    clearPending,
    clearPendingAfterSend,
    restorePending,
    attachToast,
    showAttachToast,
    draggingFiles,
    dragHandlers,
  }
}
