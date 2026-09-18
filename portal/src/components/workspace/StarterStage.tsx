/**
 * THE PARTS OF AN APP, LOOSE AROUND AN EMPTY WINDOW — the mark the `never-built` board carries
 * instead of a 30px glyph.
 *
 * WHY A SECOND COPY OF THE SANDBOX'S ARTWORK. The same picture is drawn by the starter page inside
 * every generated app (`sandbox/template/app/page.tsx`), and the two cannot share a file: they are
 * different applications, served from different origins, and the pane this one fills is the one
 * standing there BEFORE any sandbox exists to draw the other. Keeping the geometry identical is
 * what makes the hand-off invisible — the citizen sees one picture that stays put while the app
 * behind it starts. If the numbers below change, change them in both.
 *
 * IT IS THE WAITING HALF ONLY. The assembling loop belongs to a build, and no build is running on
 * any board that reaches this component; drawing it here would claim work nobody started.
 */
export default function StarterStage() {
  return (
    <div className="starter-stage mb-1" aria-hidden="true" data-testid="app-pane-stage">
      <div className="starter-window" />
      <span className="starter-piece starter-topbar" />
      <span className="starter-piece starter-side" />
      <span className="starter-piece starter-heading" />
      <span className="starter-piece starter-button" />
      <span className="starter-piece starter-avatar" />
      <span className="starter-piece starter-chart">
        <span className="starter-bar" />
        <span className="starter-bar" />
        <span className="starter-bar" />
        <span className="starter-bar" />
        <span className="starter-bar" />
      </span>
      <span className="starter-piece starter-stat" />
      <span className="starter-piece starter-rows" />
    </div>
  )
}
