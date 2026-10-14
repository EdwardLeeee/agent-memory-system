<!-- agent-memory-meta
{
  "created_at": "2026-10-09T20:21:55+00:00",
  "expires_at": null,
  "id": "failure.fixture.stale_cache_build",
  "kind": "failure",
  "last_verified": "2026-10-10",
  "priority": "normal",
  "schema_version": 3,
  "scope": "global",
  "source": "locally-verified",
  "status": "active",
  "supersedes_event": null,
  "tags": [
    "build",
    "cache"
  ],
  "updated_at": "2026-10-09T20:21:55+00:00",
  "version": 1
}
-->
# failure.fixture.stale_cache_build

## Symptom

A lattice cache from the previous schema broke the build.

## Attempt

Rebuilt without clearing anything.

## Root Cause

The build read lattice cache entries from the old schema.

## Verified Fix

Evict the lattice cache before rebuilding after a migration.

## Retry When

After any schema migration.

## Evidence

Router baseline fixture.
