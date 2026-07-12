/**
 * The project a chat belongs to, shown as a breadcrumb.
 *
 * Chats are addressed flat at `/chat/{chatId}` — the project is never a path segment.
 * This link is therefore the ONLY affordance that takes a user from a chat back to its
 * project, which makes it load-bearing navigation, not decoration.
 *
 * `projectName` resolves asynchronously (and stays `null` when the project was deleted
 * out from under an open chat), so the label degrades to a neutral "Back to project"
 * rather than flashing an empty string or blocking the transcript from rendering.
 */
import { Link } from 'react-router-dom'
import { ChevronLeft } from 'lucide-react'

export interface ProjectBreadcrumbProps {
  projectId: string | null
  projectName: string | null
}

export default function ProjectBreadcrumb({ projectId, projectName }: ProjectBreadcrumbProps) {
  // A chat rendered without a resolved project (a direct render in a test, or a chat
  // whose project vanished) still shows its transcript — it just has nowhere to go up to.
  if (!projectId) return null

  return (
    <Link
      to={`/projects/${projectId}`}
      className="inline-flex items-center gap-1 text-xs text-neutral hover:text-primary transition min-w-0"
    >
      <ChevronLeft size={13} className="flex-shrink-0" />
      <span className="truncate">{projectName ?? 'Back to project'}</span>
    </Link>
  )
}
