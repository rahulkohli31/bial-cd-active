/**
 * The client half of the shared word rule (#158).
 *
 * The cases below are the SAME list `tests/api/v1/projects/test_project_name_words.py`
 * asserts on the server. That duplication is the point: the rule is written twice, once per
 * language, so the only thing keeping the two honest is that both are pinned against the
 * same inputs. If someone "simplifies" this to `split(' ')`, a title typed with a double
 * space starts counting an extra empty word here, the counter disagrees with the API, and a
 * name that reads as 8 words in the browser is refused as 9 by the server.
 */
import { describe, it, expect } from 'vitest'
import {
  countWords,
  MAX_PROJECT_NAME_WORDS,
  MIN_DELETE_REASON_WORDS,
  MAX_DELETE_REASON_WORDS,
} from '../words'

describe('countWords — the shared rule', () => {
  it.each([
    ['', 0],
    ['   ', 0],
    ['one', 1],
    ['a  b', 2], // a double space is one separator, not an empty word
    ['a\tb', 2],
    ['a\nb', 2],
    ['a\r\nb', 2],
    [' lead and trail ', 3],
    ['a b', 2], // non-breaking space
    ['a　b', 2], // ideographic space
    ['five word title right here', 5],
  ])('counts %j as %i', (input, expected) => {
    expect(countWords(input)).toBe(expected)
  })

  it('agrees with the server on the title boundary', () => {
    // 8 words is legal; 9 is not. The server asserts exactly this pair.
    const eight = Array.from({ length: MAX_PROJECT_NAME_WORDS }, (_, i) => `w${i}`).join(' ')
    const nine = `${eight} w8`
    expect(countWords(eight)).toBe(MAX_PROJECT_NAME_WORDS)
    expect(countWords(nine)).toBe(MAX_PROJECT_NAME_WORDS + 1)
  })

  it('is not split(" ") — the mistake that would break the agreement', () => {
    // The regression this file exists to catch. `'a  b'.split(' ')` is ['a', '', 'b'],
    // which counts 3 where Python's str.split() counts 2.
    expect('a  b'.split(' ').length).toBe(3)
    expect(countWords('a  b')).toBe(2)
  })
})

describe('the shared limits', () => {
  it('carries the delete-reason bounds from §13.2', () => {
    expect(MIN_DELETE_REASON_WORDS).toBe(5)
    expect(MAX_DELETE_REASON_WORDS).toBe(50)
  })
})
