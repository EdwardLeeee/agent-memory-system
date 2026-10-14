# Agent Memory v3.1 Schema 與完整性規則

## 目的

本文件定義 canonical records、supersession events、session capsules、derived SQLite index
與 machine-local runtime state。它同時描述 CLI input payload 與持久化格式；兩者相近但
不相同：CLI payload 由使用者／agent 提供語意欄位，`memoryctl` 自動補上 timestamps、
version、status、hash links 等 metadata。

Canonical files 不應手改。新增用 `memoryctl record`，修正 active decision／fact／
preference 等用 `memoryctl supersede`，task resume 用 `memoryctl checkpoint`。手改可能
破壞 event hash chain，即使 Markdown 肉眼看起來仍正常。

## Store layout

```text
<AGENT_MEMORY_HOME>/
├── manifest.json
├── records/
│   ├── preference/<id>.md
│   ├── fact/<id>.md
│   ├── decision/<id>.md
│   ├── experience/<id>.md
│   ├── failure/<id>.md
│   └── task/<id>.md
├── archive/<kind>/<id>/v<version>-<timestamp>.md
├── events/<YYYY>/<MM>/<event-id>.json
├── sessions/by-session/<safe-session-id>/HOT.md
├── migration/
│   ├── v2-manifest.json
│   └── v2-to-v3.json
├── .cache/
│   ├── index.sqlite3
│   ├── index.dirty
│   └── semantic/
│       ├── model/agent-memory-model.json
│       ├── model/onnx/model_quint8_avx2.onnx
│       ├── index.sqlite3
│       └── index.dirty
└── .write-lock
```

只有 `manifest.json`、`records/`、`archive/`、`events/`、`sessions/` 與 `migration/`
是 durable data。`.cache` 與 `.write-lock` 是 local/generated state，不進 Git，也不進
encrypted export 的語意判斷。

Machine-local config：

```text
<AGENT_MEMORY_CONFIG_HOME>/
├── machine.json
├── semantic.json
├── adapter-backups/<timestamp-pid>/...
└── runtime/
    ├── health.json
    ├── semantic.json
    ├── precompact/<safe-session-id>/precompact.json
    └── turns/...                    # v2 compatibility state；v3 hook 不建立
```

## Manifest

新 v3 store：

```json
{
  "schema_version": 3,
  "compatible_record_schema_versions": [2, 3],
  "format": "markdown-records-with-json-metadata",
  "created_at": "2026-08-12T00:00:00+00:00"
}
```

從 v2 升級後另有：

```json
{
  "upgraded_from": 2,
  "upgraded_at": "<UTC ISO-8601>"
}
```

v3 reader 接受 v2/v3 records 與 events；v2 manifest 不能包含 v3 record。v2 store 在
migration 前可讀與 validate，但所有新 record/supersede 寫入都拒絕，避免產生 v2
manifest 配 v3 records 的混合狀態。

## Stable ID

ID 必須符合：

```text
^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$
```

有效例子：

- `pref.example.recoverable_changes`
- `decision.memory.silent_hooks`
- `experience.memory.precompact_liveness`
- `failure.nvim.glibc_parser`

無效例子：

- `Decision.Memory`：含大寫。
- `memory`：沒有 separator。
- `.memory.fact`：不是字母開頭。
- `memory secret`：含空白。

ID 是跨版本的 identity。Supersede 不能換 ID；若語意完全不同，應新增另一筆 record 並在
evidence/rationale 說明關係。

## Logical scope

Portable scope 形式：

- `global`
- `project:<stable-project-id>`
- `workspace:<stable-workspace-id>`
- `machine:<machine-uuid>`

Absolute paths 不作 canonical identity，而存在 machine-local `machine.json` 的
`scope_paths` mapping。例如：

```json
{
  "machine_id": "1a2b3c4d-b0c1-4d2e-8f30-112233445566",
  "scope_paths": {
    "project:agent-memory": "/home/example/agent-memory",
    "workspace:desktop": "/home/example/Desktop"
  }
}
```

Machine scope 是 retrieval hard boundary；foreign-machine fact 不會進 Context Pack。
Project/workspace path 目前提供 affinity/ranking，不是 security isolation；明確 query 仍可
找到其他 project 的 portable knowledge。

