# ADR-0033: Previews Are Addressed by a Random Alias and Open Only for Their Owner

## Context

A preview, and a colleague's shared view of one, was served at an address carrying the name of its
container. That name was derived from the application's identity, and a published application's
address was built from the same identity, so anyone holding a published app's link could swap the
prefix and open its owner's live preview. An interim security assessment reported this as
unauthenticated access through an exposed sandbox identifier, and asked that identifiers be neither
exposed nor reused.

A random address answers only half of that. Anyone holding a preview's link still opens it, signed in
or not. A preview is unreviewed work in progress running against its owner's own database, and it is
for the person building it. A colleague is shown a project by sharing it, which gives them a view of
their own, and a published application is the address meant for everyone.

The apps site cannot see the portal's sign-in. The portal's session cookie is held by the portal's
site alone, deliberately, so that generated code served from the apps site never receives it. Nor can
the check live in the application: its code, and the base it starts from, can be changed by prompting.
The edge therefore has to learn who is asking, and the control plane has to decide.

## Decision

**A preview's address carries a random alias.** The alias comes from a secure random generator
(ADR-0006), is minted at the one step that creates a container, is new for every container and is
never reused. Previews and shared views are served at `/a/<alias>/`.

**The alias lives in the registry record of the person whose workspace runs the container, with a
reverse key that names that person.** For a shared view that person is the colleague holding it. The
reverse key expires after a stretch without use, and each lookup renews it. A lookup trusts the
reverse key only while that person's own registry record still holds the alias, so a retired alias
answers no whatever is left over.

**The portal edge resolves an alias through an internal lookup on the control plane.** Only the apps
site of the edge calls it, presenting a shared secret that exists for that call alone. The route sits
outside the public API prefixes, is not in the API document, and is never proxied by the portal
site. It names the container in a response header only when the alias is current and the person
asking holds it. Otherwise it says which kind of no it is: nothing to open, or not this person. The
edge checks the shape of a container name before using it, caches a found answer briefly, never
caches a no, and shows the existing "app not available" page for an unknown or retired alias.

**A preview opens only for the person whose workspace runs it.** The browser proves who it is to the
apps site with a preview pass: a random value (ADR-0006) in a cookie that only the apps site
receives and that page script cannot read. The control plane keeps the pass's record in the
coordination store, naming the person and the revocation state of the sign-in it was issued from.
The record lasts as long as the portal's longest session and is not renewed by use, and the record is
all that makes a pass valid.

**A pass is issued through the portal's sign-in.** A navigation on the apps site that brings no pass
the lookup accepts is sent to a hand-over route on the portal's site, which the portal's session
cookie reaches. When that session has lapsed, a browser tab is sent to the portal, which renews the
session or asks the person to sign in, and the preview is opened again from there. The hand-over never
sends a person to sign-in directly, so a preview tab reloading after a sign-out cannot start a new
session on its own. A frame, where sign-in cannot run, gets the "app not available" page and opens
on its next load once the portal has renewed the session. The hand-over checks that the alias
belongs to that person. When it does not, the hand-over shows "app not available" and issues nothing, so a
refusal never loops. When it does, the hand-over issues a ticket that works once and lapses within
moments, and sends the browser to an entry route on the apps site. The entry route redeems the
ticket, sets the pass and continues to the address first asked for. That address travels in the
ticket's record and is never read from the entry request. A request that is not a navigation cannot
follow a sign-in, so without an accepted pass it gets the "app not available" page.

**A ticket is redeemed only by the browser that asked for it.** When the edge sends a browser to the
hand-over it also gives that browser a one-time value, in a cookie only the apps site receives, and
the ticket records that value. The entry route redeems a ticket only from a browser holding the value
it was issued against, so a ticket carried into someone else's browser plants nothing there. A ticket
the entry route cannot redeem ends on the "app not available" page, never on another redirect, so a
browser that refuses the cookies cannot loop.

**No application can stand in front of another's requests.** Every application shares the apps
site's origin, and a service worker registered at the root of that origin would see every preview
and the entry route. The edge refuses a service worker script requested outside an application's
own address and drops the response header that widens a worker's reach.

**The lookup accepts a pass only for its own person.** The pass must name the person whose registry
record holds the alias, that person must not be suspended, and their sign-in must not have been
revoked since the pass was issued. Signing out of the portal and an administrator's suspension
therefore close every preview of that person's through the revocation that already ends their
sessions (ADR-0007). There is no exception for administrators: no administrative screen opens
someone else's preview.

