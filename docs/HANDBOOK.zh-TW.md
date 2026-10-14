# Agent Memory v3.1

一套「不中斷、可修正、以實際後果校準」的本機 durable memory。Canonical
資料是使用者擁有的 Markdown ledger；`memoryctl` 負責寫入、驗證、檢索、版本鏈、
session 恢復與加密搬移；Codex hook 只負責靜默搬運必要內容。

這份 README 是操作手冊。設計論證、資料格式、測試結果與已知缺陷分別在
[架構說明](docs/ARCHITECTURE.md)、[Schema](docs/SCHEMA.md) 與
[評估報告](docs/EVALUATION.md)。離線 PDF 可用 `tools/build_handbook.py` 重建。

![Agent Memory v3.1 資料流](docs/diagrams/data-flow.svg)

## 先講結論

v3.1 刻意把兩件事分開：

1. Hook 是決定性的傳輸層：有相關資料才注入；沒有命中或發生錯誤就安靜繼續。
2. 記憶內容是機率性的證據：決策只是 working decision，後來的 outcome 可以支持、
   反駁、縮小適用範圍，或保持不確定。

因此 runtime 不會出現「用量快滿了」、「你還沒寫記憶」、「請先反思才能停止」之類
可能改變模型節奏的訊息。`PreCompact` 永遠 fail-open，memory adapter 不安裝
`Stop` hook，也不產生 review token。錯誤只留在本機 health state，想檢查時才由人或
agent 主動執行 `memoryctl health`。Semantic model 也不會在 hook 裡下載、安裝或重建。

它也不把成功一次的做法寫成永遠正確。`experience` 會保存當時情境、預期、行動、
實際後果與適用界線；`audit` 只指出缺少 outcome 或出現反證，不會自動替換 decision。
真正改 decision 必須使用 `supersede`，提供明確 change driver 與理由，舊版永遠留在
稽核鏈中。

## 它解決什麼，不解決什麼

已實作的能力：

- 跨 session／interruption／context compaction 保存 bounded resume capsule。
- SessionStart 只恢復 task capsule、全部 global user preferences 與有限的平行工作摘要；
  不預載 decisions、experiences、failures 或 machine facts。
- 每個 prompt 以 SQLite FTS5 查相關記憶，繁中使用 CJK bigrams，輸出受 token budget
  限制。
- FTS 欄位分成 identity、content 與 Evidence path；generic query 不會只因 Evidence
  filename 裡的字碰巧相同就帶回無關內容。
- 有一條可選的本機 multilingual semantic candidate route；它和 lexical route 做 late
  fusion，仍套用 active、scope、freshness、token budget 與 abstention 邊界。
- Semantic query 在獨立 worker 執行：3 秒 timeout、可用記憶體低於 384 MiB 時拒絕載入，
  連續三次失敗後開 10 分鐘 circuit breaker；任何失敗都退回 lexical，不阻斷 hook。
- decision 命中時一併展開最多兩筆 outcome evidence，反證優先。
- experience 命中時帶回其 working decision。
- 對決策顯示 `supports`、`contradicts`、`narrows`、`inconclusive` 計數與最新反證。
- 離線檢查哪些決策沒有經驗、哪些決策遇到反證。
- exact-session 優先，其他工作只顯示一行摘要；可另外精確查閱指定 session。
- Canonical Markdown、archive、immutable events 與 Git history 可人工閱讀和驗證。
- v2→v3 migration 只升級 manifest，不改寫任何既有 v2 record bytes。
- 本機加密 export/import；舊電腦的 machine-scoped facts 不會當成新電腦現況。

明確不保證的事情：

- 記憶不是命令，也不是模型權重；它不能保證 LLM 一定採取正確決策。
- Hybrid retrieval 已改善同義與跨語言 recall，但 v1 held-out 仍未達啟用 gate；v2
  development 對兩個研究候選也沒有選出 winner。因此正式設定維持 disabled，`auto`
  目前等同 lexical。
- 一筆 experience 可能有偏差；證據計數不是統計信心值。
- 回答專案現況不能只靠 memory。正確流程是「memory 提供歷史脈絡，再查目前 repo、
  tests 或 runtime」。
- 系統不會在每一回合逼 agent 反思。這保護專注力，但也表示若 agent 長期不在重要
  outcome 主動紀錄，經驗圖仍會有洞；`audit` 是離線補洞工具。
