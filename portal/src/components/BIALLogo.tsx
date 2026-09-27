/**
 * The BIAL brand mark (Kempegowda International Airport Bengaluru), served from /public.
 * ONE component so every screen renders it identically — shared by the navbar and the
 * login panel. The `<img>` defaulted to `display: inline`, sitting on the text baseline
 * with descender space that reads as "the logo looks off" (`block` removes it); with no
 * `shrink-0` the mark also compressed before the nav links at narrow widths, changing its
 * aspect screen to screen.
 *
 * `dark` puts the colour mark on a white pill and turns the wordmark white, for the dark
 * login panel, and draws it larger there so the airport's name reads; the default suits white
 * backgrounds. BASE_URL keeps the src correct under a sub-path deploy.
 *
 * Every image is rendered from the client's vector artwork at 1x, 2x and 3x of the sizes below,
 * and the sizes live here only, so no call site can scale the mark differently.
 */
export interface BIALLogoProps {
  dark?: boolean
  /**
   * The leaves alone, for the collapsed rail. The wordmark is dropped rather than folded: at 56px
   * there is no width it could occupy, and a clipped brand name is worse than none.
   */
  compact?: boolean
}

const LOCKUP = { file: 'bial-logo-52', width: 62, height: 52 }
const LOCKUP_LARGE = { file: 'bial-logo-88', width: 105, height: 88 }
const LEAVES = { file: 'bial-mark-36', width: 41, height: 36 }

export default function BIALLogo({ dark = false, compact = false }: BIALLogoProps) {
  const art = compact ? LEAVES : dark ? LOCKUP_LARGE : LOCKUP
  const base = `${import.meta.env.BASE_URL}${art.file}`
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <span
        className={`inline-flex items-center shrink-0 ${dark ? 'bg-white rounded-xl p-2.5' : ''}`}
        // The rail keeps the lockup's height, so the rows below do not move when it expands.
        style={compact ? { height: LOCKUP.height } : undefined}
      >
        <img
          src={`${base}.png`}
          srcSet={`${base}.png 1x, ${base}@2x.png 2x, ${base}@3x.png 3x`}
          width={art.width}
          height={art.height}
          alt="BIAL — Kempegowda International Airport Bengaluru"
          className="block"
        />
      </span>
      {/* THE BOARD'S WORDMARK: weight 800, brand teal #0D7377, -0.2px tracking. The `dark` arm
          keeps white, for the login panel the boards do not cover. */}
      {!compact && (
        <span
          className={`min-w-0 font-manrope text-[15px] font-extrabold leading-tight tracking-[-0.2px] ${
            dark ? 'text-white' : 'text-primary'
          }`}
        >
          BIAL Citizen Developer
        </span>
      )}
    </div>
  )
}
