# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Pre-releases.** The sandbox-first workspace ships to `main` as one release, `1.7.0`. Until then
> each wave that lands on `feat/sandbox-first-workspace` is cut as a beta below, and `VERSION`
> carries the suffix — `1.7.0-beta.N`, then `-rc.N` once only fixes remain. At the merge a dated
> `1.7.0` section is added above them and tagged `v1.7.0`; the betas stay as the record of how it
> got there. A version number marks a build, not a merge.

## [1.7.0-beta.6] - 2026-09-02

The agent's whole voice.

### Added

- **The agent's writing reaches you as it is written, in the order it was written.** It used to be
  held back until the turn ended, and any sentence written beside an action was thrown away
  outright — so the account of a build was a row of receipts with the explanation between them
  deleted. Words and steps now arrive interleaved, and a chat you reload reads in exactly the order
  you watched it happen in.
- **The agent can think for as long as a question needs, and the thinking costs you nothing.**
  Reasoning tokens no longer count against your daily allowance.

### Changed

- **Nothing is said in the agent's name that the agent did not write.** "Getting started on that…"
  is the platform's own line, and it used to sit there spinning underneath an answer you had already
  finished reading. It is taken back off the screen the moment there is anything real above it.
- **You can see what the agent read to get somewhere.** A build's activity used to open on a write,
  with no account of what was looked at first. Reads are drawn now. Only plumbing stays hidden — a
  configuration file, a housekeeping command — and a step that failed is never hidden.
- **The agent writes at the length the answer needs.** The rules that capped how much it could say,
  forced every plan into the same five parts, and prescribed how it had to sign off are gone. What
  stays is who it is writing for.

### Fixed

- **Two first messages racing on the same new chat no longer lose one of them.** The message that
  lost the race was answered with a bare failure, and the citizen watched their sentence vanish
  while the reply it had started was already running in the other chat. It now joins the chat that
  won, with the message intact.
- **An administrator can no longer set a per-conversation maximum that locks someone out.** Below a
  floor, the limit refused every chat that person opened — including a brand-new empty one — and
  told them to start a new chat, which was the one thing that also failed. The form refuses the
  value with a sentence saying why, and a value already stored below the floor is clamped on read,
  so the people the defect already reached are working again without an administrator touching
  anything.
- **A reload in the middle of a build no longer shows you every sentence twice.** The turn's stored
  prose and the re-told live turn were both drawn.
- **"Working on your app" stays where the agent actually is.** It was pinned to the top of the
  reply, so a build that thought again between steps pushed everything you had already read down
  the screen until it stopped.

### Notes

- The backend dependency stack was brought current and every floor pinned — pydantic-ai 2.37, and
  an Anthropic SDK that now carries its own fork of its HTTP client. That upgrade is what made the
  reasoning allowance reachable at all.
- A verification pass over this wave, with nothing to see on screen: six tests that the branch had
  left red or hanging are green, the test fixture that made a route's own rollback observable is
  scoped to the two tests that need it rather than reshaping all 3,700, and a handful of comments
  and docstrings that described mechanisms this wave deleted now describe what is there.

## [1.7.0-beta.5] - 2026-09-02

The project screen becomes the app.

### Added

- **Your app is on the project screen now, beside everything else about the project.** Opening a
  project shows the app on the right and its details on the left — the composer, what the app is
  doing, and the description. The app does not start because you opened the screen: it starts when
  you press **Launch Application**, once, and it stays running while you work.
- **The pane answers for every state, in plain language.** Saved and waiting for you, getting ready,
  running, another project is using your workspace (and which one, with a way to get there), and
  "we could not check". It never says an app is running on a signal that does not prove it is
  serving a page, and it never names a number nobody has measured.
- **You can choose a Plan chat or a Build chat before you type.** Until now the project screen could
  only ever start a Build chat — the other kind of chat existed but nothing in the product could
  reach it.
- **A Plan chat says everything the app pane would have said.** It has no pane, deliberately, and it
  is not silent about the workspace: the same sentence, in words, above the composer.
- **A warning before you leave the workspace with unsaved work**, on the ways out that the browser's
  own "are you sure" cannot cover — a link in the header, the breadcrumb, opening another project.
  Where the platform could not check, it says so instead of reassuring you.
- **A way past the app with the keyboard.** The app is somebody else's page inside a frame, and it
  swallows the Tab key; there is now a control that steps back out to the rail.

### Changed

- **You are asked before another project's app is stopped — every time, not only when there is
  something to lose.** It used to stop silently whenever the platform judged there was nothing at
  risk, which is a fair judgement about the work and the wrong one about the person. When the other
  project genuinely has nothing unsaved, the question says so plainly and offers no Save button for
  work that does not exist.