- Deterministic benchmark 證明候選檢索、拒答、scope 與效能邊界；另有一個隔離、完整的
  12-pair Sol-max pilot 支持 memory-on 對這組 cases 有明顯方向性效益，但每格只有一次，
  不能外推成穩定 population effect，也不能證明長期越用越準。

## 安裝或升級

### 新 store

```bash
cd ~/agent-memory-system
bin/memoryctl init --git
bin/memoryctl validate
bin/memoryctl adapter install codex   # 或 claude
```

同一套 hook transport 支援兩個 agent：

| Agent | Guidance 檔 | Hook 設定檔 |
|---|---|---|
| `codex` | `~/.codex/AGENTS.md` | `~/.codex/hooks.json` |
| `claude` | `~/.claude/CLAUDE.md` | `~/.claude/settings.json` |

執行 `/hooks` 檢查並信任安裝內容，再開一個新 session。Adapter 會先備份，保留非 memory
的內容，只更新它自己管理的區塊。Claude Code 的 `settings.json` 是共用設定檔，因此
`model`、`theme` 與其他人安裝的 hook 都會原樣保留；Codex 專屬的
`additionalContextLimit` 不會寫進 Claude Code 設定。兩者都不安裝 `Stop` hook。

### 從 v2 升級

先驗證，再升級，再驗證：

```bash
cd ~/agent-memory-system
bin/memoryctl validate
bin/memoryctl migrate-v2
bin/memoryctl validate
bin/memoryctl reindex
bin/memoryctl adapter install codex
```

`migrate-v2` 會把原 manifest 備份到 `migration/v2-manifest.json`，把 migration
證據寫到 `migration/v2-to-v3.json`，並保留所有舊 record bytes。若 v2 store 無效，
升級會停止，不會嘗試猜測或修補資料。

### 可選的 semantic 元件

核心 runtime 沒有第三方 Python dependency。若只用目前正式啟用的 lexical 路徑，不需
安裝 semantic extra。要重現研究測試或在隔離環境人工比較時才安裝：

```bash
cd ~/agent-memory-system
python3 -m pip install -e '.[semantic]'
bin/memoryctl semantic setup
bin/memoryctl semantic verify
bin/memoryctl semantic reindex
bin/memoryctl semantic status --json
```

`setup` 會下載並驗證固定 revision、檔名、大小與 SHA-256 的本機模型；它是明確的離線
maintenance command，不會由 hook 偷跑。以本報告的 2026-08-13 結果，請保持：

```bash
bin/memoryctl semantic disable
```

`semantic enable` 現在由 **activation evidence artifact** 強制把關，不再只檢查「模型檔案
在不在、index 建好沒」。啟用需要一份已審查的 artifact，其 metrics 實際通過自己宣告的
gates，且 profile、model SHA-256、embedding／document contract、dimension 與 corpus
fingerprint 全部仍與本 store 相符。**目前沒有任何符合資格的 artifact**，所以即使模型與
index 都 ready，`semantic enable` 也會拒絕。

`semantic status` 的 `activation_state` 有四種：

- `evaluation-required`：沒有 artifact，或 artifact 損壞、被手改、未通過 gates。
- `stale`：artifact 有效，但 profile／contract／model／corpus fingerprint 已不相符。
- `disabled`：artifact 有效且相符，但 runtime flag 是 false。
- `approved`：artifact 有效且相符，flag 為 true。只有這個狀態下 `auto` 才會使用 semantic。

Threshold 也一併綁定：approved 時使用 artifact 裡的**已校準**數值，其餘狀態 status 會明
說目前的 0.25 是 uncalibrated default，不是 production 設定。artifact 帶有涵蓋自身內容的
SHA-256；這是**誤啟用防護**，不宣稱對同一個 OS user 是安全邊界。

模型或 canonical record 改變後，status 會標為 stale；必須先 reindex，不能拿舊向量當新
內容。

Embedding 實作由 `agent_memory/embedding.py` 隔離 tokenizer、ONNX session、pooling、L2
normalization 與 query/passage prefix。每個 profile 的完整合約會算成 SHA-256，寫進 model
manifest 與 vector index；換模型、prefix 或維度後，舊 index 會被判 invalid，而不是混用。
正式預設仍是 `minilm-l12-v2`。`multilingual-e5-small` 與 `jina-v2-base-zh` 只供
development benchmark 的 `--semantic-profile` 使用，本輪沒有通過 production activation。

