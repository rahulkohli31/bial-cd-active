"use server";

/**
 * app/actions.ts — Server Actions live here. WORKED EXAMPLE (issue #92, R12, R19):
 * `touchAppUser` shows the pattern every Server Action that needs to know who is
 * calling it must follow. This file is a starting point — add your own actions
 * alongside this one, or delete the example once you have real actions of your own.
 *
 * R12: a page's own identity check does NOT cover the Server Actions it defines —
 * call the accessor again, INSIDE the action, every time.
 */

import { getBialIdentity } from "@/lib/bial-identity";
import { getDb } from "@/db";
import { appUsers } from "@/db/schema";
import { sql } from "drizzle-orm";

export type TouchAppUserResult =
  | { signedIn: true; displayName: string | null; email: string }
  | { signedIn: false };

/**
 * Confirms who is calling, and records them in the roster (get-or-create + touch
 * `lastSeenAt`) — the shape every "record who did this" action follows. `assertion`
 * is the in-memory token a Client Component read via `useBialAssertion()`
 * (`@/lib/bial-identity-client`) and passed explicitly: a Server Action has no
 * ambient access to a header your OWN client code attached to some unrelated
 * `fetch` call.
 */
export async function touchAppUser(assertion?: string): Promise<TouchAppUserResult> {
  const identity = await getBialIdentity(assertion);
  if (!identity) return { signedIn: false };

  await getDb()
    .insert(appUsers)
    .values({
      entraObjectId: identity.entraObjectId,
      email: identity.email,
      displayName: identity.displayName,
    })
    .onConflictDoUpdate({
      target: appUsers.entraObjectId,
      set: {
        email: identity.email,
        displayName: identity.displayName,
        lastSeenAt: sql`now()`,
      },
    });

  return { signedIn: true, displayName: identity.displayName, email: identity.email };
}
