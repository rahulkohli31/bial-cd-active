#!/usr/bin/env node
/**
 * Generate the two shipped forms of `sandbox/seed/flight-data.ts`:
 * `sandbox/template/lib/flight-data.reference.ts` and `sandbox/scripts/adopt-flight-data.mjs`.
 *
 *   node generate-reference.mjs                  # write both files
 *   node generate-reference.mjs --stdout         # print exactly what it would write to the reference
 *   node generate-reference.mjs --stdout adopt   # print exactly what it would write to the script
 *
 * Neither `--stdout` form writes anything.
 *
 * WHY A GENERATOR AT ALL. The reference file ships to every workspace commented out, because the
 * golden template deliberately pre-installs none of the four packages the example imports — an
 * app that reads no flight data must stay byte-identical to today. So the source cannot live in
 * the template: it would not compile there. It lives here, where it is typechecked under the
 * template's own `strict` settings and unit-tested, and the template gets a rendering of it.
 *
 * The adopt script is the image's install command for the same body. It installs the four
 * packages at the exact versions in this workspace's `package.json`, then writes the body to the
 * app's `lib/flight-data.ts`. The body and the versions are embedded when it is generated, because
 * this workspace is never copied into the image: the script reads nothing at run time.
 *
 * WHY LINE COMMENTS AND NOT A `/* … *\/` WRAPPER. The first draft of this file was hand-written as
 * one block comment and was silently broken: the JSDoc blocks inside the body closed the wrapper
 * early and the tail parsed as code. The template's `lint` script is `tsc --noEmit` across the
 * WHOLE tree, so that mistake fails every citizen's lint at once, in apps that never asked for
 * flight data. Line comments cannot close early, and a generator means nobody has to remember.
 *
 * THE RULE, in full, because a test in another language must be able to trust it: the head block
 * below, then every line of the source with `// ` in front of it — `//` alone for a blank line, so
 * no line ends in whitespace. Input line endings are normalised to LF on the way in, and both
 * outputs are written with LF regardless of host, because this repository is developed on macOS
 * and built on a Windows VM (see the root `.gitattributes`).
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
const MANIFEST = join(HERE, 'package.json')
const REFERENCE = join(HERE, '..', 'template', 'lib', 'flight-data.reference.ts')
const ADOPT = join(HERE, '..', 'scripts', 'adopt-flight-data.mjs')

// Where `Dockerfile.sandbox` puts the adopt script. The backend names the same path.
const ADOPT_COMMAND = 'node /usr/local/lib/bial/adopt-flight-data.mjs'

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
// line below has been typechecked under \`strict\` and unit-tested; the numbers in the comments
// are measured, not estimated.
//
// TO USE IT
//   Run the install command from the app's folder:
//
//        ${ADOPT_COMMAND}
//
//   It installs the four packages at the versions this body was tested with, then writes this
//   body, uncommented, to \`lib/flight-data.ts\`. If \`lib/flight-data.ts\` already exists it
//   changes nothing: that file is this app's own copy.
//
//   The two environment variables are injected for you when the data connector is switched on
//   for this project. If they are missing, the connector is off — the code says so explicitly.
//
//   Only in a workspace without that command, do it by hand:
//     1. Install the packages. They are NOT pre-installed — an app that does not read flight
//        data should not carry them, and yours should not carry them until it does:
//
//          npm install hyparquet hyparquet-compressors @azure/identity @azure/storage-blob
//
//     2. Copy the whole body into \`lib/flight-data.ts\`, stripping the leading \`// \` from each
//        line. All of it: the page pattern at the end keeps your app inside its memory, and it
//        uses the reading functions above it, so copy both.
//
// WHY IT SHIPS COMMENTED OUT, AND WHY AS A WHOLE FILE
// The template installs none of the four packages, so a live copy of this body would not compile
// in an app that reads no flight data. The install command writes this same verified body, so an
// app that does read flight data gets it as source it can read, not as a library. The seven
// mistakes marked below all produce an app that looks finished, builds green, and reports wrong
// numbers — or crashes only on an unusual day. None of them raises an error at the point you make
// it. A library would hide them behind a function name; the body names each one in the place it
// matters.
//
// The body is line-commented rather than wrapped in a block comment on purpose: the JSDoc blocks
// inside it would close a \`/* … */\` wrapper early and leave the rest parsing as code. This app's
// \`npm run lint\` is \`tsc --noEmit\` across the whole tree, so that mistake would fail the lint of
// every app that ships this file, including every app that never reads flight data.
// ─────────────────────────────────────────────────────────────────────────────────────────────
//
`

