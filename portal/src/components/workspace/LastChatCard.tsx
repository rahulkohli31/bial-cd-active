/**
 * THE LAST CHAT, under the composer — the newest row of the application's chat list, with the way
 * to the whole list above it.
 *
 * Its own component because no existing card fits: the application cards carry a status and a
 * menu, and a chat has neither, only its kind, its title and its age.
 */
import type { Ref } from 'react'
import { useId } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { chatHistoryPath } from '../../utils/chatHistoryAddress'
import { relativeTime } from '../../utils/relativeTime'
import type { ChatRow } from '../../hooks/useProjectChats'
import { KindChip, chatName } from './chatHistoryColumns'

export interface LastChatCardProps {
  projectId: string
  chat: ChatRow
  viewAllRef?: Ref<HTMLAnchorElement>
}

export default function LastChatCard({ projectId, chat, viewAllRef }: LastChatCardProps) {
  const headingId = useId()
  const name = chatName(chat)
  return (
    <section aria-labelledby={headingId} data-testid="last-chat" className="px-[18px] pb-[18px] pt-3.5">
      <div className="mb-2.5 flex items-center">
        <h2 id={headingId} className="text-[10.5px] font-bold uppercase tracking-[.7px] text-primary-900">
          Last chat
        </h2>
        <Link
          ref={viewAllRef}
          to={chatHistoryPath(projectId)}
          className="ml-auto inline-flex items-center gap-[3px] text-xs font-semibold text-primary hover:underline"
        >
          View all
          <ChevronRight size={13} aria-hidden="true" />
        </Link>
      </div>
      <Link
        to={`/chat/${chat.id}`}
        data-testid="last-chat-card"
        className="flex items-center gap-[11px] rounded-xl border border-bial-border bg-white px-3 py-[11px] shadow-sm transition hover:bg-bial-bg/60"
      >
        <KindChip kind={chat.kind} />
        <span className="min-w-0 flex-1">
          <span title={name} className="block truncate text-[12.5px] font-semibold text-primary-900">
            {name}
          </span>
          <span className="mt-0.5 block text-[11px] text-neutral">{relativeTime(chat.updatedAt)}</span>
        </span>
        <ChevronRight size={14} aria-hidden="true" className="flex-shrink-0 text-slate-400" />
      </Link>
    </section>
  )
}