## Record file envelope

每個 canonical record 是：

```markdown
<!-- agent-memory-meta
{
  "schema_version": 3,
  "id": "fact.demo.example",
  "kind": "fact",
  "status": "active",
  "scope": "global",
  "source": "locally-verified",
  "priority": "normal",
  "tags": ["demo"],
  "created_at": "2026-08-12T00:00:00+00:00",
  "updated_at": "2026-08-12T00:00:00+00:00",
  "last_verified": "2026-08-12",
  "expires_at": null,
  "version": 1,
  "supersedes_event": null
}
-->
# fact.demo.example

## Value

Example value.

## Evidence

Example evidence.
```

Metadata comment 由固定 start/end marker 包住，JSON 使用 UTF-8。Body 是 human-readable
Markdown，section name 同時是 validator/parser contract，不能任意改名。

## 共通 metadata

### Required persisted fields

- `schema_version`：2 或 3；新 writes 一律 3。
- `id`：stable lowercase identifier。
- `kind`：`preference|fact|decision|experience|failure|task`。
- `status`：canonical record 必須是 `active`；archive 必須是 `superseded`。
- `scope`：非空 logical scope。
- `source`：`user-stated|locally-verified|inference|migrated`。
- `priority`：`critical|high|normal|low`。
- `tags`：string array。
- `created_at`、`updated_at`：UTC ISO-8601。
- `last_verified`：ISO date。
- `version`：正整數，supersede +1。
- `supersedes_event`：第一次寫入為 null；replacement 指向 event ID。

### Optional persisted fields

- `expires_at`：ISO date/datetime、null/empty/`none`。過期後仍保留，但以 stale 呈現。
- `change_driver`、`change_reason`：replacement metadata。
- `legacy_supersedes`、`migration`：v1 migration provenance。
- `decision_links`：experience 專用，和 body 的 Decision Links 對應。

### Source 語意

- `user-stated`：使用者直接確認的 preference/correction/decision。
- `locally-verified`：由檔案、測試、runtime 或可重現實驗驗證。
- `inference`：尚未由使用者／本機驗證；不得偽裝成 constraint。
- `migrated`：從舊 ledger 搬入，需保留 migration provenance。

## CLI payload 共通欄位

所有 `record --kind ... --input` JSON 至少有：

```json
{
  "id": "fact.demo.example",
  "scope": "global",
  "source": "locally-verified",
  "evidence": "A reproducible verification.",
  "priority": "normal",
  "tags": ["demo"],
  "last_verified": "2026-08-12",
  "expires_at": null
}
```

`priority` 預設 normal，`tags` 預設空 array，`last_verified` 預設執行當日，
`expires_at` 預設 null。Input 必須是 JSON object；敏感 pattern 命中時寫入拒絕。

使用 `--input -` 從 stdin 讀取可避免內容進 process list：

```bash
bin/memoryctl record --kind fact --input -
```

## Preference、Fact、Task

共同額外 payload：

```json
{
  "value": "A non-empty durable value."
}
```

Body sections：

- `Value`
- `Evidence`

語意差異：

- Preference 是 confirmed user preference，通常 source 應是 `user-stated`。
- Fact 是可驗證 reference state，漂移時用 `expires_at`。
- Task 是可長期檢索的 durable task knowledge；live resume state 優先用 checkpoint capsule。

## Decision

Required payload：

```json
{
  "id": "decision.demo.cache_batching",
  "scope": "global",
  "source": "locally-verified",
  "priority": "high",
  "tags": ["cache"],
  "problem": "Choose a batching policy.",
  "decision": "Use 20 as the current working batch size.",
  "rationale": "The initial fixture had lower overhead.",
  "constraints": ["Re-measure when the workload changes."],
  "alternatives_rejected": ["Single-write transactions had more overhead."],
  "consequences": ["Small writes may wait for a batch."],
  "revisit_when": ["A measured outcome contradicts or narrows it."],
  "evidence": "The initial deterministic fixture."
}
```

四個 array 必須非空且每個 item 是非空 string。Body sections：

