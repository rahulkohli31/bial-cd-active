/**
 * The administrator's classification configuration: the policy, the classes the deployment
 * classification agent checks, and each scored class's share of the score.
 *
 * Every write answers with the whole configuration, so a caller replaces what it holds with the
 * answer rather than patching it. A body we cannot read throws `ApiError`: a class dropped from
 * the table would change every other class's share.
 */
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'
import { ApiError, isRecord, optionalString, readApiError, requiredString } from './apiError'

export type ClassKind = 'hard_block' | 'scored'

export interface ClassificationPolicy {
  threshold: number
  ownersCanChangeAnswers: boolean
}

export interface ClassificationClass {
  key: string
  title: string
  /** The agent's instruction for this class. Admin-only: owners never see it. */
  description: string
  kind: ClassKind
  /** `null` exactly when the class is a hard block. */
  weight: number | null
  active: boolean
  updatedAt: string
  updatedByName: string | null
}

export interface ClassificationConfig {
  policy: ClassificationPolicy
  /** Hard blocks first, then scored classes by weight, highest first. */
  classes: ClassificationClass[]
}

/** What the add and edit dialog sends. A hard block's `weight` is ignored by the server. */
export interface ClassFields {
  title: string
  description: string
  kind: ClassKind
  weight: number | null
  active: boolean
}

export const MAX_TITLE = 60
export const MAX_DESCRIPTION = 1000
export const MAX_WEIGHT = 100
export const MAX_THRESHOLD = 100

function unreadable(field: string): ApiError {
  return new ApiError(`The server sent a classification setting we could not read (${field}).`, 500)
}

function readNumber(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value)) throw unreadable(field)
  return value
}

function readBoolean(value: unknown, field: string): boolean {
  if (typeof value !== 'boolean') throw unreadable(field)
  return value
}

function toClass(value: unknown): ClassificationClass {
  if (!isRecord(value)) throw unreadable('class')
  const { kind } = value
  if (kind !== 'hard_block' && kind !== 'scored') throw unreadable('kind')
  return {
    key: requiredString(value.key, 'class', 'key'),
    title: requiredString(value.title, 'class', 'title'),
    description: requiredString(value.description, 'class', 'description'),
    kind,
    weight: kind === 'scored' ? readNumber(value.weight, 'weight') : null,
    active: readBoolean(value.active, 'active'),
    updatedAt: requiredString(value.updatedAt, 'class', 'updatedAt'),
    updatedByName: optionalString(value.updatedByName),
  }
}

function toConfig(value: unknown): ClassificationConfig {
  if (!isRecord(value) || !isRecord(value.policy) || !Array.isArray(value.classes)) throw unreadable('configuration')
  return {
    policy: {
      threshold: readNumber(value.policy.threshold, 'threshold'),
      ownersCanChangeAnswers: readBoolean(value.policy.ownersCanChangeAnswers, 'ownersCanChangeAnswers'),
    },
    classes: value.classes.map(toClass),
  }
}

async function readConfig(res: Response, fallback: string): Promise<ClassificationConfig> {
  if (!res.ok) throw await readApiError(res, fallback)
  return toConfig(await res.json())
}

const jsonOpts = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export async function fetchClassificationConfig(deps: AuthFetchDeps = {}): Promise<ClassificationConfig> {
  const res = await authFetch('/api/admin/classification', {}, deps)
  return readConfig(res, 'Could not load the classification settings')
}

export async function updateClassificationPolicy(
  patch: Partial<ClassificationPolicy>,
  deps: AuthFetchDeps = {},
): Promise<ClassificationConfig> {
  const res = await authFetch('/api/admin/classification/policy', jsonOpts('PATCH', patch), deps)
  return readConfig(res, 'Could not save the policy')
}

export async function addClassificationClass(fields: ClassFields, deps: AuthFetchDeps = {}): Promise<ClassificationConfig> {
  const res = await authFetch('/api/admin/classification/classes', jsonOpts('POST', fields), deps)
  return readConfig(res, 'Could not add the class')
}

export async function editClassificationClass(
  key: string,
  patch: Partial<ClassFields>,
  deps: AuthFetchDeps = {},
): Promise<ClassificationConfig> {
  const res = await authFetch(
    `/api/admin/classification/classes/${encodeURIComponent(key)}`,
    jsonOpts('PATCH', patch),
    deps,
  )
  return readConfig(res, 'Could not save the class')
}

/** The total weight every share divides by: active scored classes only. */
export function scoredTotal(
  classes: readonly Pick<ClassificationClass, 'kind' | 'weight' | 'active'>[],
): number {
  return classes.reduce((sum, c) => (c.active && c.kind === 'scored' ? sum + (c.weight ?? 0) : sum), 0)
}

/** A weight's share of 100, rounded half up in integer maths as the server scores; `null` when the total is 0. */
export function shareOf(weight: number, total: number): number | null {
  if (total <= 0) return null
  return Math.floor((200 * weight + total) / (2 * total))
}