### Store 不在預設路徑

全域選項必須放在 subcommand 前：

```bash
bin/memoryctl --home /path/to/store validate
```

也可以設定：

```bash
export AGENT_MEMORY_HOME="/path/to/store"
export AGENT_MEMORY_CONFIG_HOME="/path/to/local-config"
```

不要改寫 `$HOME` 來切 store。Canonical store 預設是 `~/agent-memory`；本機 adapter、
machine mapping、health 與 mechanical marker 預設在 `~/.config/agent-memory`。

## 一般使用者每天怎麼用

正常聊天不需要每一回合下指令。Session 開始時 hook 恢復該 session 的 task capsule 與
精簡 global user preferences；
每次 prompt 只有在找到相關 record 時才加入 bounded Context Pack。沒命中就沒有 memory
訊息。

你可以直接用自然語言要求 agent：

- 「告訴我這個專案目前做到哪，先看記憶再查現在 repo。」
- 「這次結果有沒有推翻以前的決策？有的話先記 experience，不要直接改 decision。」
- 「跑離線 memory audit，告訴我哪些決策還沒有 outcome。」
- 「列出所有 session 摘要；只展開 website 那一個。」
- 「這是新的可靠證據，請 supersede 舊決策並保留舊版。」

專案問題的可靠回答順序應是：

1. 讀 hook 已提供的歷史決策、失敗與 resume point。
2. 用 `memoryctl context --explain` 補查真正相關的 canonical records（必要時）。
3. 查目前檔案、Git、tests、runtime 或外部狀態。
4. 明確區分「記憶裡的歷史狀態」和「這一回合剛驗證的現況」。
5. 若有可重用的新 outcome，才記 experience 或 supersede；不因為回合結束而維護記憶。

## 查閱目前記憶

### 查一個主題

```bash
bin/memoryctl context \
  --cwd "$PWD" \
  --query "PreCompact 中斷與靜默 hook" \
  --budget-tokens 1800 \
  --explain
```

`--explain` 會顯示每筆資料被選到的理由與分數。一般 hook 不開啟它，避免 meta noise。
輸出最後的 `SOURCE IDS` 是本次 Context Pack 真正使用的 canonical IDs。

可用 `--retrieval-mode` 做診斷比較：

```bash
bin/memoryctl context --cwd "$PWD" --query "同義或跨語言查詢" \
  --budget-tokens 1800 --retrieval-mode lexical --explain

bin/memoryctl context --cwd "$PWD" --query "同義或跨語言查詢" \
  --budget-tokens 1800 --retrieval-mode hybrid --explain
```

- `auto`：正式預設；semantic disabled、unavailable 或查詢失敗時走**未修改**的 lexical
  ranking（failure 路徑的輸出與 `lexical` 模式逐位元組相同）。
- `lexical`：強制只走 field-aware FTS5，可用來建立基準與除錯。
- `hybrid`：診斷模式，明確要求 semantic backend ready **且真的回答**；尚未 setup/reindex
  或 worker 失敗時 CLI 都會拒絕並附上 error code，不會靜默降級成 lexical 假裝測到
  hybrid。hook 的 `auto` 仍 fail-open。

Semantic candidate 必須帶唯一 record ID、finite score、64 位 hex content hash，以及與本
store 相符的 encoder／corpus fingerprint；任何違反都整包拒絕。不要把「向量最近」直接等同
「應該注入」。v3.1 先從兩路產生 candidates，再以 scope、
active status、content hash、threshold、field evidence 與 budget 決定是否輸出；no-answer
時寧可保持安靜。

### 看 session 摘要與精確內容

```bash
bin/memoryctl sessions list
bin/memoryctl sessions list --json
bin/memoryctl sessions show --session-id "<exact-session-id>"
```

`list` 故意不顯示 `paused_task`、`resume_point` 等細節；只有 `show` 才展開一個指定
session，避免一次把多個專案塞回上下文。

### 看離線健康狀態

```bash
bin/memoryctl health
bin/memoryctl health --json
```

Health 只保存 hook component、`ok/error`、短 error code、時間與 SessionStart 的 bounded
數量統計（estimated tokens、行數、preferences／其他 tasks 的納入數）。它不保存 prompt、
Context Pack 內容或 transcript，也不會自動顯示給模型。