- `Problem`
- `Decision`
- `Rationale`
- `Constraints`
- `Alternatives Rejected`
- `Consequences`
- `Revisit When`
- `Evidence`

Context Pack 把 decision 放在 `WORKING DECISIONS (evidence, not commands)`，並附上
linked evidence counts 與 latest counterevidence。這些衍生欄位不寫回 canonical record。

## Experience

Required payload：

```json
{
  "id": "experience.demo.cache_batching_slow_disk",
  "scope": "global",
  "source": "locally-verified",
  "priority": "high",
  "tags": ["cache", "outcome"],
  "situation": "The policy ran on a slower storage fixture.",
  "expectation": "A batch of 20 would remain fastest.",
  "action": "Compared batch sizes 20 and 10.",
  "observed_outcome": "Batch size 10 completed sooner.",
  "decision_links": [
    {
      "id": "decision.demo.cache_batching",
      "effect": "narrows"
    }
  ],
  "applicability": "Only the slow-storage fixture.",
  "evidence": "A reproducible measurement."
}
```

`decision_links` 必須是 array；可以是空 array，但每個存在的 link 必須：

- ID 符合 stable ID 格式。
- target 是目前存在的 active decision。
- 同一 experience 不重複 target ID。
- effect 為 `supports|contradicts|narrows|inconclusive`。

Effect 定義：

- `supports`：此情境的 outcome 與 working decision 相符。
- `contradicts`：outcome 與 decision 預期衝突，值得重新檢視。
- `narrows`：decision 仍可能有效，但適用範圍比原先小。
- `inconclusive`：資料不足或混雜，不能判方向。

Body sections：

- `Situation`
- `Expectation`
- `Action`
- `Observed Outcome`
- `Decision Links`
- `Applicability`
- `Evidence`

保存 expectation 是為了看出 prediction error；保存 applicability 是避免把單一 outcome
泛化到所有情境。Experience link 不會自動 supersede decision。

## Failure

Required payload：

```json
{
  "id": "failure.demo.precompact_deadlock",
  "scope": "global",
  "source": "locally-verified",
  "priority": "high",
  "tags": ["compact", "liveness"],
  "symptom": "The active turn stopped during automatic compaction.",
  "attempt": "Required semantic review before compaction.",
  "root_cause": "Review depended on reaching a final response after compaction.",
  "verified_fix": "Make PreCompact silent and fail-open.",
  "retry_when": "Only if the platform provides a verified safe continuation contract.",
  "evidence": "A reproducer and integration tests."
}
```

Body sections：

- `Symptom`
- `Attempt`
- `Root Cause`
- `Verified Fix`
- `Retry When`
- `Evidence`

Context Pack 不只顯示 fix，也顯示 symptom、root cause 與 retry condition，避免把舊修復
不分情境套用。

## Supersede payload

Replacement payload 重複該 kind 所有 required semantic fields，但 `id` 由 CLI
`--id` 固定，不需放入 JSON。另必須有：

```json
{
  "change_driver": "new-evidence",
  "change_reason": "A measured outcome narrowed the original choice."
}
```

允許 driver：

- `user-correction`
- `new-evidence`
- `goal-change`
- `constraint-change`
- `expired`

Supersede transaction：

1. 讀 active record 與 hash。
2. 驗證 replacement payload 與 driver/reason。
3. 產生 archived copy，status 改 `superseded` 並記 `superseded_by_event`。
4. 產生 version+1 active copy，記 `supersedes_event`、driver、reason。
5. 在 write lock 內重讀並比對 hash，偵測 concurrent modification。
6. Atomic write archive、active、event；invalidate index。
7. 若 store 是 Git repo，只 commit durable paths。

## Event schema

### Recorded event

```json
{
  "schema_version": 3,
  "event_id": "event-...",
  "action": "recorded",
  "record_id": "fact.demo.example",
  "record_kind": "fact",
  "timestamp": "<UTC ISO-8601>",
  "previous_sha256": null,
  "new_sha256": "<sha256-of-full-record-envelope>"
}
```

### Superseded event

