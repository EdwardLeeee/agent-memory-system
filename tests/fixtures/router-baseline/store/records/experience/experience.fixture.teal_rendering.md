<!-- agent-memory-meta
{
  "created_at": "2026-10-09T20:21:55+00:00",
  "decision_links": [],
  "expires_at": null,
  "id": "experience.fixture.teal_rendering",
  "kind": "experience",
  "last_verified": "2026-10-10",
  "priority": "normal",
  "schema_version": 3,
  "scope": "global",
  "source": "locally-verified",
  "status": "active",
  "supersedes_event": null,
  "tags": [
    "diagram",
    "font"
  ],
  "updated_at": "2026-10-09T20:21:55+00:00",
  "version": 1
}
-->
# experience.fixture.teal_rendering

## Situation

Teal diagrams rendered blurry in exported reports.

## Expectation

A higher export resolution would fix it.

## Action

Embedded the quartzite font in the export.

## Observed Outcome

Teal diagrams rendered sharply once the quartzite font was embedded.

## Decision Links

- none

## Applicability

Exported diagrams that use a custom font.

## Evidence

Router baseline fixture.
