import { IdentityDemo } from "@/components/bial/identity-demo";
import { getBialIdentity } from "@/lib/bial-identity";

export default async function Home() {
  // WORKED EXAMPLE (issue #92, R12, R19) of the checked server-side data path: call
  // the accessor directly in a Server Component's render — no header/cookie
  // plumbing of your own, no plane-specific branching (R13). Delete this call (and
  // the <IdentityDemo/> below) if your app needs no sign-in at all — that is a
  // completely ordinary app and the platform neither requires nor records one (R1).
  const identity = await getBialIdentity();

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-6 px-6 py-16">
      <div className="space-y-3">
        <h1 className="text-4xl font-semibold tracking-tight">Your BIAL app</h1>
        <p className="text-muted-foreground text-lg">
          A blank starting point. Describe what you want to build and the app takes shape here —
          replace this page with your own UI.
        </p>
      </div>
      {identity && (
        <p className="text-sm text-muted-foreground">
          Server-rendered identity check: signed in as{" "}
          <strong>{identity.displayName ?? identity.email}</strong>.
        </p>
      )}
      <IdentityDemo />
    </main>
  );
}
