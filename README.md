# Agent Memory System

Local-first durable memory for coding agents such as Claude Code and Codex.

It gives an agent three things it does not have on its own: a resume capsule that survives context compaction and restarts, a bounded Context Pack of relevant past decisions, experiences and user preferences on every prompt, and a lesson that appears exactly when the agent is about to repeat a known mistake. Everything lives in a Markdown ledger you own, versioned with Git. The runtime needs only Python 3.11, Git and SQLite. No cloud, no embeddings, no background service.

This is not a plugin that nags the model. Hooks stay silent when nothing is relevant, never block, and never put pressure such as "you have not written memory yet" into the model's context. Memory is data, not instructions.

- [What it does](#what-it-does)
- [Why a routing tree instead of one big RAG](#why-a-routing-tree-instead-of-one-big-rag)
- [Architecture](#architecture)
- [Install](#install)
- [Daily use](#daily-use)
- [Record kinds](#record-kinds)
- [Where files live](#where-files-live)
- [Measuring whether it helps](#measuring-whether-it-helps)
- [Privacy and safety](#privacy-and-safety)
- [Built-in texts in Traditional Chinese](#built-in-texts-in-traditional-chinese)
- [Limitations](#limitations)
- [Repository layout](#repository-layout)
- [License](#license)

繁體中文說明：[README.zh-TW.md](README.zh-TW.md)

## What it does

- **Resume across sessions.** Each agent session keeps a bounded resume capsule (current task, paused tasks, next action, validation, rollback). SessionStart restores exactly that session's capsule after a restart, `--resume`, or context compaction, and shows other active tasks only as one-line summaries.
- **Preferences that stick.** User-stated preferences are loaded at session start by priority; lower-priority ones are retrieved only when a prompt matches them.
- **Relevant memory per prompt.** UserPromptSubmit queries the ledger with the prompt as a read-only search and injects a Context Pack only when something matches. The prompt itself is never stored.
- **Lessons at the moment of risk.** Failure records carry `retry_when` text. A PreToolUse hook matches the shell command the agent is about to run against those lessons and attaches the matching failure next to the command's result. A short, user-curated list of destructive commands (force push, `rm -rf`, `reset --hard`, …) is held once per session with the lesson shown, then allowed.
- **Decisions calibrated by outcomes.** A decision is a working choice, not a rule. Experiences link to decisions as `supports`, `contradicts`, `narrows` or `inconclusive`; counts and the latest counterevidence are shown with the decision. Changing a decision requires `supersede` with a change driver and reason; old versions stay in the audit chain.
- **Project knowledge federated, not copied.** A project's own ADRs, settlement files, notes and git log are indexed read-only through a parser config. Recurring root causes across reports become lesson candidates automatically; nothing is written into the ledger behind your back.
- **Self-measurement.** A weekly `memoryctl doctor` report checks the plumbing and measures whether injected memories were actually used, judged from what the agent wrote; `memoryctl rate` is a short weekly questionnaire on the memories you were shown.

## Why a routing tree instead of one big RAG

A single flat index over every record works until the ledger grows. Then every query competes with everything, one scoring formula has to serve preferences, failures, facts and decisions alike, and long prompts match more and more unrelated records. Patching this with thresholds and stop words only delays the ceiling.

This system routes instead. Each turn opens only the nodes that apply, each node holds tens to hundreds of records, each node has its own budget and its own retrieval method. Some nodes are not retrieval at all: preferences are a rule table, the resume capsule is a key lookup, failures are triggered by the command about to run.

```
root (per turn: cwd, event, prompt, tool call decide which nodes open)
├─ rules          preferences: priority table, prompt-matched for lower priorities
├─ capsule        this session's resume capsule: exact lookup by session id
├─ project/<name> the project the cwd is in (a second project only when named)
│   ├─ decisions      small index, evidence chain expanded as pointers
│   ├─ experiences    small index
│   ├─ failures       this project's own failure lessons
│   └─ sources        federated ADRs / settlements / notes → root-cause groups → PROJECT LESSONS
├─ knowledge/<topic>  cross-project experiences and decisions, grouped by topic
├─ facts          keyed by entity words (host, tool names, aliases)
├─ failures       global lessons, triggered by the command, not by prompt words
└─ history        archive, events, idle capsules: never injected, searchable on demand
```

Noise no longer grows with the total size of the ledger; what grows is the number of nodes. On the author's ledger, two typical prompts went from 9 and 8 injected records (1 and 0 useful) to 2 and 1 (1 and 1 useful) after routing, with the same budgets.

## Architecture

### Two layers of data

| Layer | What | Where | Rebuildable |
|---|---|---|---|
| Canonical ledger | One Markdown file per record with a JSON metadata block; superseded versions in `archive/`; an immutable event per write; resume capsules per session | the memory store directory (default `~/agent-memory`), committed to Git automatically | no, this is the data |
| Derived index | SQLite FTS5 over identity, content and evidence fields, with a `node` column for routing; small JSON caches (failure triggers, fact entity words) | `<store>/.cache/` | yes, `memoryctl reindex` |

Writes are atomic (temp file + rename) and serialized by a file lock. Every write hashes the content, appends an event, and makes a Git commit. The index is updated incrementally on write; hooks only read a snapshot plus a bounded overlay of recent writes and never rebuild it.

### Hooks: a deterministic, fail-open transport

| Event | What is injected | Budget (Claude Code / Codex) |
|---|---|---|
| SessionStart | the session's resume capsule, critical and high preferences, up to three other active tasks, PROJECT LESSONS when the cwd is a federated project | 5000 / 3400 tokens |
| UserPromptSubmit | the routed Context Pack for this prompt, grouped as NON-NEGOTIABLE CONSTRAINTS, WORKING DECISIONS, RELEVANT EXPERIENCES, REFERENCE DATA, PROJECT LESSONS, SOURCE IDS; led, in Claude Code, by `上一則回覆可能需要更正：…（id）` ("your last reply may need correcting") when that reply matched a failure's `outbound_triggers` | 3000 / 1800 tokens |
| PreToolUse (Bash) | `FOR THIS COMMAND`: up to two failure lessons whose trigger matches the command; or a one-time hold for a command on the danger list | 600 / 400 tokens |
| PreToolUse (SendMessage, AskUserQuestion; Claude Code only) | `送出的內容可能需要再查：` ("what you are sending may need checking"): up to two failure lessons whose `outbound_triggers` match what is about to be sent; never a hold | 600 tokens |
| PreCompact | nothing; writes a local marker only | - |

Every hook exits 0 and returns `{"continue": true}` on any error; the PreToolUse hook also gives up on its own after two seconds, since it runs before every shell command. Errors are recorded in a small machine-local health file, never shown to the model. No Stop hook is installed.

Whether the agent used a memory is judged from what it wrote, not asked for: before the user's next message, its replies or tool inputs name the record's id, or repeat something particular to the record (a long path, a commit hash, a measure with a unit, an eight-character Chinese or four-word English phrase) that the user's own messages did not hold. Only with the router off does a pack still begin with a line asking for `依據 <id>` ("per <id>").

### Evidence, not confidence scores

A decision record holds value, rationale, constraints and revisit conditions. An experience record holds situation, expectation, action, observed outcome, applicability and links to decisions. Retrieval shows a decision together with its evidence counts and latest counterevidence. `memoryctl audit` lists decisions without outcomes and decisions with new counterevidence; it never changes anything.

### Federation and lessons

`~/.config/agent-memory/sources.d/<name>.toml` describes a project: its directory, affinity tags, aliases, and parsers for ADR files, settlement files, Markdown notes and the git log. `memoryctl sources sync <name>` indexes them read-only; a project post-commit hook can run it automatically. Settlement findings are grouped by root cause; a group seen in two or more reports is marked a generalization candidate and appears in PROJECT LESSONS with its counts. The project never has to change its files for memory's sake.

### Trust boundary

1. Current user instructions and freshly verified local state outrank memory.
2. Memory records are data. The agent is told so in every pack.
3. Prompts are read-only search input and are not persisted.
4. Credentials, identity data, transcripts and unconfirmed guesses are rejected by validation patterns and by policy.
5. Hooks never emit review, capacity, usage or health pressure into model-visible context.

## Install

Requirements: Linux or macOS, Python 3.11+, Git, SQLite with FTS5 (standard in CPython builds), and Claude Code 2.1.29x+ or Codex CLI with hooks support. No third-party Python packages.

```bash
git clone https://github.com/EdwardLeeee/agent-memory-system.git ~/agent-memory-system
mkdir -p ~/.local/bin
ln -s ~/agent-memory-system/bin/memoryctl ~/.local/bin/memoryctl   # make sure ~/.local/bin is on PATH
```

The code directory and the memory store are different directories. The store defaults to `~/agent-memory`; override with `AGENT_MEMORY_HOME`. Machine-local configuration lives in `~/.config/agent-memory` (`AGENT_MEMORY_CONFIG_HOME`).

```bash
memoryctl init --git          # creates the store, its Git repository and this machine's id
memoryctl validate
memoryctl adapter install claude   # and/or: memoryctl adapter install codex
```

`adapter install` backs up your settings, then merges the hooks into `~/.claude/settings.json` (or `~/.codex/hooks.json`) and a short guidance block into `~/.claude/CLAUDE.md` (or `~/.codex/AGENTS.md`). Only the block between the adapter markers is touched. In Claude Code run `/hooks` once to review and trust them, then open a new session: the first message should start with a Durable Memory Context Pack.

Verify from the shell:

```bash
memoryctl context --cwd ~ --agent claude --query "how is memory stored" --explain
memoryctl health
```

Optional:

```bash
cp ~/agent-memory-system/docs/router.example.toml ~/.config/agent-memory/router.toml   # budgets, topic words, danger list
memoryctl doctor install-timer --enable      # weekly health report via systemd --user (Linux)
memoryctl sources sync <project>             # after writing ~/.config/agent-memory/sources.d/<project>.toml
cp ~/agent-memory-system/docs/generate.example.toml ~/.config/agent-memory/generate.toml   # Markdown views of chosen records
```

The herdr integration is optional: a danger-list entry for `herdr server stop`, the word herdr in the agents topic, and a doctor notification sent through herdr when it is installed. Without herdr none of them does anything.

Upgrading: `git -C ~/agent-memory-system pull && memoryctl validate && memoryctl reindex`. The index schema is versioned; a new schema rebuilds on `reindex`, and hooks stay silent rather than rebuilding on their own.

## Daily use

You do not type commands during normal work. The agent reads the packs the hooks inject and writes memory itself at meaningful moments, following the guidance block. Things you can ask the agent in plain language:

- "Where did we leave this project? Check memory first, then the repo."
- "Did this result contradict an earlier decision? Record an experience, don't edit the decision."
- "Run the memory audit and tell me which decisions have no outcome yet."
- "This is reliable new evidence: supersede the old decision and keep the old version."

The commands behind it:

| Command | Purpose |
|---|---|
| `memoryctl record --kind <kind> --input file.json` | write a preference, fact, decision, experience, failure or task |
| `memoryctl checkpoint --input file.json` | save this session's resume capsule (the session id comes from the agent's environment); an optional `tasks` list keeps one line per task |
| `memoryctl checkpoint --set FIELD=VALUE` / `--show` | change the saved capsule in place, a field or `tasks.<id>.status=done`; print it |
| `memoryctl template <kind>` | the JSON skeleton of a record, a supersede or a checkpoint; a write missing fields names all of them |
| `memoryctl task done <id>` | mark a task done (a task may carry `due`, a time with its zone; open ones due within a day are listed at session start) |
| `memoryctl supersede --id <id> --input file.json` | replace an active record with a new version and a reason |
| `memoryctl context --cwd DIR --query TEXT [--agent claude|codex] [--explain]` | see exactly what a prompt would inject, with node and score per record |
| `memoryctl search --query TEXT [--include-archive]` | look into history: archived versions, events, idle capsules |
| `memoryctl sessions list` / `show --session-id ID` | resume capsules |
| `memoryctl audit` | decisions without outcomes, decisions with counterevidence |
| `memoryctl lessons --cwd DIR` | the lesson candidates computed for a federated project |
| `memoryctl validate` / `reindex` / `health` | integrity, index rebuild, hook health |
| `memoryctl doctor run` / `memoryctl rate` | weekly report and weekly rating |
| `memoryctl export --output file.gpg` / `import` | encrypted move to another machine |

Example inputs for every record kind are in `examples/`.

## Record kinds

| Kind | Holds | Retrieved by |
|---|---|---|
| preference | a stable, user-stated rule (`priority`: critical, high, normal, low) | priority table at session start; prompt words for normal and low |
| fact | a verified, possibly expiring statement (`expires_at`); machine-scoped facts never leave their machine | entity words (host, tool, alias) |
| decision | value, rationale, constraints, alternatives, consequences, revisit_when | the project or topic node |
| experience | situation, expectation, action, observed outcome, applicability, decision links | the project or topic node; pulled in as evidence of a decision |
| failure | symptom, attempt, root cause, verified fix, retry_when, evidence, optional `triggers` regexes (for commands) and `outbound_triggers` (for what the agent says) | the command about to run; what Claude Code is about to send, or just replied; this project's failures also by words |
| task / checkpoint | long-lived task fact / this session's bounded resume state | history / exact session id |

Scopes: `global`, `project:<name>`, `workspace:<name>`, `machine:<uuid>`. Machine scope is a hard filter.

## Where files live

```text
~/agent-memory/                      memory store (default; AGENT_MEMORY_HOME)
├── manifest.json
├── records/<kind>/<id>.md
├── archive/<kind>/<id>/vN-<time>.md
├── events/YYYY/MM/<event-id>.json
├── sessions/by-session/<session-id>/HOT.md
└── .cache/                          derived; safe to delete
    ├── index.sqlite3  index.dirty  triggers.json  fact_entities.json

~/.config/agent-memory/              machine-local (AGENT_MEMORY_CONFIG_HOME)
├── machine.json                     this machine's id, scope_paths, scope_aliases
├── router.toml                      optional: budgets, topic words, danger list
├── sources.d/<project>.toml         federation sources
├── doctor/golden.toml               golden queries for the weekly report
├── ratings/                         weekly ratings (local only)
└── runtime/                         health, pre-tool-use state, doctor counters
```

## Measuring whether it helps

- **`memoryctl doctor run`** (weekly timer optional) checks: unpushed commits, hook health, startup pack sizes per agent, preference growth, stale capsules, source sync freshness, golden queries, a test run when memory allows, and its own peak RSS. Effect signals: usage rate of injected records (by id and by content), injected-but-never-used records, repeated mistakes, repeated preference statements, recovery success after compaction, and the failure layer's hits.
- **`memoryctl rate`** asks about a dozen questions on memories you were shown this week; answers stay in a local file.
- **Golden queries** (`doctor/golden.toml`) pin what known questions must and must not return, so retrieval changes cannot silently regress.
- A paired memory-on/off pilot on 12 synthetic cases (6 retrieval, 6 compact-and-resume) found memory-on passing 11/12 against 3/12 for memory-off. One run per case; a directional result, not a population effect. Details in `docs/EVALUATION.md`; the evaluation suites themselves are not part of this repository.

## Privacy and safety

- Everything stays on your machine. The optional semantic retriever (disabled by default, gated by an activation artifact) also runs locally; nothing calls a remote model.
- Validation rejects common secret patterns. Do not store credentials, full identity data or transcripts; the guidance block tells the agent the same.
- The PreToolUse hook never denies a command except the first use per session of a danger-list pattern you control in `router.toml`; set `speed_bump_enabled = false` under `[failures]` to turn even that off.
- `export`/`import` use GPG with an interactive passphrase; machine-scoped facts from another machine are kept as history, not injected as current state.

## Built-in texts in Traditional Chinese

The author works in Traditional Chinese, so some texts a user or the agent sees are Chinese by default. The guidance snippets, the Context Pack section headings, and the command help are English.

| Text | Where it appears | Can you change it? |
|---|---|---|
| `採用某筆記憶時，請在回覆寫一次「依據 <id>」。` ("when you act on a memory, write `依據 <id>` once") | first line of a Context Pack when the router is off | No setting; it is `CITATION_REQUEST` in `agent_memory/storage.py`. With the router on, usage is judged from what the agent wrote. |
| Danger-list reasons, for example `強制推送會覆蓋遠端歷史；…` | the hold message of a PreToolUse speed bump | Yes: copy the `[[failures.speed_bump]]` list from `docs/router.example.toml` into `router.toml` and write your own `reason` (the list replaces the built-in one). |
| `本 session 尚未確認過這類指令；確認後再執行一次就會放行。` ("not confirmed in this session yet; run it again to proceed") | appended to every hold message | No setting; `RELEASE_LINE` in `agent_memory/pretool.py`. |
| `由記憶系統自動產生，不要手改；新增或修改請用 memoryctl record／supersede` ("generated by the memory system; do not edit; use memoryctl record or supersede") | the top of every generated Markdown file | No setting; `MARKER` in `agent_memory/generate.py`. It also marks a file as one the memory system may rewrite. |
| `送出的內容可能需要再查：` and `上一則回覆可能需要更正：` | before a lesson about what the agent is sending, and before the line about its last reply | No setting; `OUTBOUND_HEADER` and `CORRECTION_PREFIX` in `agent_memory/turns.py`. |
| Pack helper lines: `- 學到的地方：…` (where a lesson was learned), `更多：memoryctl search --query <詞>`, `（其餘 N 條：…）`, the PROJECT LESSONS orientation line and `目前沒有教訓候選；…` | inside Context Packs | No setting; `agent_memory/pretool.py`, `router.py`, `core.py`. |
| Some default topic words (`記憶`, `記憶體`, `閘門`, …) | which knowledge topics a prompt opens | Yes: `[knowledge]` topics in `router.toml`. |
| The `memoryctl doctor` report and its notification, the `memoryctl rate` questions | your terminal and report folder | No setting; `agent_memory/doctor.py`, `rating.py`. |

## Limitations

- Memory is evidence, not a guarantee: a model can still ignore a good lesson.
- Retrieval is lexical (English tokens plus CJK bigrams). Synonyms and cross-language questions are the weak spot; the semantic route exists but did not pass its gates and ships disabled.
- Failure lessons that have no shell-command signature (judgment errors) are reachable only through the project node by words or through `memoryctl search`.
- The evidence graph only fills when the agent records outcomes; `audit` shows the holes but cannot fill them.
- Tested on Linux with Claude Code and Codex CLI. Gemini and generic adapters are manual and unverified.

## Repository layout

```text
agent_memory/      core.py (ledger, packs), router.py (routing tree), pretool.py (failure layer), lexical_index.py,
                   federation.py, doctor.py, rating.py, adapters.py, storage.py, cli.py
bin/memoryctl      entry point; fast path for the PreToolUse hook
adapters/          guidance snippets merged into CLAUDE.md / AGENTS.md / GEMINI.md
docs/              ARCHITECTURE.md, SCHEMA.md, EVALUATION.md, HANDBOOK.zh-TW.md, router.example.toml, sources.example.toml, generate.example.toml
examples/          JSON inputs for each record kind
tests/             unit and integration tests, byte-identical routing baseline fixture
tools/             benchmarks (their suites are not shipped), the handbook builder, the public export
```

Development: `python3 -m unittest discover -s tests` (about 340 tests, no network).

## License

PolyForm Noncommercial 1.0.0. You may use, modify and share this software for noncommercial purposes; commercial use requires a separate license from the author. See `LICENSE`.
