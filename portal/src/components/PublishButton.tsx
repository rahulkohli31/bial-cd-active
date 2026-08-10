/**
 * Publish, in the builder toolbar — beside Save, where the citizen already is when the build
 * finishes and the preview says "your app is live below".
 *
 * THE SAME CONTROL EXISTS ON THE PROJECT PAGE as a card (`DeployControl`). Two surfaces
 * because they answer different questions: this one is "I just finished, put it out there",
 * that one is "where did my app get to". Both drive `useDeployment`, so neither can drift
 * into a different idea of what publishing means.
 *
 * Compact by necessity — this sits in a toolbar that already holds device widths, Reload and
 * Save, so it shows a state and an address and nothing more. The failure detail and the full
 * history stay on the card, where there is room to read them.
 */
import { useState } from 'react'
import { CheckCircle2, ExternalLink, Loader2, Rocket } from 'lucide-react'
import { stepLabel } from '../utils/deployApi'
import { useDeployment } from '../hooks/useDeployment'
import DataClassificationModal from './DataClassificationModal'

export interface PublishButtonProps {
  projectId: string
}

export default function PublishButton({ projectId }: PublishButtonProps): React.ReactElement {
  const { deployment, running, unsaved, saving, onConfirm, saveAndPublish, dismissUnsaved } =
    useDeployment(projectId)
  const [showModal, setShowModal] = useState(false)

  const live = deployment?.status === 'succeeded' && deployment.url

  return (
    <div className="flex items-center gap-2">
      {/* The address, as soon as there is one. A toolbar is exactly where someone looks for
          "so where is it?", and a link beats making them navigate to find out. */}
      {live && (
        <a
          data-testid="publish-url"
          href={deployment.url ?? undefined}
          target="_blank"
          rel="noopener noreferrer"
          title={deployment.url ?? undefined}
          className="hidden lg:inline-flex items-center gap-1 text-[11px] font-worksans text-neutral hover:text-primary transition max-w-[200px]"
        >
          <ExternalLink size={11} className="flex-shrink-0" />
          <span className="truncate">{(deployment.url ?? '').replace(/^https?:\/\//, '')}</span>
        </a>
      )}

      {unsaved && (
        <div
          data-testid="publish-unsaved"
          className="flex items-center gap-1.5"
          role="alert"
        >
          <span className="text-[11px] text-amber-700 max-w-[180px] text-right leading-tight">
            {unsaved}
          </span>
          <button
            type="button"
            data-testid="publish-save-first"
            disabled={saving}
            onClick={() => void saveAndPublish()}
            className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-[11px] font-worksans font-semibold bg-primary text-white hover:bg-primary-600 transition disabled:opacity-50"
          >
            {saving && <Loader2 size={11} className="animate-spin" />}
            Save and publish
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={dismissUnsaved}
            className="text-[11px] font-worksans text-neutral hover:text-tertiary px-1.5 disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      )}

      <button
        type="button"
        data-testid="publish-button"
        disabled={running}
        onClick={() => setShowModal(true)}
        title={running ? 'Publishing…' : 'Publish this app'}
        className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-worksans font-semibold transition disabled:opacity-60 ${
          live
            ? 'border border-bial-border bg-white text-neutral'
            : 'bg-primary text-white hover:bg-primary-600'
        }`}
      >
        {running ? (
          <Loader2 size={12} className="animate-spin" />
        ) : live ? (
          <CheckCircle2 size={12} />
        ) : (
          <Rocket size={12} />
        )}
        {running ? stepLabel(deployment?.step ?? null) : live ? 'Published' : 'Publish'}
      </button>

      {showModal && (
        <DataClassificationModal
          onConfirm={async (answers) => {
            await onConfirm(answers)
            setShowModal(false)
          }}
          onCancel={() => setShowModal(false)}
        />
      )}
    </div>
  )
}
