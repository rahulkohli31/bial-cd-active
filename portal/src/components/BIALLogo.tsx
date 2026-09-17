/**
 * The BIAL brand mark (Kempegowda International Airport Bengaluru), served from /public.
 * ONE component so every screen renders it identically — shared by the navbar and the
 * login panel. The `<img>` defaulted to `display: inline`, sitting on the text baseline
 * with descender space that reads as "the logo looks off" (`block` removes it); with no
 * `shrink-0` the mark also compressed before the nav links at narrow widths, changing its
 * aspect screen to screen.
 *
 * `dark` puts the colour mark on a white pill and turns the wordmark white, for the dark
 * login panel; the default suits white backgrounds. BASE_URL keeps the src correct under
 * a sub-path deploy.
 */
export interface BIALLogoProps {
  dark?: boolean
  /**
   * The mark alone, for the collapsed rail. The wordmark is dropped rather than folded: at 56px
   * there is no width it could occupy, and a clipped brand name is worse than none.
   */
  compact?: boolean
}

export default function BIALLogo({ dark = false, compact = false }: BIALLogoProps) {
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <span className={`inline-flex items-center shrink-0 ${dark ? 'bg-white rounded-lg p-1.5' : ''}`}>
        <img
          src={`${import.meta.env.BASE_URL}bial-logo.png`}
          alt="BIAL — Kempegowda International Airport Bengaluru"
          // `block` kills the inline baseline gap; the fixed height is the single source of
          // the mark's size, so no call site can scale it differently.
          className={`block w-auto ${compact ? 'h-9' : 'h-11'}`}
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
