<!-- agent-memory-meta
{
  "created_at": "2026-10-09T20:21:55+00:00",
  "decision_links": [],
  "expires_at": null,
  "id": "experience.beta.zircon_probe",
  "kind": "experience",
  "last_verified": "2026-10-10",
  "priority": "normal",
  "schema_version": 3,
  "scope": "global",
  "source": "locally-verified",
  "status": "active",
  "supersedes_event": null,
  "tags": [
    "zircon",
    "probe"
  ],
  "updated_at": "2026-10-09T20:21:55+00:00",
  "version": 1
}
-->
# experience.beta.zircon_probe

## Situation

The zircon probe ran against the beta staging path.

## Expectation

The probe would report the staging path healthy.

## Action

Ran the zircon probe for an hour.

## Observed Outcome

The zircon probe confirmed the beta path in staging.

## Decision Links

- none

## Applicability

Staging checks before a beta release.

## Evidence

Router baseline fixture.