```json
{
  "schema_version": 3,
  "event_id": "event-...",
  "action": "superseded",
  "record_id": "decision.demo.example",
  "record_kind": "decision",
  "timestamp": "<UTC ISO-8601>",
  "previous_version": 1,
  "new_version": 2,
  "previous_sha256": "<hash>",
  "new_sha256": "<hash>",
  "change_driver": "new-evidence",
  "change_reason": "<concrete reason>",
  "archive_path": "archive/decision/decision.demo.example/v1-....md"
}
```

Validator 重建 archive 的 original-active envelope hash，確認 previous hash；並確認 new
hash 對應 canonical/known version。Event ID 必須唯一；每個 active `supersedes_event` 必須
存在。

`migrated-history` event 另保存 v1 的 legacy status/value/source/verification/supersedes，
避免 migration 虛構舊 rationale。

## Resume capsule

Checkpoint input required fields：

```json
{
  "state": "active",
  "active_task": "One-line current objective.",
  "paused_task": "One-line preserved work.",
  "resume_point": "One-line exact state.",
  "next_action": "One-line next executable action.",
  "validation": "One-line evidence already checked.",
  "rollback": "One-line recovery path.",
  "cwd": "/current/project"
}
```

`state` 只能 `active|idle`；七個 required fields 必須 non-empty、single-line。`cwd` 可省略，
預設目前 cwd。輸出 `HOT.md`：

```markdown
# Current Resume Capsule

- updated_at: <UTC ISO-8601>
- session_id: <stable-session-id>
- cwd: <path>
- state: active
- active_task: ...
- paused_task: ...
- resume_point: ...
- next_action: ...
- validation: ...
- rollback: ...
- read_next: retrieve only the relevant canonical records and decisions
```

整檔最多 6000 UTF-8 bytes。Session ID 若不符合安全檔名格式會轉成 SHA-256-derived safe
key，但內容仍保存原 ID。ID resolution 順序為 explicit `--session-id`，再依環境支援
`AGENT_SESSION_ID`、`CODEX_THREAD_ID`、`CLAUDE_SESSION_ID`、`GEMINI_SESSION_ID`；都沒有
時 manual checkpoint 使用 machine-derived ID。

SessionStart renderer 將此 capsule 視為最高優先資料，另外只接受 active、未過期、
`source=user-stated`、`scope=global` 的 preferences。沒有軟目標，hard limit 2800；超限先移除其他 session summaries，再以完整 preference 為單位
保留 priority prefix。較高優先 preference 若無法完整放入，後面的低優先項目不會越過它。
Decision、experience、failure、fact 與 machine state 不屬於 SessionStart schema。

## PreCompact marker

位置不是 canonical session folder，而是：

```text
~/.config/agent-memory/runtime/precompact/<safe-session-id>/precompact.json
```

Schema：

```json
{
  "schema_version": 3,
  "session_id": "...",
  "turn_id": "...",
  "cwd": "...",
  "trigger": "manual|auto",
  "checkpoint_kind": "mechanical-pre-compact",
  "captured_at": "<UTC ISO-8601>"
}
```

它不包含 active task 或 transcript，不是 semantic resume capsule，不進 Git。寫入失敗也
不改 hook 的 `continue:true`。

## Health state

```json
{
  "hooks": {
    "session_start": {
      "status": "ok|error",
      "checked_at": "<UTC ISO-8601>",
      "code": "retrieval-failed|context-trimmed",
      "estimated_tokens": 821,
      "line_count": 35,
      "preferences_total": 10,
      "preferences_included": 10,
      "other_tasks_included": 1,
      "trimmed": false
    },
    "user_prompt_submit": {
      "status": "ok|error",
      "checked_at": "<UTC ISO-8601>"
    },
    "pre_compact": {
      "status": "ok|error",
      "checked_at": "<UTC ISO-8601>"
    }
  }
}
```

未執行過的 component 由 `memoryctl health` 呈現 `never-run`，不必先寫入檔案。Error/status
code bounded 為 `retrieval-failed`、`marker-write-failed` 或 `context-trimmed`。後者只表示
adaptive composer 曾移除 lower-priority startup material，不會顯示給模型。Health 不保存
prompt、record 內容、Context Pack 或 transcript。

## SQLite FTS5 derived schema

`.cache/index.sqlite3` 包含：