### 看 decision 是否缺 outcome 或遇到反證

```bash
bin/memoryctl audit
bin/memoryctl audit --scope "project:agent-memory"
bin/memoryctl audit --json
```

目前 issue 類型：

- `decision-without-experience`：working decision 沒有任何 linked outcome。
- `new-counterevidence`：linked experience 是 `contradicts` 或 `narrows`；應先讀證據，
  再判斷是否需要 supersede。

`audit` 永遠 read-only，不會自動修正 decision，也不會在 hook 裡自動執行。

## 可複製的安全 walkthrough

以下只使用 temporary store，不碰正式記憶：

```bash
cd ~/agent-memory-system
demo_memory_home="$(mktemp -d)"
demo_config_home="$(mktemp -d)"

AGENT_MEMORY_CONFIG_HOME="$demo_config_home" \
  bin/memoryctl --home "$demo_memory_home" init --machine-id demo-machine

AGENT_MEMORY_CONFIG_HOME="$demo_config_home" \
  bin/memoryctl --home "$demo_memory_home" record \
  --kind fact --input examples/fact.json

AGENT_MEMORY_CONFIG_HOME="$demo_config_home" \
  bin/memoryctl --home "$demo_memory_home" record \
  --kind decision --input examples/decision.json

AGENT_MEMORY_CONFIG_HOME="$demo_config_home" \
  bin/memoryctl --home "$demo_memory_home" record \
  --kind experience --input examples/experience.json

AGENT_MEMORY_CONFIG_HOME="$demo_config_home" \
  bin/memoryctl --home "$demo_memory_home" checkpoint \
  --session-id demo-session --input examples/checkpoint.json

AGENT_MEMORY_CONFIG_HOME="$demo_config_home" \
  bin/memoryctl --home "$demo_memory_home" context \
  --session-id demo-session \
  --cwd /tmp/agent-memory-v3-demo

AGENT_MEMORY_CONFIG_HOME="$demo_config_home" \
  bin/memoryctl --home "$demo_memory_home" context \
  --cwd /tmp/agent-memory-v3-demo \
  --query "cache batching slow storage" \
  --budget-tokens 2400 --explain

AGENT_MEMORY_CONFIG_HOME="$demo_config_home" \
  bin/memoryctl --home "$demo_memory_home" audit

AGENT_MEMORY_CONFIG_HOME="$demo_config_home" \
  bin/memoryctl --home "$demo_memory_home" validate
```

這組 example files 也在 integration suite 中執行；文件範例不是只靠人工抄寫。

## 什麼時候寫哪一種 record

### Preference

使用者明確、穩定、跨工作可重用的偏好，例如「診斷不代表授權修改」。不要把一次性的
語氣、情緒或推測人格寫入。

```bash
bin/memoryctl record --kind preference --input preference.json
```

### Fact

經本機或可信來源驗證、未必永遠不變的事實。會漂移的 fact 應設 `expires_at`，未來
Context Pack 會把它放進 `STALE FACTS TO VERIFY`，而不是當成現況。

```bash
bin/memoryctl record --kind fact --input examples/fact.json
```

### Decision

保存「為什麼當時這樣選」，包含 constraints、被拒方案、後果與 revisit condition。
Decision 是 working choice，不是永久規則。

```bash
bin/memoryctl record --kind decision --input examples/decision.json
```

### Experience

保存 decision 之後真正發生的事：

- `situation`：當時條件。
- `expectation`：原本預期。
- `action`：做了什麼。
- `observed_outcome`：實際後果。
- `decision_links`：對一或多個 decision 的影響。
- `applicability`：這筆經驗能套用到哪裡，不能套用到哪裡。

```bash
bin/memoryctl record --kind experience --input examples/experience.json
```

Link effect 只有四種：

- `supports`：在這個情境下支持原 decision。
- `contradicts`：結果與 decision 的預期方向衝突。
- `narrows`：decision 只在更小範圍成立。
- `inconclusive`：證據不足，不能支持也不能推翻。

Experience 必須連到目前存在的 active decision。這個限制避免出現無法追查的「謎樣
信心」，但系統不會因為一筆 contradict 就自動推翻 decision。

### Failure

