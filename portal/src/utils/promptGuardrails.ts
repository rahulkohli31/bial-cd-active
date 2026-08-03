// Client-side guardrails on a build/refine request, enforced by ProjectBuilder at submit time.
// The load-bearing rule (below): a request naming passenger PII / financial / medical data is
// flagged for IT Security review before go-live. (This used to mirror the old client-side
// builder's file-storage prompt guidance, retired with the open-sandbox pivot; the gate stands.)
const RULES = [
  {
    keywords: [
      'hack', 'exploit', 'vulnerability', 'bypass security', 'unauthorized access',
      'steal data', 'surveillance without consent', 'track employees secretly',
      'spy on', 'discriminate', 'racial profiling', 'deny service based on',
    ],
    message:
      'This request contains content that may be harmful or unethical. The Citizen Developer platform is designed for building operational tools that improve airport efficiency and safety. Please revise your prompt.',
  },
  {
    keywords: [
      'personal website', 'dating app', 'social media', 'e-commerce store', 'online shop',
      'cryptocurrency', 'trading bot', 'gambling', 'game unrelated to operations',
      'my personal', 'for my side project',
    ],
    message:
      'This request appears to be outside the scope of airport operations. The Citizen Developer platform is for building tools related to terminal operations, ground handling, passenger services, logistics, security, and facility management. Please refine your prompt to align with airport operational needs.',
  },
  {
    keywords: [
      'passenger personal data', 'credit card', 'passport number', 'social security',
      'medical records without authorization', 'export all passenger', 'bulk download personal',
    ],
    message:
      'This request involves sensitive personal data that requires special handling. Building apps that process passenger PII, financial data, or medical records requires IT security review. Please contact the IT Security team before proceeding, or revise your prompt to use anonymized or aggregated data.',
  },
]

export interface PromptViolation {
  message: string
  flaggedKeywords: string[]
}

export function validatePrompt(promptText: string): PromptViolation | null {
  const lower = promptText.toLowerCase()
  for (const rule of RULES) {
    const flagged = rule.keywords.filter((kw) => lower.includes(kw.toLowerCase()))
    if (flagged.length > 0) {
      return { message: rule.message, flaggedKeywords: flagged }
    }
  }
  return null
}