```sql
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE records (
  rowid INTEGER PRIMARY KEY,
  record_id TEXT NOT NULL UNIQUE,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  scope TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  body TEXT NOT NULL,
  identity_text TEXT NOT NULL,
  content_text TEXT NOT NULL,
  evidence_text TEXT NOT NULL,
  semantic_content_hash TEXT NOT NULL,
  search_text TEXT NOT NULL
);

CREATE VIRTUAL TABLE records_fts USING fts5(
  record_id UNINDEXED,
  identity,
  content,
  evidence,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE decision_links (
  experience_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  effect TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (experience_id, decision_id)
);
```

目前 `index_schema_version = 4`。欄位語意：

- `identity_text`：stable ID、scope、tags。
- `content_text`：Evidence heading 之前的 canonical Markdown body。
- `evidence_text`：Evidence section，通常含路徑或來源描述。
- `semantic_content_hash`：versioned structured semantic document 的 SHA-256；projection
  排除 title identity、scope、tags、Evidence 與 Decision Links，保留其他 labeled sections。
  Semantic candidate 只有 hash 一致才可和當前 record 合併。
- `search_text`：三欄串接的 compatibility／diagnostic copy；實際 FTS ranking 使用分欄
  virtual table。

Evidence 欄權重低於 identity/content；若 generic query 只有 Evidence 命中且不是含 `/` 或
`\\` 的明確 path lookup，renderer 必須拒絕它。這個 schema boundary 避免來源檔名中的
generic word 冒充 record 主題。

`meta` keys：

- `index_schema_version`
- `record_schema_version`
- `generated_at`
- `source_fingerprint`
- `record_count`

Index body 是 canonical active content 的 local copy，permission 仍需 private。它不作
source of truth；缺失、dirty、corrupt 或 schema mismatch 時完整重建。

## Optional semantic derived schema

`.cache/semantic/index.sqlite3` 是另一個可丟棄 SQLite database：

```sql
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE vectors (
  record_id TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  vector BLOB NOT NULL
);
```

`meta` 至少包含：

- `index_schema_version = 3`
- `embedding_profile`、`embedding_contract_sha256`
- `semantic_document_contract_sha256`
- `model_id`、`model_revision`、`model_sha256`
- `dimension`（由 active profile 宣告，目前支援 384 或 768）
- `record_count`
- `source_fingerprint`
- `generated_at`

每個 `vector` 是 normalized float32，長度必須等於 profile dimension。`content_hash` 必須
等於 lexical index 的 `semantic_content_hash`；不一致代表 vector 對應舊內容，候選直接
丟棄。全庫 `source_fingerprint` 或 record count 不同時，status 是 `stale`；schema、model
hash、embedding contract、semantic-document contract 或 dimension 不符是 `invalid`；全部
吻合才是 `ready`。

Semantic-document schema v2 的 contract SHA-256 是
`91797843a858a5dcbc746d00fc306444c9f4438d7a3c47a3f30b0a0cf4eb4f4f`。它描述 title／metadata
排除、保留 section labels、排除 Evidence／Decision Links，以及 NFKC／line normalization。
Encoder contract 與 document contract 分開：換 passage projection 不需要重新下載相同模型，
但一定讓舊 vector index invalid。

模型 manifest 位於 `.cache/semantic/model/agent-memory-model.json`：

```json
{
  "name": "minilm-l12-v2",
  "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
  "revision": "a3ded7f4d0d725c6fc251504622d65b2516a3aa1",
  "model_file": "onnx/model_quint8_avx2.onnx",
  "sha256": "98a01d88b7de996cdea58c32ca71208c09968d143798814b2ea09d3439dc334f",
  "bytes": 118453870,
  "dimension": 384,
  "max_length": 512,
  "pooling": "mean",
  "normalization": true,
  "query_prefix": "",
  "passage_prefix": "",
  "license": "apache-2.0",
  "contract_sha256": "121bc3eb9b55fceb4b8bf5836e6e8b2fbad16d8884e3158aff7e1000df482559",
  "verified_at": "<UTC ISO-8601>"
}
```

