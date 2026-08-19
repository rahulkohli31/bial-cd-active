"use client";

/**
 * identity-demo.tsx — WORKED EXAMPLE (issue #92, R12, R19) of calling a Server
 * Action with the caller's identity attached. Delete this component (and its use
 * in app/page.tsx) once your app has its own UI — it exists to show the pattern,
 * not because every app needs a "who am I" button.
 */

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useBialAssertion } from "@/lib/bial-identity-client";
import { touchAppUser, type TouchAppUserResult } from "@/app/actions";

export function IdentityDemo() {
  const assertion = useBialAssertion();
  const [result, setResult] = useState<TouchAppUserResult | null>(null);
  const [pending, setPending] = useState(false);

  async function handleClick() {
    setPending(true);
    try {
      // The in-memory assertion is passed EXPLICITLY — a Server Action has no
      // ambient access to a header your client code attached to some other fetch.
      setResult(await touchAppUser(assertion ?? undefined));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="space-y-3 rounded-lg border p-4">
      <Button onClick={handleClick} disabled={pending}>
        {pending ? "Checking..." : "Who am I?"}
      </Button>
      {result?.signedIn && (
        <p className="text-sm">
          Signed in as <strong>{result.displayName ?? result.email}</strong> — recorded in the
          roster (<code>app_users</code>).
        </p>
      )}
      {result && !result.signedIn && (
        <p className="text-muted-foreground text-sm">
          No verified identity yet (this app may not require sign-in, or the identity handshake
          has not completed).
        </p>
      )}
    </div>
  );
}