/** The head block, then the source line-commented. The one definition of the rule. */
function render(source) {
  const lines = source.split(/\r?\n/)
  // A text file ends in a newline, so the split leaves one empty element behind it. Dropping it
  // keeps the output from ending in a stray `//` and lets the file end in a newline of its own.
  if (lines[lines.length - 1] === '') lines.pop()
  const body = lines.map((line) => (line === '' ? '//' : `// ${line}`)).join('\n')
  return `${HEAD}${body}\n`
}

/** The dependencies as `"name": "version",` lines, refusing anything but an exact version. */
function pinned(dependencies) {
  return Object.entries(dependencies)
    .map(([name, version]) => {
      if (!/^\d+\.\d+\.\d+$/.test(version)) {
        throw new Error(`sandbox/seed/package.json pins ${name} to "${version}", not an exact version`)
      }
      return `  ${JSON.stringify(name)}: ${JSON.stringify(version)},`
    })
    .join('\n')
}

/** The install command: the dependencies at their exact versions, and the source as one string
 * per line, each a JSON string literal so no character of the source can end it early. */
function adoptScript(source, dependencies) {
  const listed = (items) => items.map((item) => `  ${JSON.stringify(item)},`).join('\n')
  return `\
#!/usr/bin/env node
// GENERATED FILE — do not hand-edit. \`sandbox/seed/generate-reference.mjs\` writes it from
// \`sandbox/seed/flight-data.ts\` and the versions in \`sandbox/seed/package.json\`; regenerate it
// with \`node sandbox/seed/generate-reference.mjs\`.
//
// Installs the flight-data module into the app in the current folder:
//
//   ${ADOPT_COMMAND}
//
// It installs the four packages at the versions the module was tested with, then writes the
// module to \`lib/flight-data.ts\`. It changes nothing when that file already exists, and writes no
// module when the install fails. It skips the install when the app's \`package.json\` already pins
// all four at those versions, which is how an app whose packages need an npm flag gets the module.
// Everything it writes is embedded below.

import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'

const TARGET = 'lib/flight-data.ts'

const PINS = {
${pinned(dependencies)}
}
const PACKAGES = Object.entries(PINS).map(([name, version]) => \`\${name}@\${version}\`)

const MODULE = [
${listed(source.split(/\r?\n/))}
].join('\\n')

if (existsSync(TARGET)) {
  console.error(
    \`\${TARGET} already exists, so nothing was installed or changed. That file is this app's own \` +
      'copy of the flight-data module and stays authoritative: read its exports before relying on ' +
      'any summary of the module.',
  )
  process.exit(1)
}

const manifest = existsSync('package.json') ? JSON.parse(readFileSync('package.json', 'utf8')) : {}
const installed = Object.entries(PINS).every(
  ([name, version]) => manifest.dependencies?.[name] === version,
)

if (!installed) {
  // Inside the platform's ten-minute limit on an install command: npm is stopped here, not left
  // running after that limit ends this script.
  const install = spawnSync(
    'npm',
    ['install', '--save-exact', '--no-audit', '--no-fund', '--loglevel=error', ...PACKAGES],
    { stdio: 'inherit', timeout: 540_000 },
  )
  if (install.status !== 0) {
    const reason = install.error ? \` (\${install.error.message})\` : ''
    console.error(
      \`npm install did not succeed\${reason}, so \${TARGET} was not written. If this app's \` +
        'packages need an npm flag such as --legacy-peer-deps, install these yourself with it — ' +
        \`npm install --save-exact \${PACKAGES.join(' ')} — then run this command again: it \` +
        'writes the module once they are installed.',
    )
    process.exit(1)
  }
}

mkdirSync('lib', { recursive: true })
writeFileSync(TARGET, MODULE)
console.log(
  \`\${installed ? 'Found' : 'Installed'} \${PACKAGES.join(', ')} and wrote \${TARGET}. Import it \` +
    "from '@/lib/flight-data' in server code only.",
)
`
}

const source = readFileSync(SOURCE, 'utf8')
const outputs = {
  reference: { path: REFERENCE, text: render(source) },
  adopt: {
    path: ADOPT,
    text: adoptScript(source, JSON.parse(readFileSync(MANIFEST, 'utf8')).dependencies),
  },
}

const asked = process.argv.indexOf('--stdout')
if (asked === -1) {
  for (const { path, text } of Object.values(outputs)) {
    writeFileSync(path, text, 'utf8')
    process.stderr.write(`wrote ${path} (${text.split('\n').length} lines)\n`)
  }
} else {
  const which = process.argv[asked + 1] ?? 'reference'
  if (!Object.hasOwn(outputs, which)) {
    process.stderr.write(`--stdout takes "reference" or "adopt", not "${which}"\n`)
    process.exit(2)
  }
  process.stdout.write(outputs[which].text)
}
