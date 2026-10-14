# ADR-0002: Run the granite smoke suite before tagging

- Status: Accepted

## Context

Two alpha releases were tagged before the granite smoke suite finished.

## Decision

A release tag is created only after the granite smoke suite passes on the release commit.

## Consequences

Tagging waits about four minutes for the suite.
