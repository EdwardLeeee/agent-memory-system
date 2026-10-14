<!-- agent-memory-meta
{
  "created_at": "2026-10-09T20:21:55+00:00",
  "expires_at": null,
  "id": "failure.fixture.port_in_use",
  "kind": "failure",
  "last_verified": "2026-10-10",
  "priority": "normal",
  "schema_version": 3,
  "scope": "global",
  "source": "locally-verified",
  "status": "active",
  "supersedes_event": null,
  "tags": [
    "preview",
    "port"
  ],
  "updated_at": "2026-10-09T20:21:55+00:00",
  "version": 1
}
-->
# failure.fixture.port_in_use

## Symptom

The preview server failed because port 4173 was already in use.

## Attempt

Started a second preview server.

## Root Cause

The previous preview server was still running.

## Verified Fix

Stop the previous preview server before starting a new one.

## Retry When

Before starting a preview server.

## Evidence

Router baseline fixture.
