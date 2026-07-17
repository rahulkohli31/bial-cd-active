/**
 * The build-brief fence parser (003-U3).
 *
 * The load-bearing tests here are the DEGRADATION ones. This parser sits between the model and
 * the only build trigger on the page, so its failure mode is not "renders oddly" — it is "a
 * non-technical user who just spent three turns describing their app has no button to build it,
 * at the exact moment of highest intent". Every malformed shape must therefore still yield a
 * confirmable proposal; only a fence that isn't there at all yields no card.
 */
import { describe, it, expect } from 'vitest'
import { BUILD_BRIEF_FENCE_TAG, parseBuildBrief } from '../buildBrief'

const brief = 'Build an application for Bengaluru International Airport (BIAL) that tracks visitors.'
const fenced = (body: string): string => `\`\`\`${BUILD_BRIEF_FENCE_TAG}\n${body}\n\`\`\``

describe('parseBuildBrief', () => {
  describe('no fence', () => {
    it('returns none for ordinary prose, so a plain reply stays a plain bubble', () => {
      const text = 'Which terminals should this cover, and who approves a visitor?'
      expect(parseBuildBrief(text)).toEqual({ kind: 'none' })
    })

    it('returns none for empty text', () => {
      expect(parseBuildBrief('')).toEqual({ kind: 'none' })
    })

    it('ignores an unrelated code fence', () => {
      expect(parseBuildBrief('```js\nconst a = 1\n```')).toEqual({ kind: 'none' })
    })

    it('does not match a tag that merely starts with ours', () => {
      // `bial:build-brief-v2` is not our contract; treating it as one would parse an unknown
      // shape as a brief.
      expect(parseBuildBrief('```bial:build-brief-v2\nx\n```')).toEqual({ kind: 'none' })
    })
  })

  describe('one well-formed fence', () => {
    it('extracts the brief and strips the fence from the prose', () => {
      const result = parseBuildBrief(`Here's what I'll build:\n\n${fenced(brief)}`)

      expect(result).toEqual({
        kind: 'brief',
        brief,
        text: "Here's what I'll build:",
      })
    })

    it('keeps prose that follows the fence', () => {
      const result = parseBuildBrief(`Before.\n\n${fenced(brief)}\n\nAfter.`)

      expect(result.kind).toBe('brief')
      if (result.kind !== 'brief') throw new Error('unreachable')
      expect(result.text).toBe('Before.\n\nAfter.')
    })

    it('yields an empty prose string when the reply is only the fence', () => {
      const result = parseBuildBrief(fenced(brief))

      expect(result).toEqual({ kind: 'brief', brief, text: '' })
    })

    it('preserves blank lines and markdown inside the brief', () => {
      const body = 'Build an app that:\n\n- tracks visitors\n- **alerts** on overstay'
      const result = parseBuildBrief(fenced(body))

      expect(result.kind).toBe('brief')
      if (result.kind !== 'brief') throw new Error('unreachable')
      expect(result.brief).toBe(body)
    })

    it('tolerates trailing whitespace on the fence lines', () => {
      const result = parseBuildBrief(`\`\`\`${BUILD_BRIEF_FENCE_TAG}   \n${brief}\n\`\`\`   `)

      expect(result.kind).toBe('brief')
      if (result.kind !== 'brief') throw new Error('unreachable')
      expect(result.brief).toBe(brief)
    })
  })

  describe('degradation — the build trigger must survive', () => {
    it('degrades an unterminated fence to a card carrying the rest as the brief', () => {
      // The stream was cut, or the model forgot the closing fence. The user still gets a
      // confirmable proposal; a plain-text fallback here would remove the only build button.
      const result = parseBuildBrief(`Sure.\n\n\`\`\`${BUILD_BRIEF_FENCE_TAG}\n${brief}`)

      expect(result).toEqual({ kind: 'degraded', brief, text: 'Sure.' })
    })

    it('degrades duplicate fences to a card carrying the FIRST brief', () => {
      const other = 'Build a completely different application that does something else.'
      const result = parseBuildBrief(`${fenced(brief)}\n\n${fenced(other)}`)

      expect(result.kind).toBe('degraded')
      if (result.kind !== 'degraded') throw new Error('unreachable')
      // The first is the one to honour — but the user confirms before anything runs, which is
      // what makes guessing here safe rather than reckless.
      expect(result.brief).toBe(brief)
    })

    it('degrades an empty fence to a card rather than proposing a blank build', () => {
      const result = parseBuildBrief(fenced(''))

      expect(result.kind).toBe('degraded')
      if (result.kind !== 'degraded') throw new Error('unreachable')
      expect(result.brief).toBe('')
    })

    it('never auto-fires: degraded is still a proposal the user confirms', () => {
      // Encoded as a type-level fact — there is no variant that means "start the build".
      const result = parseBuildBrief(fenced(brief))
      expect(['none', 'brief', 'degraded']).toContain(result.kind)
    })
  })

  describe('the fence contract', () => {
    it('pins the tag shared with the backend protocol', () => {
      // Mirrors `backend/src/api/v1/claude/prompts.py::BUILD_BRIEF_FENCE_TAG`, pinned there too.
      // Drift has no user-reportable symptom: the brief just renders as raw markdown forever.
      expect(BUILD_BRIEF_FENCE_TAG).toBe('bial:build-brief')
    })
  })
})
