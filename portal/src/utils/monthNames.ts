/**
 * The twelve months, SPELLED OUT RATHER THAN LEFT TO `Intl`, and this is the reason.
 *
 * The boards draw `2 Sep`, `28 Aug → 15 Sep` and `12 Aug 2026`. `toLocaleDateString` produces
 * none of them reliably: en-GB and en-IN — the locales BIAL's own browsers are set to —
 * abbreviate September as `Sept` under current CLDR, and en-US puts the month first (`Sep 2`).
 * So the one form the boards specify is not any runtime's default, and a suite that pinned it
 * would be pinning the machine it ran on.
 *
 * IT LIVES IN A UTILITY, NOT BESIDE ITS FIRST CONSUMER. Four surfaces now set a date in one of
 * these shapes — a connector row, a window chip, an approval panel and the applications list —
 * and a formatter reached by importing a component drags that component's icons and types into
 * every one of them. `connectors/connectorPresentation.tsx` re-exports it so its own callers are
 * unchanged.
 *
 * The portal's copy is English throughout; the day and the clock stay LOCAL (the reader is in
 * Bangalore and the server stamps UTC), only the shape is fixed.
 */
export const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
