// Mock C7 brain — emits the tagged-union progress envelope from the CONTRACT
// (docs/engineering/contracts/C7-brain-interface-and-progress.md). The authoritative,
// executable shape is backend/src/api/v1/build_sessions/schemas.py (ProgressEnvelope) — this
// JS mock mirrors it so the skeleton demonstrates BRAIN->SESSION-API without importing Python.
//
// `runBuild(client, onProgress, previewUrl)` mirrors C7's `run_build(session_id, user_id,
// sandbox_client, on_progress) -> BuildResult`: it drives a happy-path build turn against the
// mock C1 client and emits a monotonic `seq` envelope stream ending preview_ready -> ended.
// This is the "mocked brain" leg of the walking skeleton.

let seq = 0
const next = () => ++seq

// The seven frozen envelope constructors (one per C7 `type`). Each carries `seq`.
export const envelope = {
  step: (name, label, state) => ({ type: 'step', seq: next(), name, label, state }),
  log: (source, stream, text) => ({ type: 'log', seq: next(), source, stream, text }),
  error: (source, title, cleaned_stack) => ({ type: 'error', seq: next(), source, title, cleaned_stack }),
  preview_ready: (preview_url) => ({ type: 'preview_ready', seq: next(), preview_url }),
  escalation: (reason, detail) => ({ type: 'escalation', seq: next(), reason, detail, last_error: null }),
  quota_exceeded: (limit, used, resets_at) => ({ type: 'quota_exceeded', seq: next(), limit, used, resets_at }),
  ended: (status, preview_url, snapshot_committed, reason) => ({ type: 'ended', seq: next(), status, preview_url, snapshot_committed, reason }),
}

// The frozen member set (mirrors backend get_args(ProgressEnvelope) - 7 types).
export const C7_TYPES = ['step', 'log', 'error', 'preview_ready', 'escalation', 'quota_exceeded', 'ended']

/**
 * A happy-path build turn against the mock C1 client, emitting the C7 envelope stream.
 * @param {{runExec:Function, devStart:Function, devStatus:Function}} client - the mock C1 client
 * @param {(env:object)=>void} onProgress - the in-process C7 sink (C7 s4)
 * @param {string} previewUrl - the sandbox next-dev root the frame will load (C2/C8)
 * @returns {Promise<object>} a BuildResult (mirrors C7 s1)
 */
export async function runBuild(client, onProgress, previewUrl) {
  seq = 0
  onProgress(envelope.step('scaffold', 'Scaffolding the app...', 'started'))
  const tsc = await client.runExec(['tsc', '--noEmit'])
  onProgress(envelope.log('exec', tsc.exit === 0 ? 'stdout' : 'stderr', tsc.stdout || tsc.stderr))
  onProgress(envelope.step('scaffold', 'Scaffolding the app...', tsc.exit === 0 ? 'ok' : 'failed'))

  onProgress(envelope.step('dev_start', 'Starting the dev server...', 'started'))
  await client.devStart()
  // Poll readiness (C1 /dev/status.ready) - the marker-seen + process-alive gate.
  let status = await client.devStatus()
  for (let i = 0; i < 40 && !status.ready; i++) {
    await new Promise((r) => setTimeout(r, 25))
    status = await client.devStatus()
  }
  onProgress(envelope.step('dev_start', 'Starting the dev server...', status.ready ? 'ok' : 'failed'))

  onProgress(envelope.preview_ready(previewUrl))
  onProgress(envelope.ended('ended', previewUrl, true, 'completed'))
  return { status: 'ended', app_id: 'skeleton-app', preview_url: previewUrl, last_seq: seq, snapshot_committed: true, error: null }
}
