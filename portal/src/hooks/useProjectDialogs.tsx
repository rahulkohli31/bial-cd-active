/**
 * An application's Settings and Share dialogs, for a workspace surface that holds the project.
 *
 * The toolbar's `⋯` menu sits above the outlet and has no project object, so the surface publishes
 * `settings` and `share` upward and renders `dialogs` itself. Both doors are `null` until the
 * project has loaded: a menu item with nothing behind it would be a press that does nothing.
 */
import { useCallback, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import AppSettingsDialog from '../components/projects/AppSettingsDialog'
import ProjectDeleteDialog from '../components/projects/ProjectDeleteDialog'
import SharePanel from '../components/projects/SharePanel'
import { deleteProject } from '../utils/projectApi'
import type { Project } from '../utils/projectApi'
import { projectsListHref } from '../utils/projectsListMemory'

export interface ProjectDialogs {
  settings: (() => void) | null
  share: (() => void) | null
  dialogs: ReactNode
}

export function useProjectDialogs(
  project: Project | null,
  onProjectUpdate: (project: Project) => void,
): ProjectDialogs {
  const [open, setOpen] = useState<'settings' | 'delete' | 'share' | null>(null)
  const navigate = useNavigate()
  const openSettings = useCallback(() => setOpen('settings'), [])
  const openShare = useCallback(() => setOpen('share'), [])
  const close = useCallback(() => setOpen(null), [])

  if (project === null) return { settings: null, share: null, dialogs: null }

  return {
    settings: openSettings,
    share: openShare,
    dialogs: (
      <>
        {open === 'settings' && (
          <AppSettingsDialog
            project={project}
            onProjectUpdate={onProjectUpdate}
            onClose={close}
            // Settings gives way to the confirmation rather than staying open behind a dialog
            // about deleting the very thing it describes.
            onDelete={() => setOpen('delete')}
          />
        )}
        {open === 'delete' && (
          <ProjectDeleteDialog
            project={project}
            onClose={close}
            onConfirm={async (remark) => {
              await deleteProject(project.id, remark)
              // The list, not back: back is an address inside the application just deleted.
              navigate(projectsListHref(), { replace: true })
            }}
          />
        )}
        {open === 'share' && <SharePanel projectId={project.id} projectName={project.name} onClose={close} />}
      </>
    ),
  }
}
