# Changelog

## v3.3.0 (2026-10-28)

What weeks of real sessions showed, and the tools to check it.

- Turn kinds: a system notice or an image-only turn gets no pack; another session's message or a subagent's report gets the cwd's project only, looked up by what it says.
- A record a session was already given in full is named, not given again, until the session is compacted, resumed or cleared.
- Routing: a project node is filled by score whichever subindex holds a record, each still within its subindex's cap and decisions within half the budget; a relevance floor on the words a record matched; dated notes decay by a half-life; a project's own decision records and ADRs sit beside the ledger's decisions.
- Outbound reminders: what the agent is about to send, or has just replied, is checked against failures' outbound triggers; it is reminded, never held.
- Usage judged from what the agent wrote, not asked; ranking feedback from the ratings and that use.
- Where each record was learned (the session that wrote it), and drafts for merging one lesson learned in several places; lessons learned in one place that look alike are listed apart.
- `memoryctl replay`: what the hooks inject over whole sessions, replayed on other code, with an accuracy check that gives every difference between a replay and the recording its cause.
- Generated Markdown views of the ledger for other tools to read; tasks in resume capsules, with due dates; facts valid for a period; the commits a session made, linked to what it recorded.
- Weekly `memoryctl doctor`: whole-session numbers beside the last week's, probe questions, the speed bumps' records, and where the program may need changing.
- The test fixtures index under their own configuration only.

## v3.2.0 (2026-10-14)

First public snapshot of the code. No memory data, no history.

- Routing tree: each prompt opens only the nodes that apply (rules, the cwd's project, knowledge topics, facts), each with its own budget and retrieval.
- Failure layer: a PreToolUse hook attaches a failure lesson when the shell command about to run matches it, and holds a short danger list once per session.
- Federation: a project's own ADRs, notes, settlement files and git log are indexed read-only from a git ref, fetched into a private ref so the project's own refs never move; lesson candidates come from recurring root causes.
- Where a lesson was learned and where it applies (`origin`, `applies`, `topic`), with the origin shown when the lesson comes from another project.
- `memoryctl search` over records, archived versions, events and idle capsules.
- Weekly `memoryctl doctor` report and `memoryctl rate` questionnaire.
