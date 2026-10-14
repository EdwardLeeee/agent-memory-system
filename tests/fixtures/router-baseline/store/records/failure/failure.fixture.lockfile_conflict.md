<!-- agent-memory-meta
{
  "created_at": "2026-10-09T20:21:55+00:00",
  "expires_at": null,
  "id": "failure.fixture.lockfile_conflict",
  "kind": "failure",
  "last_verified": "2026-10-10",
  "priority": "normal",
  "schema_version": 3,
  "scope": "global",
  "source": "locally-verified",
  "status": "active",
  "supersedes_event": null,
  "tags": [
    "installer",
    "lockfile"
  ],
  "updated_at": "2026-10-09T20:21:55+00:00",
  "version": 1
}
-->
# failure.fixture.lockfile_conflict

## Symptom

The installer stopped with a basalt lockfile conflict.

## Attempt

Reran the installer immediately.

## Root Cause

A previous installer still held the basalt lockfile.

## Verified Fix

Delete the basalt lockfile only after the previous installer exits.

## Retry When

Before rerunning the installer.

## Evidence

Router baseline fixture.
