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
is the share of the active scored weight answered Yes, out of 100. An app scoring above the
threshold waits for an administrator; at or below it, with no hard block answered Yes, it
publishes by itself. A class can be switched between the two kinds; switching to a hard block
clears the weight, and switching to scored requires one.

**Every class has a key that never changes.** It is made from the title when the class is added,
with a numeric suffix when that is already taken. The reviewer answers by key and stored decisions
refer to classes by key, so rewording a title never breaks an answer already given. The credential
scanner feeds its findings to the seeded credentials class by that class's key. Titles are unique
without regard to letter case.

**Classes are switched off, never deleted.** A decision made under a class keeps referring to it,
so the class stays and an inactive one simply takes no part in reviews or scores.

**A description is the reviewer's instruction, not owner-facing help.** It says when to answer
Yes, with short Yes and No examples. Administrators read and edit it; the owner's publish dialog
shows class titles only.

**Only a super-admin changes the configuration, and every change is audited** (ADR-0005). Each
write and its audit row — the before, the after and the actor — commit in one transaction.

**A review is pinned to the class definitions it read.** Its answers depend only on the active
classes' keys, titles and descriptions, so a review records a fingerprint of those, and it is
current only while its commit and its fingerprint both match the live values. Adding a class,
rewording one, or switching one on or off changes the fingerprint and the next dialog runs a fresh
review. A weight, kind, threshold or owners' switch change leaves reviews current and applies from
the next send, because the gate always scores with the live policy.

**The launch configuration ships in the migration** that creates the tables, so a newly deployed
environment is configured before anyone opens the console.

## Consequences

- Administrators change the classification without a release, and each change is attributable
  from the audit trail with enough detail to put the old value back by hand.
- A configuration change never rewrites a decision already made: routed and published apps keep
  the snapshot they were decided under, and an app already waiting for review is reviewed as sent.
- Rewording or adding a class costs a fresh model review for each app whose publish dialog opens
  next. Changing weights, kinds, the threshold or the owners' switch costs no review at all.
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
- **Owner-facing help text taken from the description.** The description is written for the
  agent, with examples that steer it, and reads badly as an explanation for the owner.

## Related

- ADR-0005 (the super-admin gate and the audit trail every change writes)
- ADR-0006 (the primary-key convention the configuration tables follow)
- ADR-0008 (the native enum type that holds a class's kind)
- ADR-0025 (the review's model spend is recorded but not charged to the owner's daily cap)