- **The question comes before the chat is created, not after.** Sending the first message in a new
  chat used to create the chat, then ask whether the workspace was free — so a refusal left an empty
  conversation behind, named after the message it had just refused. Nothing is created until the
  answer is known. *(Closes #161.)*
- **The switch dialog leads with the app you are starting**, not the one you are leaving, and both
  its buttons name the project whose changes are at stake. A non-technical audience could not tell
  which app "Switch without saving" applied to, and said so.
- One control collapses the details rail, and it lives beside the app so it is still reachable once
  the rail is hidden.

### Fixed

- **An unreadable answer from the platform can no longer cost you your work.** Pressing Launch
  Application on a container the platform could not reach fell into the branch written for "it is
  definitely gone", which tears the container down before restoring the last saved copy. It now
  refuses and offers a retry — the saved version is intact, and so is whatever was in the container.
- **Signing out when the sign-out fails now warns you where you can read it.** The warning said this
  browser might still hold a live session, then navigated away in the same instant and destroyed
  itself. Nobody had ever seen it.
- **The admin console tells a failure from a confirmation.** Both arrived looking identical, on the
  surface where being wrong costs the most, and a failure faded after three seconds. Failures now
  wait to be dismissed.
- A planning chat on a brand-new project now talks about what could be built, instead of stopping at
  what is there.

### Notes

- Sandbox-first turns a planning question, not just a save, into something that can create a build
  container — multiplying how often the fleet is built up — while the pass that would reclaim
  orphaned ones stays on its own deferred track (C10 §7), not shipped with this change.
- There is no feature flag. The primary project screen changes for everyone at once.
- A consolidation pass over this wave, with nothing to see on screen: the stop-then-save-then-release
  hand-over now has one home rather than two copies that had to agree, the short commit shown beside
  a saved version has one spelling again, and the project screen stops re-drawing its whole
  conversation list every forty-five seconds for a reading that had not changed. Typing in a chat no
  longer re-renders the page frame around it.

## [1.7.0-beta.4] - 2026-09-01

Finishing the removal, and putting back the guardrail it dropped.

### Added

- **A long chat now warns you before it stops working, and tells you why when it does.** As a
  conversation grows you get one line saying it is getting long and suggesting a new chat; past the
  limit your administrator set, the next message is refused with a sentence that says what happened,
  what to do, and — the part that actually matters — that your app and everything in it stays exactly
  as it is. Before this, a chat that got too long simply failed, with no warning and no reason.
- The limit is enforced on the **server**, at both places a chat can start a turn: an ordinary
  message and pressing "Build this plan". Enforcing it in the browser is what let it disappear
  silently in the first place.

### Changed

- **The per-conversation limits in the admin console do what their labels say.** "Per-conversation
  warn" and "Per-conversation max" saved cleanly and changed nothing — for anyone, since the release
  that deleted the old two-page chat. Both are now real, their help text describes what actually
  happens, and the note about when a change takes effect distinguishes the two: the max applies to
  the user's next message, the warning after their next reload.
- While the app is being refined, the overlay says **"Still working…"** rather than "Still
  iterating…".

### Removed

- **The legacy chat endpoint is gone.** There were two ways to run a chat turn; the portal stopped
  using the older one, and it has now been removed rather than left mounted. A second, unused path
  through the same work is a way around every limit the first one enforces — including the one this
  release restores.
- Six unused pieces of interface scaffolding and four dependencies that came with them, plus a chat
  helper that has had no callers since June.

### Fixed

- Around sixty comments across the codebase that described the portal as it was two releases ago —
  including four inside the new chat screen that referred to it by the name of the page it replaced,
  and one that would have led the next reader to delete a working feature. Guards in both the
  frontend and backend test suites now fail if a future removal leaves the same trail.
- Switching between chats could briefly show the previous conversation's "getting long" warning on
  the new one's composer.
- A message refused for length no longer quietly consumes a "Build this plan" card you had not
  pressed yet. It used to refuse the message and spend the offer at the same time, with nothing on
  screen saying the second thing had happened.

### Known limitations

- **The length warning does not yet count attached documents properly.** A PDF is measured as though
  it were a single image, so a conversation carrying several long documents can still pass the limit
  without warning and fail the way it did before. Chats made mostly of writing are measured
  correctly. Fixing this needs the page count recorded when a file is uploaded.
- An administrator can set a per-conversation maximum low enough to refuse every chat for that user,
  including a brand-new empty one. The lowest useful value is around 8,000; below that the person is
  locked out until the number is raised.

## [1.7.0-beta.3] - 2026-09-01

One chat surface, one publish chip, one agent voice. (#168, #170, #169)

### Added

- The agent can now say something **while it is working**, instead of only at the end. A build that
  runs for two minutes used to show a row of finished steps and no words; it can now send a short
  line as each piece starts or lands — *"Adding the status picker next to your search box now."*
  The line is bounded by the platform rather than by a request in its instructions, so it stays a
  line and cannot become an essay.
- When a single message asks for many separate things, the agent proposes what to build first:
  everything you asked for listed back, the two to four pieces it would start with, why those, and
  one question. Saying the whole thing back matters — a request for nine things answered with three
  reads as a refusal unless you can see all nine were heard.
- A build now ends by saying **what was agreed and not built**, taken from the list you agreed to
  rather than from the agent's recollection. Where the platform cannot tell which pieces landed, it
  says so instead of guessing.
- A single build is now bounded by what it **spends**, as well as by how many steps it takes and how
  long it runs. When it reaches that point it stops where the app works, keeps a copy of your work
  first, and tells you what is left.
- **"Take it back."** A version waiting with an administrator can be withdrawn from the chip with
  one confirmation, so you are not stuck behind a decision that is not yours to make. The chip also
  shows *when* a version was approved, not only which one.

### Changed

- **The chat is one screen now.** Planning a change and building it used to be two different pages
  that looked and behaved differently — different scrolling, a different composer, a different idea
  of what a message is. There is one surface, and which kind of chat you are in changes the
  placeholder and nothing else.
- **What the app is doing is written where it happened.** The build used to narrate itself in a card
  pinned to the bottom of the screen, which erased and rewrote itself as it went and left nothing
  behind. The steps now sit in the conversation, in order, grouped by the run they belong to, and
  they are still there next week. A run that hit a problem says so and opens itself; a clean one
  stays collapsed to a single line.
- **Stop moved to the composer**, where the rest of the controls are, and it no longer goes dead
  under your cursor when you press it.
- Attachments open over the conversation instead of in a new browser tab, so you keep your place and
  the reply keeps arriving behind them.
- The plan offer is a strip on the composer rather than a card in the transcript, and there is
  exactly one Build control on the screen.
- **Publishing is one chip beside your project's name.** The Publish card, the Review & approval
  card and the small button in the builder's toolbar are gone; there is one chip, it says where your
  app stands, and pressing it opens one sentence and — where there is something to do — exactly one
  button. Where there is nothing to do there is no button, rather than one that fails when pressed.
- **The chip stops guessing.** Every label is read from a single answer the server works out, instead
  of each screen recombining the same four or five fields and sometimes reaching a different
  conclusion. That guesswork had produced the same kind of mistake four times in this one feature,
  most recently promising "this can publish automatically" beside a Publish button moments before
  the app was sent to an administrator instead.
- **Being live and having newer unsaved work are now visibly different**, without opening anything:
  "Live", "Live · newer work saved", and — on the rare occasion the platform cannot check — a plain
  statement to that effect rather than a claim that nothing of yours is waiting.
- **"Taken offline" and "Switched off" are two different things again**, because they have two
  different remedies: a taken-offline app can be published straight back to the same address, and a
  switched-off one cannot be published at all until an administrator says so.
- The word "deploy", and the pipeline's own vocabulary — "Packaging your app", "Setting up the
  server" — are gone from everything you read. While a publish runs, the chip says "Starting up".
- **A plan now arrives in five named parts** — what this gives you, what you will see, what the app
  will remember, what stays exactly as it is, and what was assumed. The fourth is left out entirely
  for a first build, because there is nothing yet to leave alone. Nothing in a plan names a file, a
  folder, a framework or a command.
- The buttons under a plan are now **Build this plan** and **Keep planning**, and the agent uses the
  same two words. An agent telling you to press something the screen does not draw is a broken
  instruction at the moment you are being asked to decide.
- **One rule now governs the agent's words in both kinds of chat.** Anything written in the same
  breath as an action is the agent talking to itself on the way there, and it no longer reaches you
  in a planning chat either — which is where the 2,397-word reply of file paths came from. The
  consequence, stated plainly: **a planning answer no longer appears word by word.** It arrives
  whole when the agent finishes writing it, at the same moment it would have finished arriving
  before. Build chats have always worked this way.

### Fixed

- **A refused message is no longer lost.** If the server turned a message down — the daily limit
  reached, a build already running, the service unavailable — the composer had already emptied
  itself by the time the refusal arrived, and the text and any attached files were gone with no way
  to get them back. The box now empties only once the server has accepted the message. Two relatives
  of the same bug went with it: pressing Enter twice quickly no longer clears the box for the press
  it ignored, and a message refused while you were looking at another chat no longer leaves that
  chat unable to send anything for the rest of the session.
- When sending is refused, the reason is the one that applies — the daily limit, a build in
  progress, the attachment limit — instead of one generic sentence for all of them.
- **A file attached in one chat no longer follows you into the next one**, where it would have been
  sent to a conversation you never attached it to.
- **Attached spreadsheets and text files no longer print their whole contents into the
  conversation**, and attached images and PDFs show up at all.
- A turn can no longer end with nothing to read. Where the agent said nothing at all, the platform
  says so itself rather than leaving a screen of finished steps and silence.
- Screen readers are told what a run of work amounted to when it finishes, not only that it started
  — and are no longer read a summary of every past build when an old chat is opened.

## [1.7.0-beta.2] - 2026-09-01

The workspace owns the running app, and a chat is a plan chat or a build chat. (#166, #167)

### Added

- Pressing **Build this plan** now opens a new build chat that starts from the plan itself — the
  plan arrives as the first message, word for word, exactly as if it had been pasted in. The plan
  chat is left untouched, so a plan can be read again next week and built a second time,
  differently, without anything having to be undone.
- A plan chat can now answer questions about the app as it actually is. It used to answer from a
  saved copy, which could be days old; it reads the same running app a build chat does. Where there
  is nothing running to read, the message is refused with a sentence saying so rather than answered
  confidently from stale code.
- The warning about unsaved work now follows you across the whole workspace, not only while the
  build chat happens to be the thing on screen.

### Changed

- A chat is a **plan chat** or a **build chat**, chosen when it is created and never after. There is
  no mode pill, no switch part-way through, and no way to be in a conversation whose next message
  does something other than what the conversation is for. Two overlapping ideas with six labels
  between them became one idea with two.
- The running app is now part of the workspace itself rather than something each screen draws for
  itself. It used to exist only because the build chat was the page you happened to be on, which is
  why walking away from that page took the app down with it.
- Conversations are opened and deleted from the project page. The list that used to sit inside a
  chat — beside the chat you were already reading — is gone, so there is one place that answers
  "what is in this project" instead of two that could disagree.
- Taking the workspace no longer depends on what kind of chat asked for it. A planning question that
  needs the app running takes it on the same terms a build does, and is refused on the same terms
  when someone else holds it.
- Whether an app is published is now decided in one place on the server and read everywhere else,
  instead of each screen working it out from parts and occasionally disagreeing.

### Fixed

- **Walking away from a build chat no longer disturbs the running app.** Going to the project page
  and back reloaded it; leaving mid-build reloaded it, because the pane read the departure as "the
  turn just finished"; and leaving right after a build succeeded shut it down altogether — at the
  moment a citizen is most likely to walk away.
- **The Build button can no longer appear underneath something that was never a plan.** The offer is
  tied to the plan the agent actually wrote, because the plan travels inside the offer itself;
  nothing reads the agent's prose to guess whether a plan happened.
- Starting a build from a plan can no longer leave behind an empty chat with no message in it. The
  conversation and its first message are written together, so a failure part-way through leaves
  nothing rather than a shell you cannot use or explain.
- A plan is never quietly cut short. An overlong plan is refused with a reason instead of being
  trimmed to fit and offered as though it were complete.
- A first message that fails to save no longer takes the typed text with it. The notice asking you
  to try again appeared over an empty box, offering a retry of something that no longer existed on
  screen or in storage.
- Screens inside the workspace are no longer clipped. Each fills the space it is given instead of
  asserting a full window height of its own, and the spinner shown while a conversation loads is no
  longer pushed below centre and cut off by the navigation bar.

### Removed

- The mid-conversation mode switch and the endpoint behind it. A chat's kind is fixed at creation,
  so there is nothing to switch.
- The warning that a plan had gone stale, and the override that let you build anyway. They asked the
  citizen to adjudicate something the platform could not actually tell them.

## [1.7.0-beta.1] - 2026-08-31

Conversations say which kind they are, and the platform starts measuring. (#165)

### Added

- Every conversation on a project page now says which kind it is, in words. The list drew two
  different icons and never named either, so telling a Plan chat from a Build chat meant knowing
  what a wrench stands for. Rows now read "Build", "Plan" or "Chat", and a screen reader hears the
  whole phrase rather than a bare noun.
- The platform started measuring four things about the citizen's journey that nobody could
  previously observe: how long a cold start actually takes, how many starts reach a running app, how
  long it takes to first see your app after opening a project, and how often a project is opened
  without any chat being opened. Until now every claim about any of these was unfalsifiable. An
  administrator reads them through the counters endpoint that already existed; the two only a
  browser can see arrive through a narrow endpoint that stores no record of who sent them. No number
  reaches a citizen's screen.

### Fixed

- A conversation row could be labelled the wrong kind, or crash instead of rendering at all. The old
  test knew about one of the three kinds, so an assistant chat was labelled a plan; separately, a
  kind whose name collided with a built-in JavaScript property took the whole row down. Both were
  reachable from ordinary API data.
- A measurement failing can no longer slow down or break the thing it is measuring. The counter
  writes moved out from under the lock that a citizen's next build waits on, and a measurement that
  throws is contained instead of taking the builder's screen down with it.

## [1.6.19] - 2026-08-29

### Added

- A Marketplace of every app published at BIAL, reachable from the header and open to
  everyone who is signed in. Until now an app could only be opened by someone who had
  been sent its link, so there was no way to answer "has anyone already built this?" —
  and people spent build time recreating tools that were already running.
- Search the Marketplace by what an app does. What you type is matched against the app's
  description and results come back best-match-first rather than newest-first; quoted
  phrases and negation behave the way they do in an ordinary search box. An app whose
  description is empty still appears in the full list, but cannot be found by typing —
  descriptions are not yet written automatically.
- Apps join the Marketplace on their own once they are live. There is no listing step and
  no setting anyone has to remember, and unpublishing an app takes it back out. Each entry
  shows the app's name, its description, who built it, and a button that opens it.

### Removed

- The header controls that did nothing when clicked: the "Search pages or actions" box,
  whose entire index was three links already sitting in the nav above it; the notification
  bell, which only ever repeated a count already shown on the Admin link beside it; the
  settings gear, whose four items all produced the same "Coming soon" message; and
  "My Profile" under your own avatar, which was the same placeholder.
- The theme picker in the project builder. Picking a theme changed the label on the picker
  and nothing else — no part of the app that got built ever read the choice.
- PowerPoint (.pptx) from the attachment picker. A deck could be attached but never sent:
  every attempt failed at send time, so the option offered nothing but the failure.

### Changed

- Four Help Center answers described behaviour the product does not have. They now match
  what it actually does.

### Fixed

- The backend test suite can now be run from a Windows checkout. Fifteen tests were failing
  there for a reason that had nothing to do with what they were testing, which made a
  perfectly healthy checkout look broken.

## [1.6.18] - 2026-08-26

### Added

- Generated apps now open at one BIAL address, with the app's own key in the link. Before
  this, every app was handed an address that only resolved outside the BIAL network, so a
  preview or a shared app link opened to nothing from a BIAL desk.
- A link to an app that cannot be reached shows a plain "this app is not available at this
  address" page with a button back to the portal, instead of a raw server error.
- Apps published before this change can be moved onto the new address by a one-off script.
  It refuses to rewrite an app's link until that app has actually been published again, so a
  link that plainly does not resolve is never swapped for one that returns a confusing error.

### Changed

- Moving around inside an app keeps the app's key in the address, so a shared or bookmarked
  link still lands where it should instead of dropping out of the app.
- The portal now only allows an app to be shown inside it from the shared app address.

### Fixed

- Live reload inside a preview works again. It had been connecting to an address the
  framework no longer serves, so a preview could quietly stop updating between changes while
  still looking healthy.
- A request that tries to change your data from somewhere other than the portal is now
  refused, which matters because apps and the portal now share a site.

## [1.6.17] - 2026-08-24

### Changed

- The assistant now writes for the person who asked for the app, not for a developer. File
  names, commands, and library names stay out of the chat, and build steps read as "Building
  your app's main page" instead of a file path.
- A build answers the moment you send it rather than sitting still while its sandbox starts.
  An operation that runs long says what it is doing and clears itself when it finishes.
- A finished build ends with a short list of what you can now do with your app.
- Changing where your app stores information is one operation instead of two commands, so a
  change can no longer half-apply or stall waiting on a question nobody can answer.
- Long command output is trimmed by usefulness, with a way to read the part that was left
  out, instead of flooding a build with dependency-manager chatter.
- The starting template no longer ships a demonstration data model or example component to
  work around.

### Fixed

- Attaching a slide deck no longer counts tokens against your daily usage before you have
  sent a message.
- "Build complete" is refused when the app is not actually working, so it can no longer
  appear over an app that will not build.
- A workspace that matches its last save but still holds uncommitted work is no longer
  treated as safe to reclaim.
- Trimmed command output can no longer reveal the inside of a credential, from either end of
  the capture.
- A build that fails partway now explains itself in plain words and says what happens next.

### Removed

- Retired internal surfaces that no longer had callers: the browser build lock and its
  heartbeat, an unreachable supervisor route, and a progress frame nothing produced.

## [1.6.16] - 2026-08-23

Everything below comes from one demo. On 18 August the platform destroyed a finished app twice,
told a client the build was complete while the untouched starting template sat on screen for nine
minutes, and filled the preview with a full-screen error page in each of three builds. This
release is the answer to all three.

### Your app cannot be quietly replaced by an empty one

A workspace that had reset itself to a blank template looked, to every check the platform had,
exactly like a project nobody had built yet. So the assistant built on the blank template, and the
automatic backup then saved the blank template over the real one.

Before anything runs now, the platform checks whether your workspace still holds your app. If it
finds it has been wiped, it tells you, sets the current state aside, puts your app back from the
last copy it kept, and asks you to send your message again — because what you typed was written
about an app that is no longer there.

**It only acts when it is sure.** Three separate things have to agree before the platform will
replace a workspace, and "we could not check" is never one of them. If a check cannot be answered,
your app is left exactly as it is and you are told plainly — and if the check keeps failing, the
platform stops asking rather than locking you out of your own project.

**A backup can no longer overwrite good work with bad.** The automatic copy taken at the end of
every message asks one question first: was this built on top of the copy it is about to replace?
If not, it keeps both and tells an administrator, instead of overwriting.

**If your app stops running while you are reading, you find out.** Until now that was only caught
the next time you sent a message — which might be never. The preview checks on its own, says your
app stopped running and will be brought back, and strikes through the "your app is finished"
message that is no longer true, rather than deleting it as if it had never been said.

### "Build complete" only appears when your app is actually your app

Every check the platform had came back green on that demo, because every one of them was asking
"is an app running here" and none of them was asking "is it theirs".

Two questions decide it now. The platform loads your app's home page the way your browser would,
so a page that answers with an error can no longer pass. And it compares that home page against
the one your workspace was created with — if they are still identical, nothing you asked for is on
the page you actually look at, and the build is not finished. This works on every app that already
exists: it is a fact about your app's own history, not a marker the platform had to add.

**A change that cannot be finished now ends, and says what you are looking at.** It used to end by
saying your app "still has an error" and leaving you to work out what was on screen — or worse, to
leave the preview saying "putting the latest change together…" for as long as you left the tab
open, so the only way to learn it had stopped was to wait long enough to stop believing it. It now
tells you which of three things your app is currently showing: the starting template, an earlier
version of itself, or nothing yet. Those are different situations and they change what you would
do next.

**If your app breaks in the browser, the platform notices.** An app can answer every check the
platform makes and still die the moment it renders. Your app's own errors now reach the platform,
and a build that crashes in the browser is not reported as finished. Ordinary browser warnings do
not count as a crash — a missing key in a list is not a broken app.

### The preview stops lying about your app

**The red error screen is gone.** When a change does not compile, the preview used to fill with the
framework's own full-screen error page — file paths, stack frames, the lot. It now shows a calm
"Putting the latest change together…" card instead, and clears the moment your app compiles. This
works on apps you built weeks ago, not just new ones: the platform covers the preview from the
outside, so nothing about your app has to change.

The card only ever comes down when the platform has actually confirmed your app compiles. If it
cannot tell — the workspace is still starting, the connection dropped, anything at all — it keeps
the card up rather than guessing. Reloading the page after a change that did not come together
used to bring the error screen straight back, labelled as though the app were running fine. It no
longer does.

### Fewer wasted rebuilds, and a shorter wait

Two of the demo's four repair rounds were the platform re-reporting problems the assistant had
already fixed, because it was reading a log that outlived the fix. It now checks whether anything
changed since the error was recorded and, if so, looks again before spending another rebuild on it.

The platform also used to treat "I could not check" and "it is broken" as the same answer, so a
slow-starting app was reported to the assistant as a fault and cost you a rebuild chasing it. It
now asks again instead, and only says something is wrong when it actually found something wrong.

**The assistant is told what your app is doing before it answers you.** If you said your app was
broken, it used to answer from the conversation — where your app had been working — because that
was the only account of your app it had. It is now handed the current state of your workspace on
every message.

### When your daily budget runs out, your app is secured first

The old message told you to click Save and secured nothing itself. It now keeps a copy before it
tells you, says whether that worked, says when you can carry on, and gives you a real address to
write to if you need more before then. Your draft stays in the box.

### For administrators

The outcomes this work is judged on are counted where you can read them — completion claims
blocked, restores performed, and backups that did not land. Any workspace the platform sets aside
is listed and can be put back by a named procedure, rather than sitting in storage with nothing
able to reach it.

**Before deploying this release**, set `SUPPORT_CONTACT_EMAIL` in the App Service configuration.
It has no default and the app will refuse to start without it — deliberately, so the at-limit
message can never name a contact nobody reads.

## [1.6.15] - 2026-08-21

**Before an app goes live, the platform now reads its code and asks what kind of data it
handles.** Press Publish and an automatic check reads the version you last saved, answers six
questions about it — credentials, health data, personal information, financial data, confidential
business data, public data — and fills the form in for you, with its reason in plain language
beside each answer. You can change any answer you disagree with. Nothing sensitive found means the
app publishes on its own. Anything sensitive means it goes to an administrator instead, with your
explanation attached, and you publish that exact version yourself once they approve.

The check usually takes about twenty seconds. You can close the dialog and come back — the result
is kept against that version, so re-opening it costs nothing and re-running only happens when you
save new code.

**Administrators get a queue that leads with the disagreement.** The review screen opens on the
categories where you and the automatic check disagreed, naming who said what and which answer went
on record, followed by everything you declared and the explanation you wrote. Rejecting requires a
note, because that note is the only thing that comes back to you.

**An administrator can now take a published app off the air.** One action removes the running
container and the address stops serving. Nothing is destroyed — the app's code, its own database
and its files are untouched, and publishing again brings it back at the same address. The owner is
told what happened: where the app said **Live** it now says **Taken down**, with a line explaining
that an administrator did it and that a new Publish restores it. There is no button for this in the
admin console yet — the endpoint and the owner-facing half ship here, the console control follows.

### Added

- Pre-publish data-classification review: a model-free credential scan followed by an AI review of
  the saved code, six verdicts with plain-language reasons, stored once per app and stamped with
  the version it examined.
- Publish gate as a precedence ladder: every combination of app state, review state and declared
  answers resolves to exactly one outcome — publish, route to an administrator, or refuse — and
  says which.
- Admin review screen leading with disputed categories, plus a waiting-count badge in the nav so a
  queued app cannot sit unnoticed.
- Withdraw a submission from the queue, and a single route into the queue for both lineages.
- Review spend is metered separately and never counted against a citizen's daily token budget.
- Plain-language names for every audit action, so the trail reads as events rather than column
  names.
- Evaluation harness for measuring the review's accuracy, budgets and scan precision against a
  labelled corpus.
- **Take a published app off the air** (superadmin): removes the running container and marks that
  deployment as taken down. The accountability record is written before Azure is touched, so a
  timeout part-way through still records who acted. The app row, its database and its files are
  never named on this path.
- **Automated checks now cover the backend**, which had none at all: formatting, linting and all
  three type checkers run on every pull request, plus a check that the database migration history
  has a single head — the failure that otherwise surfaces at deploy time, once two branches have
  each added a migration on the same parent. The backend test suite still runs by hand; it needs a
  prepared database, and a check that fails by default would block every pull request.
- **Regression tests for two things that had none**: both Save endpoints (ownership, cross-user
  scoping, CSRF, conflict handling, and what actually reaches the layer beneath), and the
  crafted-sign-in-link fix released in 1.6.9 — plus the admin limits panel's "0 (default)"
  placeholder, which no test had been checking. Each was verified by deliberately breaking the
  code it covers, so a silent revert turns a named test red rather than passing unnoticed.

### Changed

- The pre-publish score now reflects the answer of record — your answers merged with the automatic
  check's — instead of your answers alone. Either side may raise a flag and neither may lower the
  other's.
- Where you and the check disagree, the form names which answer goes on record rather than
  claiming yours is always kept.
- Approval and rejection state now reaches you even where automatic deployment is not configured.
- Internal identifiers no longer appear in the admin app list, review dialog, or audit trail.
- Everywhere the portal said an app was live, it now asks whether the app is reachable *now*
  rather than whether its last deployment succeeded — a deployment that has since been taken down
  keeps that status forever. A taken-down app shows no link at all, because a dead link is
  indistinguishable from an app that has broken.
- The admin review dialog now describes the review that is actually possible: the submission
  details on screen, and the fact that approving pins that exact submission and does not deploy it.
  It previously told reviewers to download and inspect the bundle first.

### Fixed

- A pipeline re-check on save-and-publish weighed only newly-raised findings, so an app could go
  live on a category the developer had themselves declared.
- Three surfaces reported state that had moved on: an epoch date above the approve button, a
  self-publish promise on apps the gate would still refuse, and a queue badge that never refreshed.
- **The scheduled background worker consumed nothing.** It started, connected and reported healthy,
  then failed on every poll: its queue keys were spread across shards of the managed Redis, and the
  transaction it issues on each poll requires them to sit on one. The keys are now pinned together.
  A single-node Redis puts every key on one shard and cannot reproduce this, which is why it
  appeared only in production.
- **A preview could load and then never finish starting up** once apps are served from a
  `bialairport.com` address: the workspace's web server silently blocks pages requested from an
  origin it was not told about, so the browser gets a page and then a loading screen that never
  clears. The new address is now allowed. This lives in the workspace image, so it takes effect at
  the next rebuild.
- **The portal's own test suite failed on a stopwatch**, naming a different set of tests on each
  run and going green on a re-run. Its time limit is a wall clock the whole parallel run competes
  for, so heavy tests crossed it having done nothing wrong. No test was skipped and no assertion
  was changed.

### Removed

- The admin console's **Download bundle** button, which had never worked in any browser: the call
  behind it returns nothing by specification, so the button's own "pop-up blocked" warning fired on
  every click. Removed rather than repaired. Administrators still have no in-product way to inspect
  a build before approving it; that gap is tracked separately.

*Recorded 23 August. The takedown control, the backend checks, the worker and preview fixes, the
retired Download bundle button and the new Save and sign-in tests all shipped in this release but
were left out of this entry when it was written.*

## [1.6.14] - 2026-08-17

**A failed build no longer leaves you watching a spinner that will never stop.** When a workspace
could not be started, the chat could keep animating "Setting up your sandbox…" with a running
clock and a Stop button for minutes — on work that had already finished failing, usually within a
second. The reply was not lost and nothing was left running behind the scenes; the page simply had
no way to find out it was over. It now gives up within a minute and says so, instead of animating
indefinitely.

**The same fault was also what disabled the Ask/Plan/Build switch.** While the page believed a turn
was still in flight, the mode control stayed greyed out and would not respond. Both were the one
problem, and both are resolved together.

### Fixed

- The chat could show a live progress spinner, an elapsed-time counter and a Stop button
  indefinitely after a turn had already failed. The page waits for the server's event stream, and
  its one-minute inactivity guard only covered data *arriving on* an open connection — never the
  request that opens it. A connection that was accepted and then went quiet was therefore waited on
  forever, and the page's "the turn is over" step sat behind that wait. The guard now covers both,
  so every outcome is reached in bounded time.
- The Ask/Plan/Build mode switch stayed disabled and unresponsive after such a failure — the same
  cause, fixed by the same change.

## [1.6.13] - 2026-08-14

**Saving and publishing an app works again.** The control plane stores a copy of your code by
shelling out to `git`, and `git` was not present in the image it runs in — so that step failed
whenever it was reached. Everything else continued to work, which is why this surfaced as an
occasional failure on one path rather than an outage.

**A second fault was caught before it ever shipped.** Since v1.6.12 the build workspace's web
server configuration has been invalid: a logging directive sat inside a block that cannot contain
it, so the server would refuse to start. It never reached anyone, because the workspace image has
not been rebuilt since that change landed — the running image predates it. The first rebuild
without this fix would have broken every new workspace. It is corrected here.

**Every container the platform ships has moved to a supported, patched base.** The operating
system underneath the build workspace and the published apps had stopped receiving security
updates in July, and the other two images were running older bases than they needed to. All four
now sit on current, patched foundations, with the versions pinned so a rebuild produces the same
result rather than quietly drifting.

**When a build fails, the message now names the actual error.** Previously the headline could be
the framework's startup banner or a progress line, which told you nothing about what went wrong.

### Fixed

- Saving and publishing an app failed: the control plane shells out to `git` to store a snapshot
  of your code, and its image did not contain `git`. The failure now also explains itself if the
  binary is ever absent again, instead of surfacing as an unhandled crash.
- The build workspace's web server configuration has been invalid since v1.6.12 — a logging
  directive inside a block that cannot contain it, which makes the server refuse to start.
  Latent, not live: the workspace image has not been rebuilt since, so the running image does not
  contain it. Fixed before the next rebuild could surface it.
- A failed build could be titled with a spinner, a framework banner, or a configuration notice
  rather than the compiler error. Where no real diagnostic exists, it now says so instead of
  inventing one from whatever line happened to be first.

### Changed

- The build workspace moved to Debian 13, which is the current supported release; Debian 12 left
  regular security support on 12 July 2026. Its web server, package manager and Python runtime
  moved to current versions at the same time.
- Published apps are built on the same Debian 13 base, pinned by digest so the workspace an app
  is built in and the image it runs in cannot drift apart.
- The control-plane image moved to a smaller Alpine base, and the portal's web layer to a slimmer
  runtime, both pinned.
- Two duplicated build dependencies inside the workspace were collapsed onto single patched
  versions, removing the older copies that a scan would otherwise keep reporting.

### Added

- Line-ending guards for the files that ship into containers, so a checkout on Windows cannot
  produce an image that fails to start.
- Internal delivery tooling that reconciles a post-remediation vulnerability scan against the
  original one and produces the remediation report, refusing to run rather than reporting a
  reduction it cannot actually measure.

## [1.6.12] - 2026-08-12

**Idle build workspaces can now be cleaned up on their own, and the platform will tell you what
it would remove before it removes anything.** Every workspace the platform starts is a container
that costs money until something stops it. Until now nothing did, and a workspace nobody could
account for stayed running indefinitely.

**This ships switched off.** Reclamation does nothing until an operator turns it on, and turning
it on is two separate switches: one to let it look and report, a second to let it act. There is
no configuration in which it starts deleting because someone enabled a single flag. An admin
endpoint answers "what would you remove right now?" so the list can be read and agreed with
before anything is destroyed.

Nothing is removed until the work in it is provably saved somewhere durable, a workspace someone
is actively using is protected by a live signal from the build itself, and anything the platform
cannot confidently identify is escalated for a human rather than deleted. Each pass removes at
most a handful of workspaces, and every candidate is re-checked immediately before it goes.

### Added

- Fleet reclamation (ADR-0029): a scheduled pass that enumerates the Azure fleet, classifies each
  container by confidence tier, and — once both flags are on — stages and destroys only what it
  can prove is abandoned. Ships with both flags off in every environment.
- `POST /v1/admin/apps/reclamation-report` — a read-only answer to "what would the pass delete
  right now?", including per-candidate tier, verdict and reason.
- An admin lever for deploy reconciliation, which previously had no operator control.
- Identity tags stamped on every sandbox container at creation, plus a backfill for the
  pre-existing fleet, so a container can be attributed to the control plane that owns it.
- A background worker (Taskiq broker, scheduler and entrypoint) that runs the scheduled passes.
- A wall-clock liveness lease published by the turn engine, so an active build cannot be reclaimed
  out from under the person using it.
- The 24-hour drain, shipped flag-off and documented as such.

### Changed

- The live preview answers four distinct states instead of one boolean, so an asleep workspace
  reads as asleep rather than as an error.
- Redis sandbox keys are namespaced by environment, so one environment can no longer read or
  delete another's records. Landed with a dual-read window so no running fleet was lost.
- Settings are now one manifest per process (`api.py`, `worker.py`) instead of a mixin layer.
  A background process no longer has to satisfy the union of everything the API needs, and the
  worker hard-requires the three capabilities it cannot safely run without.
- Deploy reconciliation moved onto the scheduler.

### Fixed

- The sandbox reaper now validates a container name before it becomes an ARM delete.
- Tag updates merge instead of replacing — the Azure provider replaces the whole tag map on PATCH
  despite documenting merge semantics, which silently dropped identity tags.
- The preview poll no longer ends on half an answer, and overlapping probes no longer let the
  stale one win.
- Restore no longer manufactures containers with no identity.
- Two flag defects: one that would have stopped sandbox reaping entirely on deploy, and one
  switch that did nothing.

**The publish gate no longer works backwards.** Since 1.6.10, answering the data-classification
questions honestly had the opposite effect to the one intended: an app that declared credentials
or confidential business data was published automatically with nobody looking at it, while an app
that declared nothing sensitive was refused — and the refusal then nudged you toward declaring
more in order to get published. Only a fully clean declaration publishes on its own now; anything
else goes to a person.

Published apps still have **no sign-in of their own** — that half of the 1.6.10 warning stands.
Anyone with the address can open one and read or change its data.

### Added
- **Global Limits, in the admin console.** Set the daily token limit for everyone at once, or for
  a chosen set of people, instead of editing one person at a time. Applying to a chosen set records
  the previous limits first, so it can be put back; applying to **everyone** records only a count
  and **cannot be undone**.

### Changed
- **The build rail shows one step at a time.** It used to list every step at once and grow without
  bound, which made the thing actually happening the hardest thing to find. The step on screen is
  now the one genuinely running, a step that fails is surfaced rather than buried, and the full
  history moves into a dropdown once the build ends.
- **Assistant replies render as formatted text** — headings, lists, tables and code, with line
  breaks kept. Links to other sites open in a new tab; links within the page do not. Images are
  deliberately not rendered: the text is written by the model, and an image would silently call
  out to whatever address it named.

### Fixed
- **A refused publish now explains itself, and the explanation is kept.** The note you are required
  to write is recorded with the refusal instead of being thrown away, and the message no longer
  says "an administrator will review it" — nothing performed that review. It now tells you to ask
  one.
- **The deployment docs no longer claim published apps are internal-only.** That claim was never
  verified. They now state what is actually known, mark the network posture as unconfirmed, and
  give the command to settle it — while assuming the riskier answer until someone does.

### Security
- **The data-classification gate refuses what it used to publish.** The comparison ran the wrong
  way round, so the most sensitive declarations were exactly the ones that skipped review. Any
  weighted category now routes to a human, and an incomplete declaration is rejected outright
  rather than scored as though every unanswered question were a "no".

## [1.6.10] - 2026-08-10

**An app can go live in one click.** Answer six questions about the data it handles, press
Publish, and the app is built into a real image and deployed to its own container — no admin
step in between. It keeps the database and files it had while you were building it, and the
address stays the same every time you republish.

Two things to know before turning this on. Published apps have **no sign-in of their own**:
anyone with the address can open one and read or change its data. And the question set
currently runs the wrong way round — an app that declares sensitive data is published
automatically, while one that declares nothing is refused. Both are being fixed.

*(Corrected in 1.6.11: the question-set polarity described above was fixed in #117, so the gate
now refuses what this release published. The "internal-only" recommendation this entry originally
carried was itself unverified and was removed from the paragraph above — see #117's notes on
`deploy/config.py`'s `ingress` field for the hedged posture that replaced it. Everything else here
stands as written, as the record of what 1.6.10 shipped.)*

### Added
- **Publish, from the builder or the project page.** The button sits next to Save the moment a
  build finishes, and a fuller card on the project page shows progress and any failure in
  detail. Both are driven by the same underlying flow, so they cannot disagree about what
  publishing means.
- **A short data-classification form before anything goes live.** Six yes/no questions —
  credentials, health data, personal information, financial data, confidential business data,
  public data — plus an explanation box that becomes required once the answers pass a
  threshold. The answers and the score are recorded against the deployment that used them.
- **A record of every publish attempt**, including what was declared, what was built, and
  which image is actually running. Only one publish per app can be in flight at a time.

### Changed
- **Publishing no longer goes through the approval queue.** The existing submit, approve,
  reject and disable controls are untouched; this is a second, separate route that does not
  use them.

## [1.6.9] - 2026-08-10

**Losing a container no longer loses your work.** Between saves, the only copy of your app used
to live inside the running container, so anything that removed it — a closed tab, a crash, a
restart — took the work with it, behind a screen that said everything had been recovered. The
platform now keeps a copy after every turn, so at worst you lose the last one.

Separately, the whole portal is now typed, and for the first time a pull request cannot merge
without the checks having run.

### Added
- **Stop a build, then decide.** Switching project while the AI was writing used to offer a
  choice the server then refused, and one of the two buttons quietly stored a half-written
  version as the one you would get back. You are now offered Stop and save, Stop without
  saving, or Keep building — and each does what it says.
- **A recovery copy of every turn's work**, kept separate from the version you deliberately
  saved. Bringing it back is resuming, not saving: your saved version is untouched and Save is
  still your decision.
- **Automated checks on every pull request** — types, linting and tests. Nothing ran before.

### Changed
- **The whole portal is TypeScript** under strict checking. Behaviour is unchanged; the point is
  that a whole class of mistake is now caught before it reaches you.

### Fixed
- **Saving during a build refuses instead of storing something half-written.** The refusal
  explains itself rather than failing silently.
- **A restore fetches your work before removing the old container**, not after — so a failure
  partway through can no longer leave you with neither.
- **A question is no longer treated as a build.** Asking something read-only used to report that
  your app was still being built and block Save while you waited for the answer.
- **The dashboard greeting works.** It read two fields the server never sends, so it always fell
  back to the generic text.

### Security
- **A crafted sign-in link can no longer break the page.** A specially-named `authError` value
  in the URL could blank the sign-in screen for whoever opened it.
- **An unknown page origin is now treated as unknown**, rather than as a site literally named
  "null".

## [1.6.8] - 2026-08-06

**Apps the AI builds now work on a phone from the start, and the admin user list is a table you
can actually use.** Airport staff work from phones, and apps used to run off the right edge of
the screen; the starter every app is built from now handles small screens properly.

### Added
- **Sort, page and filter the admin user list.** Filter by role and by status, sort on any
  column, and move through pages instead of loading every user at once.
- **Reset a user's usage** from the same table, with the reset recorded in the audit trail.
- **A worked mobile example in the app starter** — a real page with a table, a form and a
  toolbar that all behave at phone width. The AI is pointed at it, so new apps follow the same
  pattern instead of inventing one.

### Changed
- **The app starter is mobile-ready.** A new app no longer scrolls sideways on a phone, and
  content stacks instead of running off the screen.

## [1.6.7] - 2026-08-05

**Opening a second project can no longer throw away the first one's unsaved work.** Only one app
can be running at a time, and starting a second used to quietly shut the first down. If it held
work you had not saved, that work was gone — and the screen still implied it had been recovered.

### Added
- **Save and switch.** When another project is holding the workspace, you are told which one and
  offered a choice that works: save it first, switch without saving, or cancel. What you were
  trying to do is retried for you afterwards.
- **A view of sandbox containers nothing is tracking**, so one that has been abandoned can be
  found and reclaimed instead of running up cost unnoticed.

### Changed
- **Starting a second app refuses rather than destroying the first.** The refusal names the
  project in the way and says what to do about it.

### Fixed
- **A preview whose container has been taken no longer pretends to be live.** It says the app is
  no longer running and offers to bring it back, rather than showing a frame that will never
  load.

## [1.6.6] - 2026-08-02

**The build screen gives you room to work: the preview can take the full width, it can be checked
at a real tablet or phone size, and a finished session no longer blanks out the pane.** The
project description also moves out of the rail into a proper editor you open when you need it.

### Added
- **Tablet and phone preview widths.** The preview toggle is now Desktop / Tablet / Mobile, and
  each mode sizes the preview to a real device width (834px tablet, 390px phone) rather than just
  narrowing a container — so your app's own responsive layout genuinely reflows, instead of
  looking like a squeezed browser window.
- **The chat panel can be hidden**, handing the whole width to the preview. The panel is only
  hidden, never discarded: a half-typed message and your place in the conversation are both still
  there when you bring it back. If something needs you while it is hidden — a session reclaim, a
  quota warning, an error — the toggle carries a dot rather than letting it pass unseen.
- **A pop-up editor for the project description.** The rail now shows the description as plain
  readable text with an Edit button, instead of an always-open text box. Save closes the editor;
  Cancel discards what you typed.

### Changed
- **A finished build session shows a small card, not a dead pane.** Ending a session used to
  replace the whole preview with a full-height "no longer running" block. It is now a compact
  card, with the same Relaunch button on the same terms.

### Fixed
- **The description editor is properly keyboard-operable.** Escape closes it, focus returns to
  the Edit button afterwards, and Tab can no longer escape onto the page behind it while a save
  or a generate is in flight.
- **The description editor opens above the navigation bar** instead of being painted underneath it.
- **A hidden chat panel is genuinely out of the way.** Its text box and buttons used to stay
  keyboard-reachable while invisible, so tabbing landed on controls no one could see.
- **The chat toggle no longer sits on top of the preview's device buttons.**

## [1.6.5] - 2026-08-02

**Coming back to an app you built now takes under a second instead of a minute — and pressing
Relaunch can no longer throw away work you hadn't saved.** Reopening a saved app used to rebuild
the whole container from scratch (57.8s); it now reattaches to the one already running (381ms
median). Along the way two paths were found that silently rolled a workspace back to its last
save: one needed a slow home page and two clicks, the other fired on every deploy.

### Fixed
- **Relaunch no longer discards unsaved work.** If your app's home page was slow to load, the
  platform decided the container was dead, and the next press rebuilt it from your last save —
  losing everything since, with nothing on screen to say so. A slow page is now understood as a
  slow page: the container is kept, and relaunch hands back the preview with an honest "not
  serving yet" rather than an error that invited the fatal second click.
- **Deploying no longer rolls open workspaces back.** The credential the control plane uses to
  talk to a sandbox lived only in memory, so restarting the service made every running sandbox
  look gone and rebuilt it from the last save. It is now recovered from the container itself —
  the first relaunch after a restart reattaches in ~3s instead of rebuilding in ~66s.
- **A slow relaunch says what is happening.** It could spin on an unlabelled "Restoring your
  app…" for minutes and then fail. It now tells you it is taking longer than usual, and that your
  work is safe while it waits.
- **A repaired app stops showing you the broken version.** After the assistant fixed something,
  the preview could keep displaying the old render with no way to refresh it. It now re-requests
  the page when a turn finishes, and there is a Reload button for when you want it.
- **"Build complete" can no longer appear over an app that was never built.** A build that wrote
  no files — or that simply declared itself finished — now ends as a failure that says so.
- **Saving reveals the Relaunch button.** Saving is what makes a relaunch possible, but the button
  stayed hidden until you reloaded the page.
- **Compile errors reach the repair loop.** Errors that only appeared in the dev server's own
  output were invisible to self-heal, so the assistant could not fix what it could not see.
- **The preview appears when the app is genuinely ready.** "Ready" now means a request was
  actually served, not merely that a process started, so the pane stops framing a page that is
  not there yet.
- **New chats get correctly ordered ids, and opening one stops asking the server for a record it
  knows does not exist yet.** The ids are time-sortable again, and a fresh chat no longer starts
  with a guaranteed-to-fail lookup.

### Added
- **Abandoned sandboxes are reclaimed on a schedule.** A container whose owner never came back
  used to run — and bill — until someone else happened to start a build. A sweep now collects it
  automatically. Single-replica deployments only; see the note in `reaper.py` before scaling out.

### Changed
- **Relaunch reattaches instead of rebuilding.** The common "come back to my app" path reuses the
  running container: 57.8s to 381ms median.
- **Sandboxes start faster and weigh less.** The image dropped from 719MB to 343MB, and explicit
  startup probes removed the fixed grace period every new sandbox used to pay.
- **The preview reveals when the page loads, not on a timer**, so the app appears when it is
  actually there.

## [1.6.4] - 2026-07-30

**Your daily token budget now reflects what a build actually costs — and builds no longer
die chasing a dev server that just needed a restart.** One calculator build used to book
~956k of the 1M daily cap (96% of it cached re-reads billed at full price) and still end
with no preview; the same build now books ~145k and recovers on its own.

### Fixed
- **Cache tokens no longer eat your daily budget.** The cap bills each token class at its
  real cost: fresh input and output at face value, cache reads at 10%, cache writes at
  125%. The weighting is read-side policy over the untouched raw ledger, so past days'
  displayed usage is corrected too — in the header meter, the daily gate, and the admin
  roster alike.
- **A dead dev server gets restarted, not misdiagnosed.** When the sandbox dev process
  dies (an out-of-memory kill, a startup crash), the build verifier now captures its last
  output and exit code, relaunches it, and only then re-checks readiness — instead of
  burning the entire self-heal budget telling the assistant to fix a rendering bug that
  did not exist.
- **Honest diagnostics when the server cannot be revived.** The build error now names the
  process failure and exit code, with the server's last output attached, rather than
  guessing the app "throws during render". The sandbox supervisor's status endpoint
  reports the dead process's exit code, so an OOM kill is distinguishable from a code
  crash.
- **No more Next.js dev badge floating over your app.** New apps no longer show the framework's
  floating dev-tools button, which turned into a red error counter on a rendering glitch and read
  as "your app is broken" to people who had no way to act on it. The platform still sees those
  errors — they reach the build assistant through the dev server's log, not the badge. Apps built
  before this change keep their own copy of the setting and are unaffected.

## [1.6.3] - 2026-07-30

**Chat is now one continuous conversation with three tool levels — and the composer never
locks you out.** Ask, Plan, and Write are the same agent on the same message history over the
same live workspace; switching modes just changes what the assistant is allowed to do. Write
no longer requires the Build-it button — pick it and say what you want. Build it remains the
convenient route for multi-step work: it switches the mode and seeds the approved plan.

### Added
- **A Save button that tells the truth.** Work the assistant does stays in your project's
  workspace; nothing is published until you click Save. The button highlights whenever the
  workspace differs from your last save, and leaving with unsaved work warns you first.
- **Every mode reads the live app.** Ask and Plan answer from the code as it is right now —
  including changes Write just made — instead of a stale copy on the server's disk.
- **The assistant commits as it works**, building a readable history inside the workspace so
  it can diff and revert its own changes instead of hand-undoing edits.
- **New progress surfaces while building**: workspace, preview, diagnostic, and quota events
  stream into the chat, and the working indicator stays honest until the build actually ends.

### Changed
- **Typing is never blocked.** The text box and attach button stay live while the assistant
  replies; only Send waits for the turn to finish, your focus is never stolen mid-sentence,
  and a typed draft survives reloads and chat switches.
- **The preview only claims a build that exists.** A project that never built shows no
  Relaunch button and no saved-app promise; a genuine not-found failure is announced aloud.
- **A self-healed build step reads as a retry, not a crash** — distinct copy, the detail shown
  once, no more mid-word truncation, and the page no longer stretches to 11,000px on a failed
  step.
- **The sandbox dev server reports observed truth**: "ready" now means something is actually
  serving the app port, restart attempts while the port is busy are refused, and process-kill
  commands are steered away from the managed dev server.

### Fixed
- **An expired session no longer kills the chat.** Every chat call recovers through the same
  refresh-and-retry path as the rest of the app, with a fresh CSRF token after refresh — and a
  failed mode switch reports what actually failed instead of a canned excuse.
- **Reloading a handed-off chat no longer re-sends and re-bills the opening prompt.**
- **Conversations bricked by a half-persisted build step load again** — orphaned tool results
  are repaired at load, and the write path stops minting them in the first place.
- **Cross-chat containment**: one chat's live build can no longer be torn down, stopped, or
  erased from history by a sibling chat or a mid-build reload, and each chat's send gate is
  keyed to its own turn.
- **Honest chat surfaces**: the machine-written build seed never renders as if you typed it,
  private system notes are never narrated, transcript keys are unique, and the usage meter
  updates live and stays visible on small screens.
- **Post-review hardening (2026-07-30)**: read-only turns no longer leak a background preview
  poller; every end-of-turn message stops claiming an automatic save; an accepted send whose
  reply stream failed keeps your message and asks for a reload instead of inviting a duplicate.

## [1.6.2] - 2026-07-23

**Sign-in works against BIAL's public-client Entra app, and failed sign-ins now say why in the
backend logs.** The tenant's app registration has "Allow public client flows" enabled, so
Microsoft rejected the client secret our backend sent as a confidential client (`AADSTS700025`)
and every sign-in died at the token exchange — the backend now authenticates as a public client
to match. Separately, a failed callback used to bounce the user to a generic "sign-in failed"
banner and record nothing, so a blocked network hop, a wrong secret, and a lost login-state
cookie all looked identical in the logs; that failure is now observable.

### Changed
- **Entra OIDC runs as a public client (no client secret).** `build_oauth()` registers with
  `token_endpoint_auth_method="none"`, and `AUTH__CLIENT_SECRET` is now optional (a deployment
  that still sets it boots unchanged). PKCE (S256) is the sole proof of the code exchange. This
  is a deliberate, temporary reduction in defense-in-depth — the client-authentication layer is
  dropped; the redirect-URI allowlist, PKCE, tenant-exact issuer pin, and fail-closed token
  validation all remain. Tracked as a hardening backlog item to revert once the Entra app is
  switched back to confidential ("Allow public client flows" = No). Ref ADR-0007.
- **The token exchange presents the SPA `Origin` header.** BIAL's app registration keeps the login
  callback under the Single-page application (SPA) platform, whose token endpoint only redeems a
  code from a cross-origin request (`AADSTS9002327`). `build_oauth()` now sends an `Origin` header —
  the scheme+host of the configured `AUTH__REDIRECT_URI`, so it always matches the registered SPA
  reply URL — letting our server-side (backend-owned) redemption succeed without moving auth into
  the browser. No client secret may accompany it (Entra forbids credentials when an `Origin` is
  present), which the public-client switch above already guarantees.

### Fixed
- **The auth callback logs the real failure reason.** A failed token exchange now emits a
  `auth_callback_failed` log line with the exception type and Microsoft's message (never any
  credential), so an operator can immediately tell a network-reach failure from a Microsoft
  rejection (e.g. `invalid_client` / AADSTS7000215) or a lost-state cookie. User-facing
  behaviour is unchanged — it still fails closed to the login banner.

## [1.6.1] - 2026-07-22

**The deployed portal can reach its backend again on Azure App Service.** Behind BIAL's
private networking, every API call from the portal died on Azure's "Web App - Unavailable"
403 page while the backend itself was healthy — sign-in was impossible. The portal's edge
proxy carried two defects that only surface on App Service: it addressed the backend by the
browser's hostname instead of the backend's own (App Service routes requests by Host
header), and it looked up the backend's address once at boot and never again, so it kept
using a stale public IP long after the private endpoint went live.

### Fixed
- **API calls now travel the private path.** nginx addresses the backend by its own
  hostname, presents TLS SNI on the upstream hop, and re-resolves the backend's address
  every 30 seconds — networking fixes now take effect without waiting for a container
  restart. A DNS blip fails fast (5s) instead of hanging every request for 30 seconds.

### Added
- **Boot-time guards for the proxy's deploy inputs.** `DNS_RESOLVER` is now a required
  setting (168.63.129.16 on App Service, 127.0.0.11 under local Docker), and `BACKEND_URL`
  is rejected at startup if it carries a path or trailing slash — either mistake previously
  produced a silent, total routing outage; now the container refuses to start and says why.

## [1.6.0-phase2.5] - 2026-07-22

**Every app gets its own database, and the shared data plane is gone.** Until now every
generated app read and wrote a single shared table on the platform's own database — one
app's data sat beside every other app's. This release gives each project its own isolated
PostgreSQL database with its own login role, walled off from every other app's database.
The app owns its schema (managed by Drizzle); an operator can reveal its connection string,
kill its access, or tear it down; and a reconciler reports databases and roles the registry
has lost track of. The legacy shared plane is removed.

### Added
- **A private database for every project.** Creating a project provisions a dedicated
  PostgreSQL database and a login role scoped to it, and seeds `BIAL_DATABASE_URL` into the
  app's build environment. The role is born with no superuser, create-database, or
  bypass-RLS rights and cannot open a connection to any other app's database — the isolation
  wall is raised at provision time.
- **The app owns its schema.** Generated apps manage their own tables with Drizzle
  (generate → migrate on boot) instead of writing to a shared platform table. The connection
  string stays server-side in the container.
- **Operator database controls.** An operator can reveal an app's connection string
  (superadmin-only, audited, never logged), disable its database access with a kill-switch
  (revoke-and-sever, fail-closed), re-enable it, and hard-delete an app or project with a
  force-drop that stops live connections. Every action is audited by name, never by DSN.
- **Orphan reconciler and advisory sizes.** A report-only reconciler enumerates the
  cluster's databases and roles and flags any the registry no longer tracks, and the admin
  registry panel shows each app database's advisory on-disk size.

### Changed
- **The app role owns its database's `public` schema at provision**, so Drizzle migrations
  run without a separate grant step (with a loud, actionable warning if the maintenance role
  lacks the privilege on the target cluster).
- **The `appkey` service package is now `cors`**, reflecting what it does after the
  shared-plane removal; the credential-free, null-reflecting CORS branch is gone.

### Removed
- **The legacy shared `data_records` data plane** — its tables, the per-app counter columns,
  the records API, the `X-App-Key` chain, and the quota helper. This is a breaking change:
  it must land only after every serving image has stopped reading the old plane. See the
  migration's documented three-step release window. Data in the old shared plane is not
  carried into the per-app databases.

### Fixed
- **Daily token usage no longer double-counts.** The daily spend against the token cap counts
  input plus output tokens once, correcting an over-count that could trip the cap early.
- **A severed database connection can't crash the app.** The generated app attaches an error
  listener to its connection pool, so a connection dropped underneath it (for example by the
  kill-switch) is handled instead of taking the process down.
- **Database controls fail honestly on an unreachable cluster.** The admin database levers and
  teardown return 503 rather than 500 when the cluster can't be reached, and provisioning
  survives an unconfigured or unreachable cluster.
- **The app-role password never rides an error into the logs.** Role-DDL failures are scrubbed
  to their SQLSTATE, so a provisioning error can't carry the generated password into a log line.

## [1.6.0-phase2.4] - 2026-07-21

**Builds that fail honestly, and storage that cleans up after itself.** When the store
that coordinates build sessions blinked, the platform used to say a build was already
running when none was — or fail with nothing useful in it. And a file you uploaded but
never sent quietly ate your storage allowance forever, reachable by no delete you could
find. This release makes those failures tell the truth, and gives an operator a way to
reclaim what earlier cleanups left behind.

### Added
- **Coordination health on the health endpoint.** `/v1/health` now reports the build
  coordination store alongside the database. A fault there reads as `degraded` at HTTP
  200 — build sessions stop, everything else keeps working, so the instance is not
  drained for a problem draining would not fix. Alerting should key on the status code,
  never on the word.
- **A startup probe.** The API checks coordination at boot and warns with a pointed hint
  when it cannot reach it, so a misconfigured deploy surfaces to whoever is deploying
  rather than to the first person who tries to build. Boot is never blocked.
- **An operator sweep that reconciles storage against the database.** A superadmin can
  reclaim files a failed cleanup stranded, and never-sent uploads that were consuming
  their owner's allowance with no way to reach them. Attachments and snapshots are
  reclaimed; submission bundles and legacy app files are reported only, pending a
  retention decision. Anything younger than 24 hours is left alone, so it is safe to run
  while people are working.
- **Uploads remember their conversation.** An attachment is now linked to the thread it
  was attached to, which is what makes reclaiming the never-sent ones possible at all.

### Changed
- **A coordination outage now reads as "try again", not "already running".** Every
  build-session route answers with a retryable 503 and plain copy. Before, an outage
  could surface as a conflict describing a session that never existed.
- **Deleting a project is refused while that project's app is building**, so a delete
  cannot destroy work that has not been saved yet. The refusal is scoped to the app in
  question, so building one project no longer blocks deleting another.
- **Generated apps show your change without a reload.** The build prompt now carries an
  unconditional rule that a create, edit, or delete must be reflected on screen.
- **Coordination connections retry on an explicit, bounded policy** and require TLS in
  production — a plaintext URL is refused at startup rather than putting keys on the wire.

### Fixed
- **A blip while a build was starting could strand the sandbox.** An error at the moment
  a session was registered left the user unable to start any new build, with the
  container still running and nothing able to reclaim it. It now tears down cleanly.
- **Deleting a project no longer strands a submission written mid-delete.** The
  submission prefixes are re-checked after the delete commits.

## [1.6.0-phase2.3] - 2026-07-20

**A preview you can get back, and an app that stops over-promising.** Previews are
temporary — the sandbox behind one gets torn down, and until now the link you were
handed simply started 404ing with no way forward. And a generated app would cheerfully
render a "live shared" badge over data it had loaded exactly once, or ship an example
page nobody asked for. This release closes the gap between what the product says and
what it actually does.

### Added
- **Relaunch a preview that has gone away.** When the sandbox behind a preview is torn
  down, the project page now offers Relaunch instead of a dead link. It works after a
  reload too, and when it cannot relaunch it tells you why rather than failing quietly.
  A relaunched preview comes back owned by you and is honest about how much of the
  original workspace it restored.
- **Generated apps are checked for liveness they never wired.** An app that renders
  "live" or "shared" UI over data it fetches once is flagged, so the claim on screen
  matches the behaviour underneath.
- **An env-gated trace of the agent's build path.** Setting `BRAIN_TRACE_DIR` records
  each tool call the build agent makes, for diagnosing a build that went wrong. Off
  unless that variable is set, so it costs nothing in normal operation.

### Fixed
- **Reloading the planning chat no longer re-posts your message or re-calls the model.**
  A reload mid-answer used to resend the last turn, charging you twice for it. A stream
  that stalls now ends in a clear error instead of a reply that silently stops
  mid-sentence and looks finished.
- **A message could leak into the wrong conversation.** Two chats open at once could
  cross streams. Fixed, along with the truncated reply that got faked when a stream
  ended early.
- **The model connection can no longer hang forever.** The shared Foundry client now has
  a finite socket with retries and a keepalive, so a network stall surfaces as an error
  instead of a request that never returns.
- **Only the newest brief can start a build.** An older brief left on screen could still
  fire, building something you had already moved on from.
- **Apps show their real name in the admin registry** rather than "(untitled)". The
  display name is sourced from the project, which is the thing that actually has one.
- **Storage fails closed on an unnamed 404.** An ambiguous not-found from the object
  store is now treated as a failure rather than an empty success, so a missing container
  can no longer read as "nothing there".

### Changed
- **The deploy credential is rotatable.** The SAS used for deploys can be rotated without
  redeploying the platform.
- **The build agent is held to HONEST UI, REMOVE SCAFFOLDING and RESPONSIVE rules**, so
  generated apps stop shipping example pages and stop claiming behaviour they lack.
- **The build-part write surface and attachment boundary are closed**, narrowing what a
  build session is able to write.

### Removed
- **The `/records` example route and its home-page CTA** are gone from the generated-app
  template. Every new app used to ship a page nobody asked for.

## [1.6.0-phase2.2] - 2026-07-17

**One conversation per project.** Describing an app used to mean two chats: a planning chat
that asked good questions, and a separate builder chat that did the building — with a modal
handoff between them that a non-technical user would never find. Worse, the builder built on
the first thing you said. Ask it for "a visitor app" and it guessed at the rest and built
that, and you only found out minutes later by looking at the result.

Now a project has one thread. You say what you need, the assistant asks a couple of questions
if it genuinely can't tell what to build, and then shows you the brief it intends to build
before it builds anything. One click starts it. The same thread handles every change after
that, and it keeps the whole story: your prompt, the questions, the brief, and what each build
produced.

### Added
- **The assistant asks before it builds.** On a vague request it asks at most three focused
  questions, in one turn, and only about what it genuinely cannot infer. On a request that is
  already clear it asks nothing and goes straight to the brief. The guidelines live on the
  server, so they are the same for everyone and cannot be edited away by the browser.
- **You see what will be built, and you approve it.** The assistant proposes a brief on a card;
  the build starts only when you click it. That is true for the first build and for every
  change afterwards — "add a chart" gets you an updated brief to confirm, not a surprise
  rebuild. If the brief comes back malformed, the card still works rather than leaving you
  holding a description with no way to build it.
- **One thread per project, reachable from the project page.** Opening a project and building
  from it lands in the same conversation every time, instead of leaving a pile of one-shot
  build chats behind. Older build chats stay readable in the project's list.
- **The transcript records what each build produced** — finished or failed, the preview link,
  and the reason when it failed. Reopening a project weeks later tells you the whole story.
  The server writes this record when the build finishes, so it is there even if you closed the
  tab and walked away, which is the normal thing to do during a build that takes minutes.

### Fixed
- **A message could be silently swallowed.** Two things now write to one transcript (you, and
  the build recording its outcome), and they could both claim the same slot. The loser was
  answered "saved" and written nowhere. Reloading the page mid-build and then sending a message
  hit this every time: the message vanished while the assistant still answered it, leaving a
  thread holding an answer to a question that wasn't there. The server now assigns the slot and
  tells the browser which one it used. This also removes the same failure from the planning
  chat, where it had always been possible.
- **A build that ran but did not save now says so** on its record in the thread, instead of
  reporting plainly as finished.

### Changed
- **The chat request's `system` field is now size-capped** (64 KB). It was the one unbounded
  field on that endpoint — an oversized prompt would bill against your daily token cap on every
  turn and push the real conversation out of the model's window.
- **The planning chat's "Launch Builder" hands off to the project's thread** rather than
  minting a new build chat, so the brief it worked to produce lands where the work is.

### Removed
- **The chat relay no longer injects the project's stored source code** into a builder turn. It
  was reachable by nothing (the builder page never used the chat relay, and nothing has written
  that stored copy since the single-file era), and on the new interview turns it would have
  pushed up to ~75k tokens of source against your daily cap to answer "what should this app
  track?". Builds get code from the restored workspace, which is the path that actually runs.

## [1.6.0-phase2.1] - 2026-07-17

**Pilot closure.** Every in-pilot gap from the 2026-07-16 release audit, closed against
existing seams. Two of these were silent-data-loss paths: a build could quietly overwrite
your saved app with a blank template, and a finished build could report that your work was
saved when it wasn't. Attachments you added to a build now actually reach the agent — before
this, the build ran as if the file wasn't there.

### Added
- **Files attached in the composer reach the build.** Images and PDFs arrive as vision
  content, spreadsheets and documents as their extracted text, CSV/TXT inline — so "build me
  an app from this spreadsheet" now works. Attachments are collected from every turn since the
  last build (not just the newest message), scoped to the owner and the project, and a file
  that can't be read fails the start with a clear message naming it rather than silently
  building the wrong app.
- **Deployed apps get their own long-lived storage credential.** A superadmin mints a
  365-day, container-scoped Blob credential (`POST /v1/admin/apps/{id}/deploy-credential`), so
  a live app reaches its own storage directly with no platform in the data path. It is minted
  against a per-app stored access policy, which is what makes revoking it real: delete the
  policy and the credential dies. Supersedes the go-live runbook's KNOWN GAP.
- **"Your app is live."** The superadmin records the deployed URL at mark-deployed and the
  app's owner sees a Live link instead of "deployed by the platform team". https-only.
- **Prompt caching on the build loop**, at the 1-hour tier — a build's steps can sit minutes
  apart while npm installs, and a 5-minute cache would expire between them and cost more than
  it saved.

### Changed
- **The build agent never seeds fake data.** No dummy, sample, or placeholder records; it
  builds honest empty, loading, and error states, and real data arrives by upload or entry.
  Restores a promise the POC kept and the open-sandbox rewrite lost.
- **The end-of-build event is now emitted by the session manager, not the agent** — after the
  snapshot commits, so `snapshot_committed` finally tells the truth. The agent can no longer
  emit a terminal event at all; the capability was removed rather than merely discouraged.
- Portal lint runs again: a v9 flat config plus the plugins it always needed. `npm run lint`
  has been in `package.json` for months with no config on disk, so it could not execute at all.

### Fixed
- **A transient storage error can no longer put a blank template over your work.** The restore
  path retries, then fails the build with "Sandbox unavailable. Please try again later or
  contact the admin" — leaving your saved version intact. Provisioning a fresh app now happens
  only when storage positively confirms there is nothing saved. A failing restore used to fall
  back to a blank sandbox, which the next snapshot then wrote over the user's real work.
- Builds no longer fail on deployments that run without object storage configured.
- A build that finished while a stop was landing could be torn down without its snapshot while
  still reporting success.
- An escalated build reported itself as a graceful end rather than a failure.
- Attachments now precede the instruction in the prompt, matching Anthropic's vision ordering
  and the portal's own assembly.
- Office attachment `format` is sanitized before it reaches the prompt fence.
- A 2083-character URL at mark-deployed returned a server error instead of a validation error.

## [1.6.0-phase2.0] - 2026-07-13

Phase-2 **Stage 0 — the agentic-build foundation.** This is the sequential,
one-branch foundation the four parallel Wave-1 tracks fork from; it freezes the
cross-track seams and stubs the shared skeletons so every Wave-1 worktree only
*adds* files. It does **not** implement the build loop. Shipped on the non-prod
`release/phase2` integration branch (forked from `release/1.5.0`).

### Added
- **Nine field-level cross-track contracts (C1–C9)** frozen as durable docs under
  `docs/engineering/contracts/` — the supervisor HTTP API, sandbox-client ABC,
  build-session control API, snapshot/sync ordering, Redis key namespace, golden-template
  shape, brain interface + progress envelope, preview transport/framing, and interim
  app-data access — each specified to request/response/enum/signature level so the four
  Wave-1 tracks build against faithful mocks without reading each other's code.
- **Frozen backend shared-file stubs.** Optional `redis` + `sandbox` sub-configs on
  `Settings` (prod-gated, so the existing suite boots with no new env); a `services/redis/`
  async pool + frozen C5 key namespace; a `services/sandbox/` complete `SandboxClient` ABC +
  `SandboxHandle`; and a `build_sessions/` API package with real C3/C7 schemas (status enum +
  tagged-union progress envelope) behind a mounted stub router.
- **Golden Next.js CRUD template + pre-baked sandbox base image** under a new top-level
  `sandbox/` tree (Next.js 16 / React 19 / Tailwind v4 / shadcn/ui / TypeScript 5.x on Node 24
  LTS, latest-stable-then-pinned), with the single swappable data module wired to the existing
  platform data-service (HTTP client, not an ORM), cross-origin `frame-ancestors` framing in
  Caddy, and cross-platform (LF) guards for the Windows image build.
- **Walking skeleton** (`scripts/skeleton/`) that proves the two genuinely-hard facts once for
  real — cross-origin `frame-ancestors` framing with origin-validated `postMessage`, and a real
  golden-template `next dev` render — rather than mocking them away.

### Changed
- **Retired the old single-file `/preview` backend.** The in-browser Babel `/preview` shell is
  removed (route, shell, CSP builder, middleware branch, reserved root); the deployed `/apps`
  runner is unchanged. The builder live-preview is knowingly dark on `release/phase2` until the
  Wave-1 PORTAL-PREVIEW track lands the per-session cross-origin preview.
- **Decision record updated** — ADR-0014 storage clause (local disk + git-snapshot to Blob,
  public-ingress POC posture, still Proposed) and ADR-0018 (latest-stable-then-pinned stack +
  interim data-service client).

## [1.5.0] - 2026-07-03

### Added
- **Sign in with Microsoft (Entra ID).** The portal now authenticates against the
  organization's Microsoft Entra ID tenant. Signing in is a single "Sign in with Microsoft"
  click — the FastAPI control-plane runs the OpenID Connect flow itself (Authorization Code +
  PKCE), validates the Microsoft token fail-closed against the one configured tenant
  (outside-tenant and personal accounts are rejected), provisions you by your stable Entra
  Object ID, and issues its own secure session. Sessions are cookie-based (nothing is kept in
  the browser's storage), silently refresh in the background across tabs, carry an 8-hour
  absolute cap, and can be revoked server-side instantly.
- **The FastAPI control-plane backend lands (Phase 1 foundation).** A new `backend/`
  Python service (FastAPI, async SQLAlchemy 2.0 + asyncpg, Alembic, PostgreSQL) begins the
  incremental, strangler-fig replacement of the Express portal backend (ADR-0001). This first
  cut is the foundation scaffold: a typed fail-first `Settings`, the app factory with
  security headers + credentialed CORS, boundary exception handlers, the v1 router with a
  `/health` database-liveness probe, UUIDv7 / timestamp / user-scope DB mixins, the initial
  Alembic migration (pgcrypto + pgvector extensions), and an Azure Blob object-storage service
  (owner-scoped keys, no-SAS server-proxy default, managed-identity user-delegation SAS).
- **The citizen-developer app + data plane is live (control-plane completion).** You can now
  describe an app in chat and get a real, running app: the FastAPI backend provisions it,
  persists its data as per-app records in PostgreSQL (create / list / search / edit / delete),
  stores and parses per-app file uploads, and serves the generated app in a sandboxed frame
  with a built-in data client — so a freshly generated app shows its empty state instead of a
  "failed to fetch" flash. Admin governance (submit / approve / disable), conversations and
  message history, the Claude chat relay (via Azure Foundry), attachments, per-user usage and
  daily-token limits, and feedback all now run on the FastAPI control-plane, RBAC-gated and
  audited.
- **The backend can sign in to Postgres with Microsoft Entra (managed identity).** In
  production, the FastAPI control-plane can now connect to an Azure Database for PostgreSQL
  Flexible Server using a short-lived Microsoft Entra token in place of a stored database
  password — set `DB_AUTH_MODE=entra` and the app fetches the token via its managed identity on
  every connection, over verify-full TLS. Local development and tests are unchanged (the default
  `password` mode keeps using the Docker Postgres), so there is no database secret to store or
  rotate in production.

### Changed
- **The portal login is now Microsoft-only.** The username/password form is replaced by a
  single "Sign in with Microsoft" button, and the app shell reads the signed-in profile from
  the backend rather than from a stored token. The legacy Express password login is retired
  behind a `PASSWORD_LOGIN_ENABLED` gate (default enabled) so it can be switched off the moment
  Entra is verified live in production, then removed in a later step — no lockout risk during
  the cutover.
- **The changelog and product version moved to the repo root.** As the platform grows past the
  single `portal/` app to include the `backend/` control-plane, this changelog moved from
  `portal/CHANGELOG.md` to `CHANGELOG.md`, and the product version of record now lives in a
  root `VERSION` file. This is the first release tracked at the root, continuing the line from
  `1.4.9` → `1.5.0`. (The backend service keeps its own component version, `0.1.0`, in
  `backend/pyproject.toml` — the product version and the backend's API-maturity version are
  deliberately separate axes.)
- **The portal is now a static SPA behind nginx.** With the Express backend gone, the
  React/Vite portal ships as static files served by nginx, which proxies the API surface to
  the FastAPI control-plane. One less moving part, and no Node server in the portal image.
- **The backend API layer now follows one consistent shape (internal refactor).** Every domain
  keeps its routes and its request/response models side by side on one shared camelCase base,
  every route documents the errors it can return in the OpenAPI contract, and error responses
  are raised through a single path. No response body or status code changed — the wire contract
  the portal and generated apps depend on is byte-for-byte identical, locked by characterization
  tests.

### Removed
- **The Express / Node / Cosmos POC backend is retired.** `portal/server.js`, all of
  `portal/server/`, the Vercel Claude proxy, the Cosmos/Mongo operational scripts, and the
  Express-era single-container Docker setup are gone. The FastAPI control-plane + PostgreSQL
  fully replace them — the portal no longer runs any Node backend.

### Security
- Backend-owned OIDC as the relying party (no trusted proxy-asserted identity): a pinned HS256
  session-JWT algorithm that rejects `alg=none`, SHA-256-hashed refresh tokens with strict
  single-use rotation and family-based reuse detection, a `token_version` instant-revocation
  lever, environment-aware `__Host-`/`__Secure-` cookies, and signed double-submit CSRF
  protection on state-changing requests (ADR-0007).

## [1.4.9] - 2026-06-26

### Added
- **You can now attach PowerPoint decks (`.pptx`) to a chat.** Plan, App Builder, and
  the general assistant accept a `.pptx` alongside images, PDF, Word, and Excel, and the
  assistant reads the deck as a visual document — slides, layout, and charts and all — so
  you can ask it to summarize, critique, or build an app from a presentation. You can also
  attach a deck at the "Generate App" step so the very first build turn can reason over it.
  The original `.pptx` is what's stored and re-downloaded, and decks count toward the same
  per-conversation attachment cap as other files. (Available when the deck feature is
  enabled on the server with a reachable conversion sidecar; when it's off, `.pptx` simply
  isn't offered, with a clear message.)
- **The deck renderer now ships in one container for single-slot hosts.** The portal API/SPA
  and the Gotenberg/LibreOffice renderer build into a single image (`Dockerfile.appservice`)
  that run together on loopback with only the portal port exposed, so the deck feature can
  deploy to one-container platforms (Azure App Service for Containers) without a separate
  sidecar. The packaging is proven by the repo's first committed Playwright e2e suite, which
  drives a real browser through attach `.pptx` → assistant reads it → download the original,
  against both the dev stack and the built container (`npm run e2e` / `npm run e2e:container`).

### Changed
- **The "Generate App" file picker now accepts the same files as chat.** It previously took
  only spreadsheets (`.xlsx`/`.xls`/`.csv`/`.tsv`); it now accepts images, PDF, Word, Excel,
  and (when enabled) PowerPoint, and the files you pick feed the first generation turn
  directly instead of being flattened into pasted text.

### Fixed
- **App Builder's live preview no longer fails its data calls in local development.** The dev
  server was answering the preview's cross-origin preflight itself and blocking the request;
  it now lets the app server handle it, matching how production already behaves.

## [1.4.8] - 2026-06-25

### Fixed
- **Generated apps no longer advertise Word support their file picker won't accept.** A spreadsheet
  dashboard could show "Word (.docx) supported" in its upload error while its picker only took
  Excel/CSV — confusing when you tried to add a Word file. App-generation guidance now keeps each
  app's file-picker `accept` and its on-screen "supported types"/rejection message in sync with
  what the app actually handles, and documents that Word files can be stored (`docx` was already
  accepted server-side). Word parsing and storage themselves were never broken.

### Removed
- The "Empowering airport staff…" tagline on the login page.
- The "Featured Demo / RideLink BLR" sample card on the Sandbox start screen.

## [1.4.7] - 2026-06-25

### Fixed
- **App Builder now shows the AI's reply right away — no page refresh needed.** When a build
  prompt was answered with clarifying questions instead of an app (no code generated), the
  reply was saved but never rendered, so the chat looked empty until you reloaded the page.
  The Builder now appends the assistant's reply to the conversation as soon as generation
  finishes (the live preview still renders any generated app; the code block is stripped from
  the chat bubble as before). It also no longer claims "Your app is ready" over an empty
  preview when the model only asked questions.

## [1.4.6] - 2026-06-25

### Added
- **Generated apps can now store Word (`.docx`) files, not just Excel/CSV.** Word documents
  are accepted by the per-app file store (`BIALData.uploadFile`), so an app can keep an
  uploaded `.docx` in object storage alongside its record data and re-open it later to read
  its text — the same store-and-reparse flow that already worked for spreadsheets. Word is
  parsed by mammoth inside the sandboxed parse worker, served back with `nosniff` + a
  locked content CSP, exactly like the existing `.xlsx` path.

## [1.4.5] - 2026-06-24

### Added
- **Generated apps can turn an uploaded spreadsheet into a dashboard.** A deployed or
  preview app can now hand an uploaded Excel (`.xlsx`/`.xls`), CSV, or Word (`.docx`) file
  to the platform to be parsed — spreadsheets come back as structured rows, with the list
  of worksheet names so the app can offer a sheet picker; Word comes back as text — and
  render KPI cards, charts, and sortable tables from it. A view-only app parses for the
  session and keeps nothing; nothing is stored unless the app explicitly saves it. Reached
  through the injected `BIALData.parseFile(...)` client (a fresh file, or a previously
  uploaded one by id). PDF parsing is a planned fast-follow.
- **Real charts in generated apps.** The sanctioned Recharts charting library is now
  available inside every app sandbox, so dashboards render proper bar / line / grouped /
  stacked charts instead of hand-drawn SVG.

### Changed
- **Builder guidance for parsing and charts.** The app builder now knows to parse files via
  `BIALData.parseFile` (never a hand-rolled or CDN parser, and never assuming a global like
  `XLSX`), to offer worksheet and column selection where useful, and to draw charts with
  the Recharts global.

### Security
- **Untrusted uploaded files are parsed under strict server-side limits.** Parsing runs in
  an isolated worker thread with a hard wall-clock time budget and a memory ceiling, behind
  file-size, decompressed-size (zip-bomb), and row/column caps — an oversized or malicious
  file is rejected or truncated cleanly rather than exhausting the server, and a bomb can't
  slip through by being relabelled. The chart library is served through the sandbox's
  existing script allowlist with no change to the network/image rules that keep an app's
  session token from leaking.

## [1.4.4] - 2026-06-24

### Added
- **Attach Word and Excel files in chat.** You can now drop a `.docx` or `.xlsx` into any of
  the three chat surfaces (App Plan, Build, and BIAL Chat) alongside images, PDFs, and
  CSV/TXT. The document's text and the spreadsheet's sheets are read so the AI can answer
  questions about them, build from them, or summarise them. The original file stays attached
  as a chip you can click to download, byte-for-byte. Up to 4 MB per file. Legacy `.doc`
  files are politely declined with a "save as .docx" message.

### Changed
- **Large spreadsheets are handled gracefully.** Each sheet now sends up to 1,000 rows to the
  AI (raised from 200), so real rosters and schedules come through whole. If a sheet is still
  larger, the attachment is marked "truncated" and hovering the chip tells you exactly what
  was shortened — for example "first 1,000 of 2,300 rows" — while the file you download stays
  complete.

## [1.4.3] - 2026-06-24

### Fixed
- **No more surprise sign-outs on a brief hiccup.** When the app refreshed your session
  in the background, a momentary network blip, a rate-limit, or a transient server error
  could wrongly sign you out and bounce you to the login screen with "session expired" —
  even though your session was still valid. The app now signs you out only on a real
  authentication failure; transient errors keep you signed in and retry quietly.

### Changed
- **Steadier background session refresh.** After a transient refresh failure the app now
  waits briefly before trying again instead of retrying on every click — which, when many
  pilot users share one network, was making the rate-limiting worse. Each fail-open event
  is now logged to the browser console so session issues are easier to diagnose.

## [1.4.2] - 2026-06-24

### Added
- **Apps can now keep files, not just records.** A generated app can store an uploaded
  file or a file it produces (for example a reconciliation report), then list it,
  download it to your device, or re-open it inside the app later. Files survive a page
  refresh and are scoped to the app. Supported types: CSV, Excel (xlsx/xls), JSON, text,
  PDF, and common images (PNG/JPEG/GIF/WebP), up to roughly 18 MB per file.
- **Admin file visibility and cleanup.** Admins can see each app's file count and storage
  use, clear an app's files, and recompute the usage counters if they ever drift. Deleting
  an app also removes its stored files.

### Changed
- **Builder guidance for files.** The app builder now knows when to keep a file versus keep
  records, shows the worked reconciliation-report pattern, and warns that an app holding
  sensitive files must require sign-in and IT security review before go-live.
- **Runtime download support.** Deployed apps and the live preview can trigger a file
  download and render stored images inline, without widening what the sandbox can reach.

### Fixed
- Hardened the two-store file writes so a failed upload or delete no longer leaves an
  orphaned file or a wrong usage counter; cleanup and counter-recompute are race-safe.
- File lists now query against a matching database index, avoiding a slow or failing path
  on the production database.
- A generated file download over a non-secure URL now safely falls back to the in-app proxy.

## [1.4.1] - 2026-06-23

### Added
- **Pilot (POC) notice on the home screen.** A short banner now states this is an
  early proof-of-concept and that apps and data are for demonstration only and may
  change or reset, so first-time users know what to expect.

### Changed
- **The daily AI token counter is now easy to see.** It moved from tiny grey text to
  a clear status chip showing `used / limit` that turns amber as you near the limit
  and red when it's used up. It still reads your live usage and resets at midnight IST.
- **Clearer "Plan with AI" vs "Build an App".** The App Builder now explains that
  Plan with AI scopes your requirements in a guided chat first (no code yet), while
  Build an App jumps straight to a working draft.
- **Honest global search.** The search box no longer advertises apps it can't find —
  it now reads "Search pages or actions…" to match what it actually searches.

### Removed
- **Removed the non-functional "Data Source" dropdown and "Backend Schema" toggle**
  from the build sandbox. They connected to no real system, so they are gone, along
  with the misleading help text that claimed the portal connects to AODB, FIDS, and
  other airport systems. File upload, the Theme picker, and saved app data are
  unchanged.
- **Removed the meaningless role label** ("User") shown under the home-screen greeting.

## [1.4.0] - 2026-06-23

### Added
- **Build real, data-backed apps and deploy them to a shareable link.** The App
  Builder now generates working tools (like a Gate Inspection Log) that save records
  to a shared, per-app data store instead of holding everything in the browser.
  Start from a prompt or seed from an uploaded CSV, then **Submit for deployment**;
  once an admin approves, the app is served at its own `/apps/:id` URL. Apps can
  require your BIAL portal sign-in, and what you save persists and is shared with
  other signed-in users.
- **Search, filter, and page through your records.** Generated apps now include a
  search box that matches across every field, per-field filters (e.g. show only
  Status = Fail), and page-number pagination with a live total count. These are
  powered by a shared data API and the App Builder wires them in automatically, so
  apps stay fast even as the record count grows — no more loading every row into the
  browser to search or sort.
- **Admin App Registry.** Admins can review and approve or reject submitted apps,
  turn each app's sign-in requirement on or off, disable or delete an app, clear its
  data, and read a full audit trail of who created, changed, or deleted records.

### Security
- **Strict per-app data isolation.** Every record read and write is scoped to its
  own app, so one app can never see or change another app's data — even if someone
  guesses a record ID. Per-app storage quotas and request rate limits are enforced.
- **Hardened app sandbox.** Deployed apps and the live preview run in an
  opaque-origin sandboxed frame that cannot read your portal session. A scoped
  content-security-policy blocks any off-origin leak of the short-lived access token,
  native form submissions can't smuggle it out, and the long-lived refresh token is
  never handed to an app.

### Fixed
- **Record search and lists work on the deployed (Azure Cosmos DB) database, not
  just locally.** Record search now sorts on a single field, and the per-app
  list/search reads ship the tenant-scoped composite indexes Cosmos requires — it
  rejects a multi-field sort, or a filtered-and-sorted read with no matching index,
  with a 400 (the same constraint that broke chat history in 1.3.1–1.3.3). The
  indexes are created automatically on server start and can be applied to a running
  deployment with `node scripts/ensure-indexes.js`.
- **Sign-in works in deployed data-backed apps.** The app page now signs you in with
  the shared BIAL login and hands the running app a ready session (your identity is
  available to the app, never your password), so apps no longer try — and fail — to
  log in from inside their sandbox. The App Builder also stops generating a redundant
  in-app login form, and any older app that still has one now skips it automatically.

## [1.3.3] - 2026-06-23

### Fixed
- **Opening a conversation now actually loads its messages on the deployed app.** A
  live probe against the Cosmos account showed it serves only single-field ORDER BY
  — any multi-field sort (`{seq, createdAt}`, `{seq, createdAt, _id}`) returns the
  same 400, even with a matching compound index, which is why 1.3.1/1.3.2 did not
  fully resolve it. Messages now sort by `seq` alone; `seq` is a unique, monotonic
  per-conversation counter (user = N, assistant = N+1) so it fully orders messages
  with no tiebreak, and the matching index drops to `{conversationId, username, seq}`.

## [1.3.2] - 2026-06-23

### Fixed
- **Opening a chat or App Builder conversation works on the deployed app.** The
  1.3.1 indexes fixed the conversation list, but loading a single conversation's
  messages still failed with the same Cosmos 400 because the message read sorted by
  `_id` as a final tiebreak — and Azure Cosmos DB for MongoDB will not serve an
  ORDER BY that includes `_id`, even with the index present. Messages now sort by
  `{seq, createdAt}` and the matching index drops `_id`, so the read is served.

## [1.3.1] - 2026-06-23

### Fixed
- **Chat and App Builder history loads again on the deployed app.** On Azure Cosmos
  DB, listing your conversations and opening a chat were failing with a 400 error
  because the database had no composite index to serve those sorted, filtered
  reads (it worked locally, where the database does not require one). The required
  indexes are now created automatically on server start, so a fresh deployment
  fixes itself. To unblock a running deployment without redeploying, run
  `node scripts/ensure-indexes.js`.

## [1.3.0] - 2026-06-22

### Added
- **Your chats, generated apps, and uploaded files now follow you across browsers
  and devices.** Planning chats, App Builder sessions, the generated app code, and
  attachments are saved to your account on the server instead of only in this
  browser. Sign in on another machine and your recent work is already there;
  clearing your browser no longer loses anything.
- **Image and PDF attachments are kept in cloud object storage.** Attachment files
  live in a dedicated object store (Azure Blob Storage in production, or any
  S3-compatible store) and are served back through an authenticated, per-user link,
  so your files are only ever readable by you. Small text files (CSV/TXT) travel
  inline with the message. Supported uploads: PNG, JPEG, GIF, WebP, and PDF, up to
  4 MB each, with a 50 MB per-user total; unsupported files are rejected with a
  clear message.

### Changed
- **Conversations and generated code load from the server.** The App Builder and
  chat history, message order, and the latest generated app preview are read from
  the server on every open and refresh, replacing the previous browser-only
  storage. Signing out clears your local session while your work stays safe on the
  server.

## [1.2.0] - 2026-06-19

### Fixed
- **No more surprise logouts while navigating.** The route guard now silently
  refreshes an expired access token (using the still-valid 7-day refresh token)
  before redirecting, instead of bouncing you to the login screen the moment the
  15-minute access token lapsed mid-session. A transient network error during the
  refresh no longer wipes your session either — only a genuine auth failure signs
  you out, so a brief connectivity blip lets the next action retry.

### Removed
- **Deploy feature removed.** The non-functional "Deploy App" button — and its
  mock deploy page/route — is gone, along with the related Help Center FAQ, the
  "Understanding Deployment" section, and the deploy references in the App Builder
  copy.
- **Login "Contact IT Support Desk" link removed**, as it pointed at a
  non-functional destination.

### Changed
- **Consistent "Plan with AI" naming.** The App Builder sandbox's planning toggle
  is now labelled "Plan with AI" (was "Chat & Plan"), matching the hero and
  history CTAs. The duplicate "Plan with AI" button in the workspace empty state
  was removed, since the hero card above it already offers the same action.

## [1.1.1] - 2026-06-19

### Changed
- **BIAL Chat is temporarily hidden.** The general-assistant chat no longer
  appears in the top nav, the search dropdown, or the dashboard, and the
  dashboard reflows cleanly around the single remaining App Builder card. This is
  a temporary suppression behind a single flag — the `/chat` pages still work by
  direct URL and the feature can be restored in one line.
- **BIAL pilot users now get memorable temporary passwords.** The pilot seed sets
  each user's password to `<LastName>BIAL@123` (e.g. `FernandezBIAL@123`) instead
  of a random string, and every run now also resets existing users' passwords to
  this value — so missing users are created and existing ones refreshed in a
  single pass. Passwords are still stored only as Argon2id hashes. The redundant
  `--rotate` flag was removed; `--dry-run` still previews without writing.

## [1.1.0] - 2026-06-18

### Added
- **Send feedback from anywhere.** A "Feedback" button in the header opens a modal
  with a single text box; submitting stores the message tagged with who sent it,
  when, and which page they were on, then confirms with a toast.
- **Review feedback in Admin.** A read-only "Feedback" tab in the Admin console
  lists submissions newest-first (user, message, page, time), visible to admins only.

### Changed
- New required setting `MONGODB_FEEDBACK_COLLECTION` plus a pre-created Cosmos
  `feedback` collection are needed before deploy. Local dev (docker `mongo:7`)
  auto-creates the collection on first write, so only the env var is needed locally.
