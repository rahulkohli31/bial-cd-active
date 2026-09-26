# ADR-0031: Administrators Configure the Publish Classification

## Context

Before an owner's app publishes, a reviewer agent reads its saved code and answers a set of
data-classification questions about it, and the publish gate decides from those answers whether
the app goes live by itself or waits for an administrator. The questions, what each one was
worth and the cut-off between the two outcomes were written into the code. Changing any of them —
adding a question, rewording one the agent kept misreading, deciding that one kind of data should
always go to review, or moving the cut-off — took a release.

Those are governance decisions, and the people who make them are the platform's administrators.
They need to make them from the admin console, see the effect on the next app sent for
publishing, and be able to tell afterwards who changed what.

## Decision

**The configuration lives in the database, as classes and one policy.** A class has a title, a
description, a kind, a weight and an active switch, and records who changed it last and when. The
policy is a single row: the threshold, and whether owners may change the reviewer's answers. There
is no version counter and no versions table. Every gate decision stores a snapshot of the
configuration it was made under, and every change leaves an audit row with its before and after
values, so neither history nor a counter is needed to reconstruct what applied when.

**Two kinds of class.** A hard block sends the app to review on a Yes whatever else it answered,
and carries no weight. A scored class carries a whole-number weight from 0 to 100, and the score
is the share of the active scored weight answered Yes, out of 100, rounded half up; with no active
scored weight the score is 0. A class can be switched between the two kinds; switching to a hard
block clears the weight, and switching to scored requires one.

**The gate decides in a fixed order, and every route needs the owner's note.** A send routes to
an administrator when there is no finished review of the saved version under the live class
definitions, when a hard block is answered Yes, when an administrator's rejection stands, or when
the score is over the threshold; anything else publishes by itself. A route without a note is
refused as incomplete and names which of those four applied, so the owner's dialog can tell a
stale review from a real reason. The gate always scores with the live kinds, weights, threshold
and owners' switch.

**The owners' switch decides whose answers are scored.** While owners may change answers, the
owner's answer for a scored class replaces the reviewer's, and a class the owner leaves alone
keeps the reviewer's answer. While they may not, only the reviewer's answers count and anything
the owner sends is ignored. Hard blocks are always the reviewer's. An owner's answer counts only
against a review that is current, and an answer for a class that is not active is refused.

**Every class has a key that never changes.** It is made from the title when the class is added,
with a numeric suffix when that is already taken. The reviewer answers by key and stored decisions
refer to classes by key, so rewording a title never breaks an answer already given. The credential
scanner feeds its findings to the seeded credentials class by that class's key. Titles are unique
without regard to letter case.

**Classes are switched off, never deleted.** A decision made under a class keeps referring to it,
so the class stays and an inactive one simply takes no part in reviews or scores.

**A description is the reviewer's instruction, not owner-facing help.** It states its rule, where
the rule stops, and Yes and No examples of it; the reviewer's fixed instructions say the examples
illustrate the rule rather than list the only cases. Administrators read and edit it; the owner's
publish dialog shows class titles only.

**Only a super-admin changes the configuration, and every change is audited** (ADR-0005). Each
write and its audit row — the before, the after and the actor — commit in one transaction.

**One reviewer, one output shape, the class definitions as its instructions.** The reviewer
answers every active class Yes or No, by key, and nothing else is accepted: a missing, extra or
repeated answer goes back to the model and, if it persists, the review fails. The active classes'
keys, titles and descriptions are rendered in key order into the reviewer's fixed instructions as
delimited blocks the instructions describe as definitions and nothing else, with administrator
text escaped so it cannot close its block. That rendered text is the cached part of every request,
identical for every review under the same definitions; the output shape and the tools never
change with the configuration.

**A review is pinned to the class definitions it read.** A review records the fingerprint of that
rendered text, and it is current only while its commit and its fingerprint both match the live
values. Adding a class, rewording one, or switching one on or off changes the fingerprint and the
next dialog runs a fresh review, with a fresh allowance of attempts. A weight, kind, threshold or
owners' switch change leaves reviews current and applies from the next send.

**Every decision is recorded with what it was decided under.** Publish or route, the gate stores a
declaration on the app and in the decision's audit row: the commit and the decision time, the
policy, a snapshot of each class's key, title, kind and weight, the reviewer's answers and
reasons, the owner's answers when they counted, both scores, the outcome and its reason, and the
note. Declarations stored before this shape stay readable as they were written. The deployment
record no longer carries a copy of the answers.

**The credential scan is evidence, never a verdict.** Its findings reach the reviewer only while
the seeded credentials class is active, whatever its kind, and a finding never routes an app by
itself.

**The launch configuration ships in the migration** that creates the tables, so a newly deployed
environment is configured before anyone opens the console.

## Consequences

- Administrators change the classification without a release, and each change is attributable
  from the audit trail with enough detail to put the old value back by hand.
- A configuration change never rewrites a decision already made: routed and published apps keep
  the snapshot they were decided under, and an app already waiting for review is reviewed as sent.
- Rewording or adding a class costs a fresh model review for each app whose publish dialog opens
  next, and so does any change to the reviewer's fixed instructions. Changing weights, kinds, the
  threshold or the owners' switch costs no review at all.
- The reviewer's output allowance grows with the number of active classes; there is no cap on how
  many classes an administrator adds, so the review's time limit is what bounds a very long list.
- An owner whose app routes always writes a note, even when the only reason is a review that could
  not finish.
- Administrator-written text reaches the reviewer's prompt. Titles and descriptions are the only
  administrator-controlled text there, and they are rendered inside delimited blocks the fixed
  instructions describe as class definitions and nothing else.
- The class list only grows. An inactive class stays in the administrator's table, switched off.
- Downgrading past the migration discards every administrator's edits along with the tables.

## Rejected alternatives

- **A configuration version counter, with each review and decision stamped by version.** Every
  change, including a weight edit that cannot change any answer, would invalidate every stored
  review, and the counter would be one more value to keep in step with the rows it describes. The
  per-decision snapshot and the definition fingerprint carry the same information without it.
- **Keeping the classes in code or in settings.** Every governance change would stay a release
  or a restart, made by whoever holds the deployment rather than by the administrators.
- **Deleting classes.** Stored decisions refer to classes by key; a deleted key would leave them
  pointing at nothing.
- **An output shape generated from the active classes.** Naming the keys in the schema would change
  the output tool's definition with every configuration and miss the cache; one fixed shape and a
  check that each run answered exactly its own classes give the same guarantee.
- **Owner-facing help text taken from the description.** The description is written for the
  agent, with examples that steer it, and reads badly as an explanation for the owner.

## Related

- ADR-0005 (the super-admin gate and the audit trail every change writes)
- ADR-0006 (the primary-key convention the configuration tables follow)
- ADR-0008 (the native enum type that holds a class's kind)
- ADR-0025 (the review's model spend is recorded but not charged to the owner's daily cap)