只有 profile name、file path、byte size、revision、artifact SHA-256 與完整 contract hash
全部吻合，`model_installed` 才為 true。Contract hash 覆蓋模型身分、artifact、dimension、
max length、pooling、normalization 與 query/passage prefix。`setup` 使用 staging directory
驗證後 atomic replace；不接受「名稱看起來一樣」的模型。

Machine-local `<AGENT_MEMORY_CONFIG_HOME>/semantic.json`：

```json
{
  "enabled": false,
  "min_similarity": 0.25,
  "calibration": null
}
```

這個檔案以嚴格 parser 讀取，欄位型別與範圍都會驗證：`enabled` 必須是 boolean，
`min_similarity` 必須是 finite number 且落在 0..1，整份內容必須是 JSON object。
`AGENT_MEMORY_SEMANTIC_PROFILE` 也必須落在 profile allowlist 內。任何違反都轉成帶穩定
code 的 `SemanticUnavailable`（例如 `invalid-profile`、`config-invalid-json`、
`config-threshold-out-of-range`），**不會**被靜默 coerce 成預設值，也不會以未分類的例外
逃逸。此驗證發生在讀 `enabled` 之前，因此 semantic 停用時一份損壞的設定同樣會被拒絕。

三個 hook 對這類錯誤一律 fail-open：降級成純 lexical retrieval，輸出 `continue=true`，
只在 machine-local health 留下 error code，不注入任何錯誤文字。顯式 CLI 指令則回穩定
error code 與非零 exit，不吐 traceback。

`enabled` 是 runtime feature flag，不是「模型存在」的同義詞。正式環境目前為 false，因
held-out activation gates 未過；v2 development 候選也沒有 winner。v1 10k benchmark 在
自己的隔離 config 選到 0.29，候選 E5／Jina 則只在 development temporary configs 選到
0.80／0.25；structured E5 follow-up 選到 0.81。它們都不會覆寫 production config。若未來
正式啟用，必須透過經 gate 驗證的 maintenance flow 寫入對應 calibration metadata，不能把
default 0.25 誤稱為已校準。

Model manifest `.cache/semantic/model/agent-memory-model.json` 除了既有的 profile 欄位
外，還有 `artifacts`（每個 encoder 依賴檔的 `path`／`size`／`sha256`）與涵蓋整份 artifacts
清單的 `artifacts_sha256`。驗證分兩級：`fast_model_check` 只比對存在與大小（供 status
使用），`verify_model_artifacts` 重算每個檔案的 SHA-256 並要求宣告集合與實際檔案集合完全
一致（供 `semantic verify` 與 `semantic enable` 使用）。錯誤碼包含
`model-manifest-missing`、`manifest-artifacts-invalid`、`model-artifact-changed`、
`model-artifact-corrupt`、`model-artifact-set-mismatch`。

`.cache/semantic/model-promotion.json` 是 promotion 的 transaction journal，只在 promotion
進行中存在。若上一次執行中斷，下一次 `semantic setup` 會先依它復原先前已驗證的 model。

`.cache/index.dirty` 是有界的變更日誌，不是單純的時間戳：
`{"bounded": true|false, "record_ids": [...], "marked_at": "..."}`。`record`／`supersede`
會附上變更的 record ID；批次操作寫 `bounded: false`。超過 32 筆自動降為 unbounded。
不可讀、格式錯誤或舊版純時間戳的 marker 一律視為 unbounded —— 安全的解讀是「delta 無法
重放」。Hook 只在 `bounded` 時套用 overlay，其餘情況靜默不注入。

Machine-local `<AGENT_MEMORY_CONFIG_HOME>/semantic-activation.json` 是啟用的唯一依據。
欄位：`schema_version`、`state`、`profile`、`model_sha256`、`embedding_contract_sha256`、
`semantic_document_contract_sha256`、`dimension`、`index_source_fingerprint`、
`min_similarity`、`fusion_weights`、`suite_digest`、`split`、`metrics`、`gates`、
`reviewed_at`、`reviewed_by`，以及涵蓋上述全部欄位的 `artifact_sha256`。

