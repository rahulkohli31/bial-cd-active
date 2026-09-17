/**
 * Strip comments before a source-scanning rule reads a file — shared, because the subtlety is.
 *
 * The `[^:]` guard on `//` exists so a URL scheme (e.g. `https://…`) is not mistaken for a
 * comment opener; every caller scans files that carry such links. A copy of this regex that lost
 * the guard would silently eat the rest of every line holding a link, and the rule reading the
 * result would go quiet rather than red — hence one shared definition, not five that can diverge.
 *
 * COMMENTS ARE NOT SOURCE, and here that distinction has teeth: several vendored files carry a
 * comment naming the identifier they deliberately dropped, so a re-copy is recognised. Scanning
 * comments would push someone to delete the one line that explains the trap.
 */
export function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}
