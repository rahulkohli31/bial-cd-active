// PLATFORM-OWNED. Run in the BUILDER stage, after `next build`.
//
// WHY THIS EXISTS. `scripts/db-migrate.mjs` runs at container start and lives outside the
// Next module graph, so `next build` never traces it and its dependencies do not reach
// `.next/standalone/node_modules`. The first attempt at solving that listed the packages by
// hand in `outputFileTracingIncludes` — and shipped an image that died at start with
//
//     [db] migrations failed: Cannot find module 'xtend/mutable'
//       - node_modules/postgres-interval/index.js  ->  pg-types  ->  pg
//
// because `postgres-interval@1` needs `xtend` and nobody had thought of it. That is the
// whole problem with the hand-written list: `outputFileTracingIncludes` copies files, it
// does NOT follow their dependencies, so every entry silently promises a closure it does
// not deliver, and the next lockfile resolution finds a new hole.
//
// So this computes the closure instead of guessing it: walk `dependencies` from each root
// through the real installed tree and copy every package reached. Adding a database driver
// or bumping a version needs no change here, which is the point.
//
// `optionalDependencies` are followed when present and skipped when not — that is what
// optional means, and `pg` genuinely ships one (`pg-cloudflare`) that is absent on a normal
// Linux install.

import fs from 'node:fs'
import path from 'node:path'

// What the migrator needs at runtime and Next cannot see. `pg` is the driver;
// `drizzle-orm` carries the `node-postgres/migrator` submodule the script imports.
const ROOTS = ['pg', 'drizzle-orm']

const APP = process.cwd()
const STANDALONE_MODULES = path.join(APP, '.next', 'standalone', 'node_modules')

/** Find a package the way Node does: walk up `node_modules` from `fromDir`. */
function resolvePackageDir(name, fromDir) {
  let dir = fromDir
  for (;;) {
    const candidate = path.join(dir, 'node_modules', name)
    if (fs.existsSync(path.join(candidate, 'package.json'))) return candidate
    const parent = path.dirname(dir)
    if (parent === dir) return null
    dir = parent
  }
}

function readManifest(pkgDir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(pkgDir, 'package.json'), 'utf8'))
  } catch {
    return {}
  }
}

const seen = new Set()
const queue = []

for (const root of ROOTS) {
  const dir = resolvePackageDir(root, APP)
  if (dir) queue.push([root, dir])
  // A missing root is not fatal here: an app that genuinely does not use Postgres has
  // nothing to migrate, and `db-migrate.mjs --strict` is the thing that decides that — it
  // fails the container start with a legible message. Failing the BUILD here would refuse
  // to publish an app whose only sin is not using a database.
  else console.warn(`[deps] root not installed, skipping: ${root}`)
}

while (queue.length > 0) {
  const [name, dir] = queue.shift()
  if (seen.has(name)) continue
  seen.add(name)

  const target = path.join(STANDALONE_MODULES, name)
  // Next already traced some of these from the app's own imports. Copying over the top
  // would be wasted work, and worse, could replace a build-tuned copy with a different one.
  if (!fs.existsSync(target)) {
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.cpSync(dir, target, { recursive: true, dereference: true })
  }

  const manifest = readManifest(dir)
  const required = Object.keys(manifest.dependencies ?? {})
  const optional = Object.keys(manifest.optionalDependencies ?? {})

  for (const dep of [...required, ...optional]) {
    if (seen.has(dep)) continue
    const depDir = resolvePackageDir(dep, dir)
    if (depDir) {
      queue.push([dep, depDir])
    } else if (required.includes(dep)) {
      // A declared, non-optional dependency that is not installed means the tree is broken.
      // Failing here is far cheaper than the container dying on MODULE_NOT_FOUND minutes
      // later, which is exactly the failure this script exists to prevent.
      console.error(`[deps] ${name} requires ${dep}, which is not installed`)
      process.exit(1)
    }
  }
}

console.log(`[deps] copied ${seen.size} packages into .next/standalone/node_modules`)
