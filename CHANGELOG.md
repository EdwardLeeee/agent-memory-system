# Changelog

## v3.2.0 (2026-10-14)

First public snapshot of the code. No memory data, no history.

- Routing tree: each prompt opens only the nodes that apply (rules, the cwd's project, knowledge topics, facts), each with its own budget and retrieval.
- Failure layer: a PreToolUse hook attaches a failure lesson when the shell command about to run matches it, and holds a short danger list once per session.
- Federation: a project's own ADRs, notes, settlement files and git log are indexed read-only from a git ref, fetched into a private ref so the project's own refs never move; lesson candidates come from recurring root causes.
- Where a lesson was learned and where it applies (`origin`, `applies`, `topic`), with the origin shown when the lesson comes from another project.
- `memoryctl search` over records, archived versions, events and idle capsules.
- Weekly `memoryctl doctor` report and `memoryctl rate` questionnaire.
