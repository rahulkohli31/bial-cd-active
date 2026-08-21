/**
 * shadcn pagination primitives, hand-authored for THIS project rather than pulled by the CLI.
 *
 * Two adaptations from the upstream snippet, both load-bearing:
 *
 * 1. TAILWIND 3, NOT 4. The published variant uses v4's trailing-`!` important syntax
 *    (`border-primary!`, `bg-transparent!`). This project is on Tailwind ^3.4.17, where that
 *    is not a modifier at all — it would silently do nothing and the active page would look
 *    identical to the inactive ones. Written leading-`!` here.
 * 2. BUTTONS, NOT ANCHORS. Upstream renders `<a href='#'>`, which on a client-rendered list
 *    means every page click pushes a history entry and, under a router, can navigate. The
 *    catalog paginates in place, so these are real `<button>`s — which also gives keyboard
 *    users the semantics they expect and lets a disabled Previous/Next actually be disabled.
 *
 * Styling follows the repo's tokens (`bial-border`, `text-tertiary`, `text-neutral`,
 * `primary`) rather than shadcn's defaults, so the control sits in the page instead of on it.
 */
import { ChevronLeft, ChevronRight } from 'lucide-react'
import type React from 'react'

import { cn } from '../../lib/utils'

function Pagination({ className, ...props }: React.ComponentProps<'nav'>): React.JSX.Element {
  return (
    <nav
      // Named for assistive tech: several lists can share a page, and "pagination" alone
      // does not say which one this drives.
      aria-label="Marketplace pagination"
      className={cn('mx-auto flex w-full justify-center', className)}
      {...props}
    />
  )
}

function PaginationContent({
  className,
  ...props
}: React.ComponentProps<'ul'>): React.JSX.Element {
  return <ul className={cn('flex flex-row items-center gap-1', className)} {...props} />
}

function PaginationItem(props: React.ComponentProps<'li'>): React.JSX.Element {
  return <li {...props} />
}

interface PaginationLinkProps extends React.ComponentProps<'button'> {
  isActive?: boolean
}

function PaginationLink({
  className,
  isActive = false,
  ...props
}: PaginationLinkProps): React.JSX.Element {
  return (
    <button
      type="button"
      // `aria-current="page"` is what tells a screen reader which page it is on; the
      // underline is only the sighted half of that same fact.
      aria-current={isActive ? 'page' : undefined}
      className={cn(
        'inline-flex h-9 min-w-9 items-center justify-center rounded-none px-3 text-sm font-medium transition',
        'text-neutral hover:text-tertiary disabled:pointer-events-none disabled:opacity-40',
        // The underline treatment from the mockup. Leading `!` — Tailwind 3 (see the module
        // docstring); trailing `!` would be inert.
        isActive && '!border-primary border-0 border-b-2 !bg-transparent font-semibold text-tertiary !shadow-none',
        className,
      )}
      {...props}
    />
  )
}

function PaginationPrevious({
  className,
  ...props
}: React.ComponentProps<'button'>): React.JSX.Element {
  return (
    <PaginationLink
      aria-label="Go to previous page"
      className={cn('gap-1 pl-2.5', className)}
      {...props}
    >
      <ChevronLeft size={16} />
      <span>Previous</span>
    </PaginationLink>
  )
}

function PaginationNext({
  className,
  ...props
}: React.ComponentProps<'button'>): React.JSX.Element {
  return (
    <PaginationLink
      aria-label="Go to next page"
      className={cn('gap-1 pr-2.5', className)}
      {...props}
    >
      <span>Next</span>
      <ChevronRight size={16} />
    </PaginationLink>
  )
}

function PaginationEllipsis({
  className,
  ...props
}: React.ComponentProps<'span'>): React.JSX.Element {
  return (
    <span
      aria-hidden
      className={cn('flex h-9 w-9 items-center justify-center text-neutral', className)}
      {...props}
    >
      &hellip;
    </span>
  )
}

export {
  Pagination,
  PaginationContent,
  PaginationEllipsis,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
}
