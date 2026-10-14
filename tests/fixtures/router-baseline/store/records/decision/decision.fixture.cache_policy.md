<!-- agent-memory-meta
{
  "created_at": "2026-10-09T20:21:55+00:00",
  "expires_at": null,
  "id": "decision.fixture.cache_policy",
  "kind": "decision",
  "last_verified": "2026-10-10",
  "priority": "normal",
  "schema_version": 3,
  "scope": "global",
  "source": "locally-verified",
  "status": "active",
  "supersedes_event": null,
  "tags": [
    "cache",
    "lattice"
  ],
  "updated_at": "2026-10-09T20:21:55+00:00",
  "version": 1
}
-->
# decision.fixture.cache_policy

## Problem

Lattice cache entries outlived the schema they were computed under.

## Decision

Evict the lattice cache on every schema migration.

## Rationale

Serving entries from an older schema produced wrong totals.

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