可重用的故障教訓，必須包含 symptom、attempt、root cause、verified fix、retry when
與 evidence。不要只記「某指令失敗」，要記未來能識別的症狀與已驗證修復。

```bash
bin/memoryctl record --kind failure --input failure.json
```

### Task / Checkpoint

`task` 是可長期檢索的 task fact；`checkpoint` 是當前 session 的 bounded resume state。
長工作開始前、interruption 後、里程碑變化時才 checkpoint：

```bash
bin/memoryctl checkpoint \
  --session-id "<stable-session-id>" \
  --input examples/checkpoint.json
```

不要用一個 session ID 覆蓋另一個 live task。沒有 lifecycle ID 的 agent 應固定設定
`AGENT_SESSION_ID` 或每次傳同一個 `--session-id`。

## 修正 decision，而不是手改 Markdown

```bash
bin/memoryctl supersede \
  --id decision.demo.cache_batching \
  --input examples/supersede-decision.json
```

允許的 `change_driver`：

- `user-correction`
- `new-evidence`
- `goal-change`
- `constraint-change`
- `expired`

還必須提供具體 `change_reason`。舊版搬到 `archive/`，active record 保留 stable ID 並
增加 version，immutable event 連結前後 SHA-256。不要直接編輯 canonical Markdown、
archive、event 或 Git history。

## Hook 在做什麼

![靜默 hook 時序](docs/diagrams/hook-sequence.svg)

- `SessionStart`：使用專用 session context interface，載入 exact session capsule、全部 active／
  未過期的 `user-stated + global` preferences；`startup/resume/clear` 最多三筆平行 task
  摘要，`compact` 不帶其他 session。它不載入 decision、experience、failure、fact 或
  machine state。
- `UserPromptSubmit`：用 prompt 當唯讀查詢；不保存 prompt。有命中才傳
  `additionalContext`，預設上限 1800 estimated tokens。
- `PreCompact`：只在 local runtime 寫 mechanical marker，永遠回
  `{"continue":true}`。Marker 失敗也不阻止 compact。
- `Stop`：v3 adapter 不安裝。舊安裝若仍呼叫，handler 是 compatibility no-op。