`gates` 是 `{metric: {min|max}}`；載入時會**重新驗算** metrics 是否真的滿足 gates，不信任
`state` 欄位的宣告。artifact 只能由 `build_activation_artifact()` 產生，該函式對未通過
gates 的 metrics 會拒絕產出 —— 刻意不提供 CLI 指令，因為 artifact 必須來自真的通過的評估
執行，而不是操作者的斷言。

`artifact_sha256` 是**誤啟用防護**，不是安全邊界：同一個 OS user 可以重算它。它的作用是讓
被手改或半途複製的 artifact fail closed，而不是靜默啟用未校準的路徑。

四種 `activation_state`：`evaluation-required`（無 artifact／損壞／未通過 gates）、
`stale`（有效但 fingerprint 不符）、`disabled`（有效且相符但 flag false）、`approved`。
`auto` 只在 `approved` 時使用 semantic；approved 時 threshold 取自 artifact，其餘狀態
status 的 `threshold_calibrated` 為 false。

Semantic worker 的 query 輸出是一個 envelope：`candidates` 陣列加上一個 `fingerprint`
物件，後者包含 `profile`、`embedding_contract_sha256`、
`semantic_document_contract_sha256`、`dimension` 與 index 的 `source_fingerprint`。每筆
candidate 必須有唯一且非空的 `record_id`、finite 的 `score`，以及 64 位小寫 hex 的
`content_hash`。候選數不得超過請求的 limit。任何缺欄、非 hex 或長度不符的 hash、NaN／
Infinity／boolean score、重複 record ID、超量或 fingerprint 不符，都會使**整份 payload**
被拒絕並記為 `worker-invalid-output`；不做逐筆挑揀。

Query boundary 回傳 typed outcome，status 為 `success`、`no_match`、`unavailable`、
`failed` 或 `circuit_open`。`success` 與 `no_match` 代表 backend 已回答，其餘代表未回答，
此時 `auto` 必須走未修改的 lexical ranking。

`runtime/semantic.json` 只保存 bounded operational state：
`consecutive_failures`、`last_error_code`、`last_success_at`、`checked_at` 與可選的
`circuit_open_until`；不保存 query、record body、candidate IDs 或 transcript。連續三次
query failure 開 10 分鐘 circuit；一次成功清零。

Canonical `record`／`supersede` 不得依賴 semantic backend 成功。若已 enabled 且 index
ready，write commit 後可 best-effort incremental update；任何 failure 只建立 semantic
dirty marker。若未 enabled／ready，直接標 stale，待明確 `semantic reindex` 重建。

## v2→v3 migration evidence

`migration/v2-manifest.json` 是原 manifest bytes。`migration/v2-to-v3.json`：

```json
{
  "schema_version": 3,
  "migration": "v2-to-v3-manifest",
  "timestamp": "<UTC ISO-8601>",
  "old_manifest_sha256": "<hash>",
  "new_manifest_sha256": "<hash>",
  "record_policy": "preserve-v2-record-bytes"
}
```

Migration idempotence：manifest 已是 v3 時回報 already v3，不再寫第二份事件。若既有
v2 backup bytes 與目前 v2 manifest 不同，migration 停止，避免覆蓋 rollback evidence。

## Validation invariants

`memoryctl validate` 至少檢查：

- Manifest JSON 與支援 schema version。
- Record envelope、ID、kind-directory、source、priority、scope、version、date/expiry。
- Canonical record 全部 active 且 ID 唯一。
- 每種 kind 的 required Markdown sections。
- Experience link type/effect/uniqueness/decision existence。
- Archive status、supersession event link 與 reconstructed hash。
- Event uniqueness、action、schema 與前後 hashes。
- Session capsule size、required fields、path/session consistency。
- Legacy precompact metadata（若存在）的 schema/session consistency。
- 常見 secret patterns。

Validation 通過表示 ledger 結構與已定義 hash invariants 一致，不表示 body 裡的自然語言
陳述在現實中仍然正確。會漂移的 fact 仍須 fresh verify；experience 的因果解讀仍需判斷。
Semantic model/index 是 derived optional state，不納入 canonical ledger validity；用
`memoryctl semantic status --json` 另查 `model_installed`、`index_status`、coverage、failure
與 circuit。這樣向量故障不會把一份本來有效的 durable ledger 判成無法使用。
