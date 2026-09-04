/**
 * The BIAL brand mark (Kempegowda International Airport Bengaluru), served from /public.
 *
 * ONE component, so every screen renders it identically (#158 §6). It already was shared —
 * the navbar and the login panel both use it — but two things made it sit badly:
 *
 *  1. THE IMAGE WAS INLINE. An `<img>` defaults to `display: inline`, so it sits on the
 *     text baseline and carries a few pixels of descender space underneath. Against a
 *     wordmark centred by flexbox, that reads as the mark being a touch high — the classic
 *     cause of "the logo looks off" that no amount of padding fixes. `block` removes it.
 *  2. IT COULD BE SQUEEZED. With no `shrink-0` the mark compressed before the nav links did
 *     at narrow widths, so its aspect changed from screen to screen.
 *
 * `dark` sits the colour mark on a white pill and turns the wordmark white, for the dark
 * login panel; the default suits white backgrounds. BASE_URL keeps the src correct under a
 * sub-path deploy.
 */
export interface BIALLogoProps {
  dark?: boolean
}

export default function BIALLogo({ dark = false }: BIALLogoProps) {
  return (
    <div className="flex items-center gap-2.5">
      <span className={`inline-flex items-center shrink-0 ${dark ? 'bg-white rounded-lg p-1.5' : ''}`}>
        <img
          src={`${import.meta.env.BASE_URL}bial-logo.png`}
          alt="BIAL — Kempegowda International Airport Bengaluru"
          // `block` kills the inline baseline gap; the fixed height is the single source of
          // the mark's size, so no call site can scale it differently.
          className="block h-8 w-auto"
        />
      </span>
      {/* THE BOARD'S WORDMARK: 15px, weight 800, brand teal #0D7377, -0.2px tracking. It was
          18px/700 in #00818A — a teal that is not the brand teal and that no board draws. The
          `dark` arm keeps white, for the login panel the boards do not cover. */}
      <span
        className={`font-manrope text-[15px] font-extrabold leading-tight tracking-[-0.2px] ${
          dark ? 'text-white' : 'text-primary'
        }`}
      >
        BIAL Citizen Developer
      </span>
    </div>
  )
}
