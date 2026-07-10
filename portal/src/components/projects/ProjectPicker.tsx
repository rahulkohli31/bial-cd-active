/**
 * Select-or-create a project. This is the single gate in front of every create
 * affordance that does not already know its project — Sandbox's "Generate App" and
 * Workspace's "Plan with AI".
 *
 * It exists because there is no Default project and no implicit container: a chat and
 * an app are always filed under a project the user named. The server enforces this
 * (`header.projectId` is required on the create branch, absent → 400); the picker is
 * what makes that requirement something the user answers rather than something they
 * hit as an error.
 *
 * Creation reuses `ProjectCreateModal` rather than growing a second create form.
 */
import { useCallback, useEffect, useState } from 'react'
import { Boxes, Loader2, Plus } from 'lucide-react'
import { listProjects } from '../../utils/projectApi'
import type { Project } from '../../utils/projectApi'
import ProjectCreateModal from './ProjectCreateModal'

export interface ProjectPickerProps {
  /** Copy for what the user is about to do once a project is chosen. */
  title?: string
  onClose: () => void
  onPick: (project: Project) => void
}

export default function ProjectPicker({ title = 'Choose a project', onClose, onPick }: ProjectPickerProps) {
  const [projects, setProjects] = useState<Project[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  const load = useCallback(async (): Promise<void> => {
    try {
      // One page is the right depth for a picker; a user with more projects than this
      // searches from /projects and starts the chat from the project home instead.
      const page = await listProjects({ limit: 50 })
      setProjects(page.items)
      setError(null)
    } catch (caught) {
      setProjects([])
      setError(caught instanceof Error ? caught.message : 'Could not load your projects.')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const confirm = (): void => {
    const picked = projects?.find((p) => p.id === selectedId)
    if (picked) onPick(picked)
  }

  if (creating) {
    return (
      <ProjectCreateModal
        onClose={() => setCreating(false)}
        onCreated={(project) => onPick(project)}
      />
    )
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
        <h3 className="text-base font-bold text-tertiary">{title}</h3>
        <p className="text-sm text-neutral mt-0.5">
          Every chat and every app lives in a project. Pick one, or start a new one.
        </p>

        <div className="mt-4 max-h-64 overflow-y-auto scrollbar-thin -mx-1 px-1">
          {projects === null ? (
            <div className="flex items-center justify-center gap-2 py-10 text-sm text-neutral">
              <Loader2 size={15} className="animate-spin" /> Loading projects…
            </div>
          ) : error !== null ? (
            <p role="alert" className="py-8 text-center text-xs text-danger">
              {error}
            </p>
          ) : projects.length === 0 ? (
            <p className="py-8 text-center text-sm text-neutral">
              You don’t have a project yet. Create one to get started.
            </p>
          ) : (
            <div className="space-y-1.5">
              {projects.map((project) => {
                const active = project.id === selectedId
                return (
                  <button
                    key={project.id}
                    type="button"
                    onClick={() => setSelectedId(project.id)}
                    aria-pressed={active}
                    className={`w-full flex items-center gap-2.5 rounded-xl border px-3 py-2.5 text-left transition ${
                      active ? 'border-primary bg-primary/5' : 'border-bial-border hover:bg-bial-bg'
                    }`}
                  >
                    <div className="w-7 h-7 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
                      <Boxes size={14} className="text-primary" />
                    </div>
                    <span className="text-sm font-semibold text-tertiary truncate">{project.name}</span>
                  </button>
                )
              })}
            </div>
          )}
        </div>

        <button
          type="button"
          onClick={() => setCreating(true)}
          className="mt-3 flex items-center gap-1.5 text-sm font-semibold text-primary hover:underline"
        >
          <Plus size={15} /> New project
        </button>

        <div className="flex gap-3 mt-5">
          <button
            type="button"
            onClick={confirm}
            disabled={selectedId === null}
            className="flex-1 bg-primary hover:bg-primary/90 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Continue
          </button>
          <button
            type="button"
            onClick={onClose}
            className="px-4 border border-bial-border text-tertiary hover:bg-bial-bg font-semibold py-2.5 rounded-xl transition text-sm"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
