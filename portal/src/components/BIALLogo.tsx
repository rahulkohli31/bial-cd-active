// Official BIAL brand mark (Kempegowda International Airport Bengaluru), served
// from /public. `dark` sits the colour logo on a white pill + white wordmark so
// it stays legible on the dark login panel; the default suits white backgrounds
// (navbar). BASE_URL prefix keeps the src correct under sub-path deploys.
export interface BIALLogoProps {
  dark?: boolean
}

export default function BIALLogo({ dark = false }: BIALLogoProps) {
  return (
    <div className="flex items-center gap-2.5">
      <span className={`inline-flex items-center ${dark ? 'bg-white rounded-lg p-1.5' : ''}`}>
        <img
          src={`${import.meta.env.BASE_URL}bial-logo.png`}
          alt="BIAL — Kempegowda International Airport Bengaluru"
          className="h-8 w-auto"
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