**The application never holds the pass.** The edge removes it from every request it passes to a
container, as it already removes its own routing cookie. A found answer is cached against the alias
and the pass together, never the alias alone.

**The edge refuses container names.** An address that names a preview or shared-view container
directly shows the same page, so the old addresses stop working at deploy.

**Published applications keep their address and their access.** The permanent `/a/pub-…/` address is
the application's public link, and the edge's rule for it never consults the lookup, so nothing here
changes how a published application is reached. Changing its address would break every saved link
and gain nothing: the edge refuses container names for previews, and production containers are
reachable only through the edge.

**The pass stands in for the user scope the edge cannot apply (ADR-0004).** The edge holds no user,
so the scope is carried twice: the alias must still be held by the record of the person it was minted
for, and the pass must name that same person.

**One shared secret authenticates the edge.** It has the same value on the control plane and the
portal, is generated per environment, and both refuse to start without a well-formed one. The edge
presents it on the lookup and on the entry route. Rotating it is a coordinated restart of both.

## Consequences

- A published app's link no longer leads to its owner's preview, a preview's address no longer
  reveals its container, and no address survives its container.
- A preview opens only for the person running it. A link handed to anyone else, or opened signed out,
  ends at sign-in and then "app not available". Showing work to a colleague is done by sharing the
  project.
- The first preview a browser opens passes through the portal and back. In the workspace's preview
  pane this is invisible: the pane frames a preview only after the portal has just answered for the
  person, so their session is current when the hand-over reads it.
- The pane's redirects carry the portal's session only because the portal and the apps site share a
  scheme and a registrable domain, which makes the frame's requests same-site. Moving either site
  breaks this silently. A local stack needs both sites on secure origins under one shared parent
  name for the same reason: a bare local host name is a site of its own.
- The gate stops other people, not other applications. Every app shares the apps site's origin, so an
  app its owner opens in the same browser can still reach that owner's preview without knowing its
  address, and calls from one
  container to another inside the network do not pass the edge. A service worker an application
  registered at the root before the edge refused them stays in that browser until it is cleared.
  Code running in a container can read its own Host header, which carries the container's name, so
  a page that echoes it can print it.
- A revoked pass keeps opening a preview only until the edge's cached answer for it expires, and a
  connection already open, such as the live-reload socket, stays open until it closes.
- Previews resolve only while the control plane, the coordination store and the database answer,
  since accepting a pass reads the person's revocation state. When any of them does not, the edge
  shows "app not available" or the request fails; it never guesses. A deployed application is
  unaffected.
- Nothing between the apps site and the browser may cache it: a shared cache would hand one person's
  preview to another.
- A preview already running when aliases were introduced has no alias. The scheduled sweep stops
  sparing a record without one, so it is written back and torn down, and the owner's next start
  brings it back with an alias (ADR-0030).
- The portal changes before the control plane. The new edge works against the old control plane,
  which never refuses a pass, so previews stay open until the control plane restarts; a rollback
  takes the control plane back first. A browser that holds no pass when the gate arrives is sent
  through the hand-over on its next navigation.

## Rejected alternatives

- **Hashing the container name.** The result is still derivable from the application's identity by
  anyone who knows the scheme, and still the same for every container that application ever has.
- **Changing published addresses.** It breaks every saved link for no security gain.
- **Sending the portal's session cookie to the apps site.** Widening it to the shared domain sends the
  platform's session to every site under that domain and to every generated application.
- **Serving previews from the portal's own site.** Generated code would run beside the portal's
  session and could act as the person viewing it.
- **Sign-in at the hosting platform or the gateway.** It would gate the portal as well, and it knows
  who signed in but not whose preview is whose.
- **Checking inside the application or its base.** The code is generated and the base can be changed
  by prompting; a check the application can remove is not a check.
- **A self-contained signed pass with no record.** Renewing it by use would mean rewriting the cookie
  on responses the application produces, so it would lapse on a fixed clock in the middle of work.

## Related

- ADR-0004 (the user scoping the pass stands in for at the edge)
- ADR-0006 (secure-random values for anything that functions as a secret)
- ADR-0007 (the portal sign-in a pass is issued from, and the revocation it follows)
- ADR-0030 (the scheduled sweep that retires previews from before this change)
