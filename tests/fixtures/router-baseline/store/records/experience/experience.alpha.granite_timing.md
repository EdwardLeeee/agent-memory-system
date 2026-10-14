<!-- agent-memory-meta
{
  "created_at": "2026-10-09T20:21:55+00:00",
  "decision_links": [],
  "expires_at": null,
  "id": "experience.alpha.granite_timing",
  "kind": "experience",
  "last_verified": "2026-10-10",
  "priority": "normal",
  "schema_version": 3,
  "scope": "project:alpha",
  "source": "locally-verified",
  "status": "active",
  "supersedes_event": null,
  "tags": [
    "granite",
    "timing"
  ],
  "updated_at": "2026-10-09T20:21:55+00:00",
  "version": 1
}
-->
# experience.alpha.granite_timing

## Situation

The granite smoke suite gated an alpha release.

## Expectation

The suite would finish within a minute.

## Action

Timed the granite smoke suite on the release runner.

## Observed Outcome

The granite smoke suite takes four minutes on the alpha runner.

## Decision Links

- none

## Applicability

Planning alpha release timing.

## Evidence

Router baseline fixture.
