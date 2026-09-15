import { clsx, type ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

/**
 * `max-w-thread` is the portal's own theme key (`tailwind.config.js`), and tailwind-merge only
 * ships knowledge of Tailwind's stock scale. Unregistered, it is not merely unknown — it is
 * outside every conflict group, so it can neither override a stock `max-w-*` nor be overridden by
 * one. Both survive the merge and the cascade decides, which is the one thing callers of `cn`
 * are entitled to assume cannot happen. Registering the key puts it back in the `max-w` group.
 */
const twMerge = extendTailwindMerge({
  extend: { classGroups: { 'max-w': [{ 'max-w': ['thread'] }] } },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