官方 Hooks 文件說明：hook context 會累加並可能降低模型表現、`PreCompact` 可以用
`continue:false` 阻止 compact，而 `Stop` continuation 會自動產生新的 prompt。v3 因此
只使用必要的正向傳輸，不把 maintenance pressure 放進 model context。參考
[OpenAI Codex Hooks](https://learn.chatgpt.com/docs/hooks)。

SessionStart 採內容驅動的 adaptive budget：沒有軟目標，只有內部最終安全上限 2800
與各區塊自己的上限（例如 PROJECT LESSONS ≤400），Codex `additionalContextLimit` 設為 3000。依
[官方 hook config 實作](https://github.com/openai/codex/blob/main/codex-rs/config/src/hook_config.rs)，這是把較大
`additionalContext` 落盤的 approximate-token spill threshold，不是截斷上限；內部 2800
因此仍是本系統真正的內容邊界。若異常超限，先移除其他 session 摘要，再以完整 preference
為單位保留 priority prefix；不把 preference 截成可能改變意思的半句，也不讓較低優先項目
越過已被省略的較高優先項目，更不優先犧牲當前 task capsule。

2026-08-12 在正式 store 的相同 session/cwd 實測，舊通用 renderer 是約 4518 estimated
tokens／297 個原始換行；專用 SessionStart renderer 是 821 tokens／35 行，且十筆 global
user preferences 全部保留。Codex UI 的 `+N lines` 還會受 client 折疊方式影響，所以它不是
record 數；真正驗收以輸出行數、estimated tokens 與選入種類為準。

## 檔案放在哪裡

Canonical store：

```text
~/agent-memory/
├── manifest.json
├── records/<kind>/<stable-id>.md
├── archive/<kind>/<stable-id>/vN-<time>.md
├── events/YYYY/MM/<event-id>.json
├── sessions/by-session/<session-id>/HOT.md
├── migration/
└── .cache/
    ├── index.sqlite3          # field-aware lexical index，可丟棄、可重建
    ├── index.dirty            # 有界變更日誌；hook 據此做 overlay，不重建
    └── semantic/
        ├── model/             # pinned local model；由 explicit setup 建立
        ├── index.sqlite3      # vectors + content hashes，可重建
        └── index.dirty        # canonical write 後的 stale marker
```

Machine-local state：

```text
~/.config/agent-memory/
├── machine.json
├── semantic.json              # enabled、threshold、calibration metadata
├── adapter-backups/
└── runtime/
    ├── health.json
    ├── semantic.json          # failures 與 circuit state；不含 prompt/body
    └── precompact/<session-id>/precompact.json
```

Agent adapter：

```text
~/.codex/AGENTS.md          # codex guidance
~/.codex/hooks.json         # codex hooks
~/.claude/CLAUDE.md         # claude code guidance
~/.claude/settings.json     # claude code hooks（共用設定檔，只 merge 自己的區塊）
```

Codex 自己產生的 `~/.codex/memories/` 是 secondary recall cache，不是本專案 canonical
ledger，也不由 `memoryctl` 修改。

## Portability 與隱私

```bash
bin/memoryctl export --output /safe/path/agent-memory.bundle.gpg
bin/memoryctl import --input /safe/path/agent-memory.bundle.gpg
```

GPG 在 interactive terminal 直接詢問 passphrase。`memoryctl` 不接受 passphrase CLI
argument、environment variable 或 memory record。輸入內容會用常見 secret patterns
檢查；credentials、完整 identity data、高敏感內容、完整 transcripts 與未確認猜測不應
進 canonical memory。

Machine-scoped fact 只在相同 machine UUID 當現況使用；import 到新機後會保留歷史，
但不會注入成新機事實。多個 store 只有在 Git ancestry 可證明時自動 merge；真正分歧的
decision 會停止等待人工判斷。

## Benchmark：到底有沒有用

### 10k deterministic retrieval suite（歷史 v1 held-out）

固定 suite 共 160 cases：40 exact、40 English paraphrase、40 cross-language、20
no-answer、20 foreign-scope decoys；以穩定 hash 切成 80 development 與 80 held-out。
Threshold 只可在 development split 選，held-out 只在最後讀一次：

```bash
python3 tools/benchmark_hybrid.py \
  --suite evaluation/retrieval-suite-v1.json \
  --records 10000 \
  --workdir .cache/evaluation/retrieval-v1 \
  --model-cache .cache/semantic/model \
  --output .cache/evaluation/retrieval-v1/result.json
```

2026-08-13 的 held-out 結果：

- Exact Hit@1：100%（20/20）。
- English paraphrase Hit@3：70%（14/20），未達 85% gate。
- Cross-language Hit@3：75%（15/20），未達 85% gate。
- No-answer false-positive rate：20%（2/10），高於 10% gate。
- Foreign-scope intrusion：0%（held-out 0/10；all splits 0/20）。這裡越低越好，表示
  別的 machine／project decoy 沒被帶入；它不是「完全沒召回」的意思。
- Wi-Fi Evidence-only false positive：沒有再出現。
- Lexical query p95：0.883 ms；semantic worker p95：2246.939 ms。
- Semantic query peak RSS：511716 KiB；vector index：20897792 bytes。

因此 semantic 目前仍為 disabled。這不是說 semantic 沒改善：它把舊版刻意 semantic-gap
的 0/4 提高到較大 held-out set 的 70%／75%；但「有改善」不等於「夠安全上線」，因為
false positive 和 recall gates 同時沒有過。

### 2026-08-20 v2 development-only 模型比較

為了針對 Protocol 4 唯一的 cross-language miss，另建 frozen
`agent-memory-retrieval-v2-sealed-20260820` suite，SHA-256：

```text
bfaa35f36f1fd895e8d5356b5df25b77afbeb80a43431f92e6b7bf61fbfc0d09
```

它仍是 160 cases、固定切成 80 development／80 held-out；跨語言 query 不和 target 共享
ASCII alphanumeric token。這一輪只讀 development。現有 MiniLM baseline 在較嚴格的
10,000-record workload 已經完成；兩個新候選依使用者要求只做最小 160-record local-ONNX
smoke，沒有 GPT／remote inference call，也沒有做 10k 候選壓測。

| Profile／workload | Exact Hit@1 | English Hit@3 | Cross-language Hit@3 | No-answer FPR | Scope intrusion | Query p95／peak RSS | 判斷 |
|---|---:|---:|---:|---:|---:|---:|---|
| MiniLM，10k records | 100% | 55% | 85% | 0% | 0% | 2859.457 ms／512,288 KiB | English 與 RAM gate 失敗 |
| E5-small，160-record smoke | 100% | 35% | 85% | 10% | 0% | 1637.376 ms／484,968 KiB | English gate 失敗 |
| Jina v2 zh，160-record smoke | 100% | 45% | 60% | 0% | 0% | 1060.389 ms／269,872 KiB | 兩個 recall gate 失敗 |

候選 smoke 的 latency／RAM 不能和 10k baseline 當成同容量比較；它只回答「值得不值得繼續
花資源」。答案是否定的：沒有 profile 同時達到 English 與 cross-language Hit@3 ≥85%。
因此停止調參、不選 winner、不跑 v2 sealed held-out、production semantic 維持 disabled。
這也避免一邊看 held-out、一邊改 representation 所造成的 test leakage。

同一輪接著只隔離一個非模型變因：舊 passage 會把 record ID、scope、tags 與 Markdown body
一起送進 encoder。新的 `semantic_document.py` 改為保留結構化內容與
`Observed Outcome`／`Applicability`，排除 title identity、metadata、`Evidence` path 與
`Decision Links`；lexical stale hash、full/incremental vector index、status 與 benchmark
全部共用同一個 versioned contract。

只追加一次 E5、160-record development smoke，結果是 English Hit@3 35% → 35%、
cross-language 85% → 85%、no-answer FPR 10% → 0%、scope intrusion 仍為 0%；reindex
5,290 → 3,670 ms，query p95 1,637.376 → 1,522.622 ms。它改善資料邊界與成本，**沒有改善
target recall**。Raw ranks 顯示多個 English target 落在第 11–64 名或 top-64 外，因此主因
不是 identity 噪音，也不是再微調 threshold 能解決；本輪在這裡停止，held-out 仍為 0/80。

接著只重播既有 raw cache 做零推論上限分析：semantic shortlist K=32 的 English target
coverage 是 15/20，所以即使 reranker 完美，Hit@3 也最多 75%；K=64 才到 17/20（85%），
而且沒有任何可容許的排序失誤。因此目前不跑 reranker。先讓通用 candidate generation／
query decomposition 把 English 與 cross-language candidate recall 都提高到至少 95%，才有
值得花推論成本的 reranking 問題。這一步新增 embedding／reranker／GPT calls 都是 0，
held-out 仍是 0/80。

可重跑同一個離線判斷；命令只讀既有 development cache，不會載入或呼叫模型：

```bash
python3 tools/analyze_candidate_ceiling.py \
  --workdir .cache/evaluation/retrieval-v2-dev-e5-structured-smoke \
  --output docs/candidate-discrimination-ceiling.json
```

Lexical index schema 現為 v4，semantic index schema 為 v3，semantic-document contract 為
v2。舊 derived state 會顯示 `invalid`，不會被誤當成新向量使用；目前正式 lexical route
不受影響。本輪沒有為未通過 gate 的功能重新下載或重建 production semantic index。

若只要驗證 suite contract，不會載模型：

```bash
python3 tools/benchmark_hybrid.py \
  --suite evaluation/retrieval-suite-v2.json \
  --phase development \
  --validate-suite
```

完整方法、artifact hash、候選研究與限制分別在
[評估報告](docs/EVALUATION.md)及
[多語檢索模型研究](docs/research/multilingual-retrieval-model-selection-2026-08-20.md)。

### GPT-5.6 Sol max memory-on/off A/B

Corrected Protocol 4 是最小完整 24 calls：6 組 retrieval 與 6 組 compact/resume，
每組各跑 memory-off／memory-on 一次，固定 `gpt-5.6-sol` 與 `reasoning_effort=max`。
每個 memory-on context 都由真實 hook 產生；model execution 再以 Bubblewrap 隔離，只看得
到當前 fixture，不可看到 host home、canonical `memoryctl`、suite、之前輸出或 sibling
stores。

2026-08-20 的 A/B status: **COMPLETE MINIMUM PILOT**。Protocol 4 在 frozen suite 上完成
24/24 calls、12/12 pairs：

- Overall：memory-on 11/12（91.67%），memory-off 3/12（25.00%），絕對差 +66.67pp；
  On wins 8、Off wins 0、both pass 3、both fail 1。
- Retrieval：On 5/6、Off 3/6，+33.33pp。Compact/resume：On 6/6、Off 0/6，
  +100pp。
- 唯一 On failure 是跨語言歷史：hook 有執行但 lexical route 產生空 context，On/Off 都回
  `UNKNOWN`。這與 deterministic benchmark 一致，semantic 仍不得啟用。
- 24 份 raw artifact 完整；0 failed status、0 Off context、0 pair/model/protocol mismatch，
  且 events/final/stderr 沒出現 host home、suite 或 `memoryctl` 洩漏。

這是有效的**方向性 pilot**，不是普遍因果定律：每個 case/arm 只有一次，case 是 synthetic，
而且沒有跨週 decision→outcome trace。先前 Protocol 3 的 21 個污染 calls 仍全部 excluded，
沒有混進以上數字。

重跑時先做不呼叫 model 的 hook/isolation preflight，再做 DNS/HTTPS transport probe，最後
才移除 preflight flags 跑正式 workload：

```bash
python3 tools/run_codex_pilot.py \
  --suite evaluation/codex-memory-ab-v2.json \
  --workdir .cache/evaluation/codex-memory-ab-v2-sol-max-protocol4 \
  --model gpt-5.6-sol --reasoning-effort max --prepare-contexts

python3 tools/run_codex_pilot.py \
  --suite evaluation/codex-memory-ab-v2.json \
  --workdir .cache/evaluation/codex-memory-ab-v2-sol-max-protocol4 \
  --model gpt-5.6-sol --reasoning-effort max --probe-transport

python3 tools/run_codex_pilot.py \
  --suite evaluation/codex-memory-ab-v2.json \
  --workdir .cache/evaluation/codex-memory-ab-v2-sol-max-protocol4 \
  --model gpt-5.6-sol --reasoning-effort max \
  --output .cache/evaluation/codex-memory-ab-v2-sol-max-protocol4/result.json
```

實測數字與解讀在 [評估報告](docs/EVALUATION.md)，原始機器可讀結果在
[`docs/evaluation-results.json`](docs/evaluation-results.json)。只有完整、隔離、配對的
Protocol 4 raw result SHA-256 是
`0bc7f7a4dc1f109511bfa2dba732a24b778a0428a0fd9336796053e8c528e8b6`。它回答的是這 12
個 paired cases 的 end-to-end 行為，不取代 deterministic gates 或 longitudinal evidence。

## 開發、測試與 PDF

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile agent_memory/*.py tools/*.py
bin/memoryctl validate
```

重建繁中 handbook：

```bash
python3 tools/build_handbook.py \
  --output dist/agent-memory-v3.1-handbook-zh-TW.pdf

python3 tools/verify_handbook.py \
  dist/agent-memory-v3.1-handbook-zh-TW.pdf
```

若要在保留舊檔的前提下同步到桌面：

```bash
python3 tools/build_handbook.py \
  --output dist/agent-memory-v3.1-handbook-zh-TW.pdf \
  --desktop-copy ~/Desktop/agent-memory-v3.1-handbook-zh-TW.pdf
```

Build-time 需要 `markdown-it-py`、Pygments 與本機 Chromium／Google Chrome；PDF QA
使用 Poppler 的 `pdfinfo`、`pdffonts`、`pdftotext`、`pdftoppm`。Core runtime 只有
Python 3.11 standard library（router.toml 用 `tomllib`）、Git、GPG 與 SQLite FTS5；optional semantic extra 直接使用
固定版本的 Hugging Face downloader、NumPy、ONNX Runtime 與 Tokenizers，模型則固定
revision／artifact SHA-256，不依賴 remote database 或線上 inference。

## 文件索引

- [Architecture](docs/ARCHITECTURE.md)：研究問題、資料流、hook liveness、檢索與權衡。
- [Schema](docs/SCHEMA.md)：每種 record、event、session、index 與 migration invariant。
- [Evaluation](docs/EVALUATION.md)：測試結果、10k benchmark、效益界線與 A/B protocol。
- [Memory Protocol](MEMORY_PROTOCOL.md)：agent 端最小操作規則。
- [Raw benchmark JSON](docs/evaluation-results.json)：本機實測原始值。

如果文件、程式與實測有衝突，以 freshly verified code/test behavior 為準，並把文件視為
需要修正的 defect。
