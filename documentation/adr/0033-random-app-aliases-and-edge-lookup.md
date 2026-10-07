# ADR-0033: Previews Are Addressed by a Random Alias, Resolved at the Edge

## Context

A preview, and a colleague's shared view of one, was served at an address carrying the name of its
container. That name was derived from the application's identity, and a published application's
address was built from the same identity, so anyone holding a published app's link could swap the
prefix and open its owner's live preview. An interim security assessment reported this as
unauthenticated access through an exposed sandbox identifier, and asked that identifiers be neither
exposed nor reused.

Previews are shareable by design: anyone holding the link opens the preview, and signing in is the
application developer's job. The aim is therefore not a gate. It is that a preview's address cannot
be derived from anything else, is new for every container, and does not name the container.

## Decision

**A preview's address carries a random alias.** The alias comes from a secure random generator
(ADR-0006), is minted at the one step that creates a container, is new for every container and is
never reused. Previews and shared views are served at `/a/<alias>/`.

**The alias lives in the owner's registry record, with a reverse key that names the owner.** The
reverse key expires after a stretch without use, and each lookup renews it. A lookup trusts the
reverse key only while the owner's own registry record still holds the alias, so a retired alias
answers no whatever is left over.

**The portal edge resolves an alias through an internal lookup on the control plane.** Only the apps
site of the edge calls it, presenting a shared secret that exists for that call alone. The route sits
outside the public API prefixes, is not in the API document, and is never proxied by the portal
site. It always answers successfully: the container name in a response header when the alias is
current, nothing otherwise. The edge checks the shape of that name before using it, caches a found
answer briefly, never caches a no, and shows the existing "app not available" page for an unknown
or retired alias. A retired alias stops routing within the cache window.

**The edge refuses container names.** An address that names a preview or shared-view container
directly shows the same page, so the old addresses stop working at deploy.

**Published applications keep their address.** The permanent `/a/pub-…/` address is the
application's public link. Changing it would break every saved link and gain nothing: the edge
refuses container names for previews, and production containers are reachable only through the edge.

**The lookup has no per-user scope, which is an exception to ADR-0004.** Its caller is the edge, which
holds no user, and the alias is all it can name. The registry carries the scope instead: the alias
must still be held by the record of the user it was minted for.

**One shared secret authenticates the edge.** It has the same value on the control plane and the
portal, is generated per environment, and both refuse to start without a well-formed one. Rotating
it is a coordinated restart of both.

## Consequences

- A published app's link no longer leads to its owner's preview, a preview's address no longer
  reveals its container, and no address survives its container.
- Anyone holding a preview link still opens it. The alias hides and renews an identifier; it is not
  authentication.
- Known limit: code running in a container can read its own Host header, which carries the
  container's name, so a page that echoes it can print it. Calls from one container to another
  inside the network do not pass the edge.
- Previews resolve only while the control plane and the coordination store answer. When the store
  does not, the edge shows "app not available" rather than guessing; when the control plane itself
  is unreachable the request fails. A deployed application is unaffected by either.
- A preview already running at deploy has no alias. The scheduled sweep stops sparing a record
  without one, so it is written back and torn down, and the owner's next start brings it back with
  an alias (ADR-0030).
- The control plane and the portal change together: previews are unavailable between the
  control plane restarting and the portal coming up, and a rollback takes both back.

## Rejected alternatives

- **Keeping container names behind a sign-in gate.** Previews are shareable by design; a gate
  changes the product and is out of scope here.
- **Hashing the container name.** The result is still derivable from the application's identity by
  anyone who knows the scheme, and still the same for every container that application ever has.
- **Changing published addresses.** It breaks every saved link for no security gain.

## Related

- ADR-0004 (the user-scoping decision the lookup is an exception to)
- ADR-0006 (secure-random values for anything that functions as a secret)
- ADR-0030 (the scheduled sweep that retires previews from before this change)
