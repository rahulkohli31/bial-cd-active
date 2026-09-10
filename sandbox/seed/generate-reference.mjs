#!/usr/bin/env node
/**
 * Generate `sandbox/template/lib/flight-data.reference.ts` from `sandbox/seed/flight-data.ts`.
 *
 *   node generate-reference.mjs            # write the file
 *   node generate-reference.mjs --stdout   # print exactly what it would write, and write nothing
 *
 * WHY A GENERATOR AT ALL. The reference file ships to every workspace commented out, because the
 * golden template deliberately pre-installs none of the four packages the example imports — an
 * app that reads no flight data must stay byte-identical to today. So the source cannot live in
 * the template: it would not compile there. It lives here, where it is typechecked under the
 * template's own `strict` settings and unit-tested, and the template gets a rendering of it.
 *
 * WHY LINE COMMENTS AND NOT A `/* … *\/` WRAPPER. The first draft of this file was hand-written as
 * one block comment and was silently broken: the JSDoc blocks inside the body closed the wrapper
 * early and the tail parsed as code. The template's `lint` script is `tsc --noEmit` across the
 * WHOLE tree, so that mistake fails every citizen's lint at once, in apps that never asked for
 * flight data. Line comments cannot close early, and a generator means nobody has to remember.
 *
 * THE RULE, in full, because a test in another language must be able to trust it: the head block
 * below, then every line of the source with `// ` in front of it — `//` alone for a blank line, so
 * no line ends in whitespace. Input line endings are normalised to LF on the way in, and the
 * output is written with LF regardless of host, because this repository is developed on macOS and
 * built on a Windows VM (see the root `.gitattributes`).
 *
 * This module is the ONLY implementation of that rule. `sandbox/tests/test_template_reference_is_
 * generated.py` shells out to `--stdout` rather than re-deriving it in Python, so the two can
 * never drift apart.
 */

import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const SOURCE = join(HERE, 'flight-data.ts')
const TARGET = join(HERE, '..', 'template', 'lib', 'flight-data.reference.ts')

const HEAD = `\
// ─────────────────────────────────────────────────────────────────────────────────────────────
// REFERENCE ONLY — nothing here runs until you switch it on.
//
// GENERATED FILE — do not hand-edit. Its source is \`sandbox/seed/flight-data.ts\`, where it is
// typechecked under these same \`strict\` settings and unit-tested; regenerate it with
// \`node sandbox/seed/generate-reference.mjs\`. An edit made here is overwritten by the next
// regeneration, and a test fails the moment this file and its source disagree.
//
// The worked example for reading BIAL flight operations data from the connected data lake. Every
// line below has been typechecked under \`strict\` and run against a real lake; the numbers in the
// comments are measured, not estimated.
//
// TO USE IT
//   1. Install the packages. They are NOT pre-installed — an app that does not read flight data
//      should not carry them, and yours should not carry them until it does:
//
//        npm install hyparquet hyparquet-compressors @azure/identity @azure/storage-blob
//
//   2. Uncomment the body (strip the leading \`// \` from each line) into your own module, or copy
//      the parts you need. Prefer copying: this is a worked example, not a library to import.
//
//   3. The two environment variables are injected for you when the data connector is switched on
//      for this project. If they are missing, the connector is off — the code says so explicitly.
//
// WHY IT SHIPS COMMENTED OUT, AND WHY AS A WHOLE FILE
// The seven mistakes marked below all produce an app that looks finished, builds green, and
// reports wrong numbers — or crashes only on an unusual day. None of them raises an error at the
// point you make it. A library would hide them behind a function name; a worked example makes you
// read each one once, in the place it matters.
//
// The body is line-commented rather than wrapped in a block comment on purpose: the JSDoc blocks
// inside it would close a \`/* … */\` wrapper early and leave the rest parsing as code. This app's
// \`npm run lint\` is \`tsc --noEmit\` across the whole tree, so that mistake would fail the lint of
// every app that ships this file, including every app that never reads flight data.
// ─────────────────────────────────────────────────────────────────────────────────────────────
//
`

/** The head block, then the source line-commented. The one definition of the rule. */
export function render(source) {
  const lines = source.split(/\r?\n/)
  // A text file ends in a newline, so the split leaves one empty element behind it. Dropping it
  // keeps the output from ending in a stray `//` and lets the file end in a newline of its own.
  if (lines[lines.length - 1] === '') lines.pop()
  const body = lines.map((line) => (line === '' ? '//' : `// ${line}`)).join('\n')
  return `${HEAD}${body}\n`
}

const output = render(readFileSync(SOURCE, 'utf8'))

if (process.argv.includes('--stdout')) {
  process.stdout.write(output)
} else {
  writeFileSync(TARGET, output, 'utf8')
  process.stderr.write(`wrote ${TARGET} (${output.split('\n').length} lines)\n`)
}
