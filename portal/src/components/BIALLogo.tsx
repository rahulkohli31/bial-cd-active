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
      <span
        className="font-manrope text-lg leading-tight"
        style={{ fontWeight: 700, color: dark ? '#FFFFFF' : '#00818A' }}
      >
        BIAL Citizen Developer
      </span>
    </div>
  )
}
