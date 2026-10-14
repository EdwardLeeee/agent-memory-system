# ADR-0001: The lattice cache is keyed by schema version

- Status: Accepted

## Context

The lattice cache served entries computed under an older schema after every migration.

## Decision

Key every lattice cache entry by the schema version and evict the whole cache on migration.

## Consequences

The first request after a migration is slower; stale entries can no longer be served.
