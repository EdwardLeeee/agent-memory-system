<!-- agent-memory-meta
{
  "created_at": "2026-10-09T20:21:55+00:00",
  "expires_at": null,
  "id": "decision.alpha.release_gate",
  "kind": "decision",
  "last_verified": "2026-10-10",
  "priority": "normal",
  "schema_version": 3,
  "scope": "project:alpha",
  "source": "user-stated",
  "status": "active",
  "supersedes_event": null,
  "tags": [
    "release",
    "granite"
  ],
  "updated_at": "2026-10-09T20:21:55+00:00",
  "version": 1
}
-->
# decision.alpha.release_gate

## Problem

Alpha releases were tagged before the smoke suite finished.

## Decision

Every alpha release passes the granite smoke suite before tagging.

## Rationale

A tag is what users install.

## Constraints

- Fixture constraint.

## Alternatives Rejected

- Fixture alternative.

## Consequences

- Fixture consequence.

## Revisit When

- Fixture revisit condition.

## Evidence

Router baseline fixture.
