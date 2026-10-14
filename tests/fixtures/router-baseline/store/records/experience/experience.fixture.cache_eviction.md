<!-- agent-memory-meta
{
  "created_at": "2026-10-09T20:21:55+00:00",
  "decision_links": [
    {
      "effect": "supports",
      "id": "decision.fixture.cache_policy"
    }
  ],
  "expires_at": null,
  "id": "experience.fixture.cache_eviction",
  "kind": "experience",
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
# experience.fixture.cache_eviction

## Situation

A schema migration left lattice cache entries in place.

## Expectation

Evicting the lattice cache on migration would fix the totals.

## Action

Evicted the lattice cache during the migration.

## Observed Outcome

Totals matched after the lattice cache eviction.

## Decision Links

- decision.fixture.cache_policy: supports

## Applicability

Any migration that changes how cached lattice values are computed.

## Evidence

Router baseline fixture.
