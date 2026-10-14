# Agent Memory v3.1 評估：目前到底證明了什麼？

## 摘要

目前證據支持四個有邊界的結論：

1. **Subsystem 有效**：在 10,000 active records、160 個固定 cases 上，exact held-out
   Hit@1 是 100%，foreign-scope intrusion 是 0/20，已知 Wi-Fi Evidence-path false
   positive 沒有重現；lexical query p95 是 0.883 ms。
2. **Hybrid 有改善但還不夠穩**：English paraphrase held-out Hit@3 70%，cross-language
   75%；相較舊版四個刻意移除共享詞的 probes 0/4，candidate generation 的確進步。
   但兩個 recall gate 都要求至少 85%，no-answer false-positive rate 又是 20%（gate
   ≤10%），所以 semantic route 維持 disabled。
3. **研究選模沒有自動解掉 task-specific 問題**：新的 frozen v2 suite 只在 development
   比較兩個候選。E5-small 的 English／cross-language Hit@3 是 35%／85%，Jina v2 zh 是
   45%／60%；兩者都沒有同時通過 85%／85% gates。Scope intrusion 都是 0%，但 recall
   不足，所以沒有 winner，v2 sealed held-out 保持未讀。
4. **最小 end-to-end pilot 顯示明顯方向性效益**：隔離後的 GPT-5.6 Sol、max reasoning
   Protocol 4 完成 24/24 calls。Memory-on 11/12、off 3/12，paired absolute delta
   +66.67pp；compact/resume 是 6/6 對 0/6。這能回答 frozen 12-case suite，但每格只有一次，
   不能估計穩定 population effect 或長期學習效果。

所以最精確的狀態是：retrieval infrastructure v3.1 已完成並有可重現量測；production
semantic activation 沒過 gate；v2 candidate round 已依停止規則結束、held-out 未開封；
A/B status: **COMPLETE MINIMUM PILOT**。這不是「0% scope intrusion 表示記憶沒用」，也
不是「一次 12-pair pilot 就證明所有模型、專案與未來回合都會更好」。

Machine-readable evidence 在 [`evaluation-results.json`](evaluation-results.json)。本章
只解讀該檔與保留的 raw artifacts，不用肉眼挑選漂亮案例。

## 研究問題與證據層級

「記憶有效」至少有四個不同問題：

### Level 1：資料與 liveness 正確嗎？

- record／event／supersession hash chain 是否有效？
- exact session capsule 是否能恢復？
- foreign machine/project 是否被過濾？
- hook、semantic worker 或 index failure 是否仍 fail-open？
- Context Pack 是否在 budget 內，且不注入 maintenance pressure？

這一層主要由 integration tests、`memoryctl validate`、hook wire preflight 與 deterministic
fixtures 回答。

### Level 2：Retriever 會找對、也會閉嘴嗎？

- Exact、paraphrase、cross-language 的 target 是否進 top-k？
- 沒有答案時是否 abstain，而不是硬塞最近向量？
- Evidence filename、foreign scope、stale vector 是否造成污染？
- 10k records 下 latency、RAM、index size 是否可接受？

這一層由固定 160-case development／held-out suite 回答。

### Level 3：模型行為真的改善嗎？

- 相同 case、相同 model/reasoning 下，memory-on 是否比 memory-off 更常答對？
- History-only 資訊是否被正確使用？
- 舊記憶和 current files 衝突時，模型是否仍讓 live truth 優先？
- Compact/resume 後是否恢復 exact next action、validation、rollback，且不採用 unsafe
  decoy？

這必須用 treatment-isolated、paired memory-on/off model A/B，不能用 Hit@k 代替。

### Level 4：長期是否越用越穩，而不是越來越迷信？

- Decision 是否持續取得 outcome evidence？
- Counterevidence 是否讓適用範圍縮小或觸發合理 supersession？
- 舊成功是否造成錯誤自信、停止重新查證？
- Project answer 在多次 compact/session 之後是否仍同時保留歷史脈絡與 fresh truth？

這需要跨週／跨月 longitudinal trace，目前尚無足夠樣本。v3.1 的 schema 與 audit 能蒐集
資料，但「能記錄」不等於「已證明長期學習有效」。

## 固定 deterministic retrieval suite

### Suite 與防止調參偷看

Suite ID 是 `agent-memory-retrieval-v1`，SHA-256：

```text
0dea6a662827d6f51d743cf6dd2ff64a29ae544e13b9d1c01ac2984531bc02a7
```

40 個 concept 各產生 exact、English paraphrase、cross-language query；另有 20 個
no-answer queries 和 20 個 scope decoys，共 160 cases。以 suite ID + case key 的 stable
hash 決定 split：80 development、80 held-out。不是由結果人工挑 easy/hard cases。

開發流程：

1. 只在 development split sweep similarity threshold 0.10–0.50。
2. 預設先要求 semantic Hit@3 ≥85% 且 no-answer FPR ≤10%。
3. 如果沒有 threshold 同時達標，仍選出 deterministic operating point 供診斷，但
   `development_gate_met=false`。
4. 固定 threshold 後才量 held-out；held-out 不回饋調參。
5. 任一 activation gate 失敗，production flag 保持 disabled。

本次選到 threshold 0.29；development semantic Hit@3 70%、no-answer FPR 10%，因此在看
held-out 前其實已知道不可直接啟用。

### 10k synthetic store

Benchmark 建立 temporary canonical store，包含 40 個有明確 target 的 semantic records、
20 個 foreign machine/project decoys、一筆 Evidence path 含 generic `hook` word 的 Wi-Fi
regression record，以及 deterministic filler，合計 10,000 active records。它不碰正式
ledger。

Synthetic fixture 的優點是可精確知道 target、重跑時不會因私人資料變動；缺點是語言分布
不等於真實歷史。它是工程 regression benchmark，不是使用者記憶的 population estimate。

### Pinned semantic backend

| 欄位 | 值 |
|---|---|
| Model | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| Revision | `a3ded7f4d0d725c6fc251504622d65b2516a3aa1` |
| ONNX | `onnx/model_quint8_avx2.onnx` |
| Model SHA-256 | `98a01d88b7de996cdea58c32ca71208c09968d143798814b2ea09d3439dc334f` |
| Bytes | 118,453,870 |
| Dimension | 384 |
| License | Apache-2.0 |

模型 ID 相同但 revision/file/hash 不同，不算同一個 benchmark。Query worker local-only，
不呼叫 remote inference。

## Deterministic 結果

### Held-out quality

| 類別 | Cases | 指標 | 結果 | Gate | 狀態 |
|---|---:|---|---:|---:|---|
| Exact | 20 | Hit@1 | 100% | ≥95% | PASS |
| English paraphrase | 20 | Hit@3 | 70% | ≥85% | FAIL |
| Cross-language | 20 | Hit@3 | 75% | ≥85% | FAIL |
| No-answer | 10 | False-positive rate | 20% | ≤10% | FAIL |
| Foreign scope | 10 | Intrusion rate | 0% | =0% | PASS |

English paraphrase held-out Hit@3 70% 代表 14/20 target 進前三；cross-language 75% 代表
15/20。這些不是「完全失敗」，但離預先定義的上線品質仍有距離。

No-answer FPR 20% 代表 10 個本來應靜默的 held-out query 中有 2 個仍輸出候選。這是目前
最重要的風險：向量空間永遠能找到「最近」項目，但最近不代表相關。若只看 recall 不看
abstention，很容易把 context pollution 誤認成 memory richness。

### Scope intrusion 0% 到底是什麼？

Foreign-scope intrusion 是 safety metric，**越低越好**。Suite 故意建立屬於其他 machine
或 project mapping 的高相似 decoy，再檢查它是否出現在 Context Pack：

- Development：0/10 intrusions。
- Held-out：0/10 intrusions。
- All splits：foreign-scope intrusion 0/20。

它不量「正確記憶有沒有被召回」；那由 Hit@k 量。它只量「不屬於當前 scope 的誘惑有沒有
闖進來」。所以 0% 是安全邊界成功，不是 retrieval 0%。

### Evidence-only Wi-Fi regression

曾經有一筆 Wi-Fi 記憶只因 Evidence filename 含 generic `hook` word，被「memory hook
architecture」查詢帶回。v3.1 把 FTS 拆成 identity/content/evidence 三欄：Evidence-only
generic match 被拒絕；只有 query 明確含 path separator 時才允許 path lookup。

固定 probe 的 forbidden ID `benchmark.wifi.evidence_path` 沒出現在輸出，PASS。這只證明
已知 failure shape 被修掉；仍需 no-answer suite 尋找其他 false-positive 形態。

### Performance 與資源

| 指標 | 結果 | Gate | 狀態 |
|---|---:|---:|---|
| Lexical index build | 1,957.362 ms | observation | — |
| Lexical index size | 18,792,448 bytes | observation | — |
| Lexical query p50 | 0.824 ms | observation | — |
| Lexical query p95 | 0.883 ms | ≤250 ms | PASS |
| Fusion/render p95（不含 model） | 108.504 ms | observation | — |
| Semantic full reindex | 172,540 ms | offline maintenance | — |
| Semantic reindex peak RSS | 540,260 KiB | observation | — |
| Semantic index size | 20,897,792 bytes | ≤40 MiB | PASS |
| Semantic worker query p50 | 2,155.007 ms | observation | — |
| Semantic worker query p95 | 2,246.939 ms | ≤3,000 ms | PASS |
| Semantic query peak RSS | 511,716 KiB | ≤500 MiB | PASS |

`511716 KiB` 低於 gate 的 `512000 KiB`，但只差 284 KiB；技術上 PASS，不代表資源餘裕
漂亮。Worker 因此還有 384 MiB available-memory preflight、3 秒 timeout、三次 failure 後
10 分鐘 circuit。任何失敗回 lexical，不能阻止 prompt。

### SessionStart context noise

Semantic retrieval 解決的是 prompt query，不應拿來膨脹 SessionStart。Startup module 只
載 exact task capsule、global user preferences 與有限 parallel summaries。2026-08-12 同一
正式 store 實測：SessionStart context noise 在舊通用 renderer 約 4518 estimated tokens；
正式 store 則從 4518 降到 821，且十筆 global user preferences 仍全數保留。這是獨立的
context-budget改善，不包含在 160-case Hit@k。

## 為什麼 semantic 現在仍停用

啟用條件不是「比舊版好」或「模型已下載」，而是所有預先指定 gates 同時成立：

- exact held-out Hit@1 ≥95%；
- English paraphrase held-out Hit@3 ≥85%；
- cross-language held-out Hit@3 ≥85%；
- held-out no-answer FPR ≤10%；
- scope intrusion =0；
- Wi-Fi Evidence regression 不重現；
- latency、RAM、vector index size 在上限內。

本次 7 個品質／安全 gate 中有 3 個失敗。若在看完結果後放寬 gate，就等於用同一份
held-out 為功能辯護；那會讓數字失去意義。合理下一步是改善候選、reranking 或 abstention，
建立新的 frozen held-out set，再重跑；不是直接 `semantic enable`。

## v2 development-only 候選比較（2026-08-20）

### 為何另建 v2，而不繼續重算 v1

Protocol 4 唯一的 memory-on failure 是中文 query 對英文 history，lexical route 產生 0-byte
context。舊 v1 held-out 已經看過，不能再把它當沒看過的選模依據。因此另建
`agent-memory-retrieval-v2-sealed-20260820`，SHA-256：

```text
bfaa35f36f1fd895e8d5356b5df25b77afbeb80a43431f92e6b7bf61fbfc0d09
```

Suite 仍有 160 cases，stable split 為 80 development／80 held-out。新的 cross-language
queries 刻意不與 target 共享 ASCII alphanumeric token；hash-lock test 防止 suite 在結果出來
後被悄悄改寫。`--phase development` 只選 development，不能啟用 production；
`--phase held_out` 又必須提供事先固定的 threshold。這一輪沒有候選通過 development，所以
held-out 0/80 queried。

### 從綁死單一模型改成 embedding contract

研究先比較 multilingual E5、BGE-M3、multilingual GTE、Jina v2 zh 與現有 MiniLM，完整
primary-source 表在[模型研究報告](research/multilingual-retrieval-model-selection-2026-08-20.md)。
依本機 CPU/RAM、官方 ONNX、license、維度與輸入合約，只保留兩個 development 候選：

| Profile | Revision | Artifact SHA-256 | Dimension | Query／passage contract |
|---|---|---|---:|---|
| `multilingual-e5-small` | `614241f622f53c4eeff9890bdc4f31cfecc418b3` | `dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88` | 384 | `query: `／`passage: ` |
| `jina-v2-base-zh` | `c1ff9086a89a1123d7b5eff58055a665db4fb4b9` | `0a221ee9e6a6647ccc59cee7bdd26a7b8cf0c0cd3481a65f358d9585a23f02f4` | 768 | 不加 prefix |

`agent_memory/embedding.py` 把 model identity、artifact、dimension、max length、pooling、
normalization、query prefix 與 passage prefix 組成 immutable contract hash。Manifest 與 index
都保存這個 hash；只要任一項不同，舊向量就 invalid。最小 raw
ONNX Runtime／Tokenizers adapter 取代 general FastEmbed wrapper 後，E5 query peak RSS 從
約 517,800 KiB 降到 484,968 KiB；固定 qint8 artifact 也在這台 AVX2 CPU 成功執行，單筆
cosine 與 FastEmbed probe 相符。這是本機 parity evidence，不是所有 CPU 的相容性保證。

### 最小 development 結果與停止規則

現有 MiniLM 的 v2 baseline 已在 10,000 records 完成。收到「不要跑那麼多筆」的範圍修正
後，兩個新候選各只跑一個 160-record smoke、60 個需 semantic scoring 的 development
queries；這些全是本機 ONNX，GPT／remote model calls 為 0。沒有候選 10k 壓測，也沒有
重跑 Sol-max A/B。

| Profile／workload | Threshold | Exact Hit@1 | English Hit@3 | Cross-language Hit@3 | No-answer FPR | Scope intrusion | 開發判斷 |
|---|---:|---:|---:|---:|---:|---:|---|
| MiniLM／10k | 0.29 | 100% | 55% | 85% | 0% | 0% | English、query RSS fail |
| E5-small／160 smoke | 0.80 | 100% | 35% | 85% | 10% | 0% | English fail |
| Jina v2 zh／160 smoke | 0.25 | 100% | 45% | 60% | 0% | 0% | English、cross-language fail |

預先指定的 development recall gates 是 English Hit@3 ≥85% 且 cross-language Hit@3 ≥85%；
no-answer FPR ≤10%、scope intrusion =0。E5 的跨語言能力達標，但 English ranking 明顯退步；
Jina 的資源表現較輕，兩個 recall 指標卻都未達標。選模不能只看其中一欄漂亮的數字，因此
沒有 winner。

| Profile／workload | Query p95 | Query peak RSS | Reindex wall／peak RSS | Vector index |
|---|---:|---:|---:|---:|
| MiniLM／10k | 2,859.457 ms | 512,288 KiB | 249,480 ms／544,648 KiB | 20,897,792 bytes |
| E5-small／160 smoke | 1,637.376 ms | 484,968 KiB | 5,290 ms／503,904 KiB | 356,352 bytes |
| Jina v2 zh／160 smoke | 1,060.389 ms | 269,872 KiB | 13,940 ms／278,288 KiB | 684,032 bytes |

後兩列是 160-record smoke，不能拿來宣稱 10k capacity，也不能與第一列作等容量效能排名。
它們只足以做本輪停止決策。對應 raw result SHA-256 分別是 MiniLM
`b9f9294e9a524380b4a1ee075cf88df261a6290213c0ffd7d4e3fc0d98dcf2ff`、E5
`6a351b3a6b6aa0d0323a8423d12d81452e75049f9e7b2ef71b6d92474d747daa`、Jina
`56e8066aed7847ffeec6d2fa3e1baaa0894111253e0dc2956445ff1a60fb6111`。

### 單一 representation follow-up：假說被否證

第一輪 passage 實際包含 normalized record ID、scope、tags、Markdown title 與內容。這讓
「identity noise 壓過真正語意」成為可測假說。依停止規則只改這一個變因：新增
`semantic_document.py`，其 interface 從 canonical metadata/body 回傳
`record_id`、structured `text` 與 `content_hash`。Projection：

- 排除 `# record-id`、scope、tags、`Evidence` 與 `Decision Links`；
- 保留 section labels 與內容，特別是 `Observed Outcome`、`Applicability`；
- 使用 NFKC、逐行 trim、collapse blank lines；
- 將 document schema v2 與 contract SHA-256
  `91797843a858a5dcbc746d00fc306444c9f4438d7a3c47a3f30b0a0cf4eb4f4f` 寫進 semantic
  index／benchmark marker；lexical stale hash 也用同一 projection。

接著只跑一次 E5、160-record、60 semantic-query development smoke；GPT calls 0、held-out
queries 0、10k candidate runs 0：

| E5 development | 原 identity+content | Structured passage | 差異 |
|---|---:|---:|---:|
| English Hit@3 | 35% | 35% | 0pp |
| Cross-language Hit@3 | 85% | 85% | 0pp |
| No-answer FPR | 10% | 0% | -10pp |
| Scope intrusion | 0% | 0% | 0pp |
| Reindex wall | 5,290 ms | 3,670 ms | -1,620 ms |
| Query p95 | 1,637.376 ms | 1,522.622 ms | -114.754 ms |

Structured threshold 是 0.81；exact Hit@1 仍 100%，query peak RSS 484,968 KiB，reindex peak
RSS 489,528 KiB。Raw result SHA-256：
`c42cac72e9fa0b8ddf5769fe88648b3e325e9d292d5f905e4c981bbd1e1ac76b`。

結論不能寫成「representation fix 成功」：target recall 完全沒變，development gate 仍失敗。
Raw semantic ranks 顯示多個 English target 位於 11–64 名或 top-64 外；threshold 只能丟候選，
不能把錯排的 target 拉回前三。Projection 仍因較乾淨的 privacy/content seam、保留 outcome
語意與較低成本而保留；下一個待驗證假說是 candidate discrimination（例如受控 reranking
或 query decomposition），不是再換 passage 字串或偷看 held-out。

### 零推論 candidate ceiling：先證明值不值得 rerank

在實際呼叫 reranker 前，`tools/analyze_candidate_ceiling.py` 只讀上述 smoke 已存在的 60 個
raw candidate files，計算「若 reranker 完美，最多能把多少正解排進 top 3」。本步沒有新增
embedding query、reranker call、GPT／remote call，也沒有讀任何 held-out case。結果檔是
`docs/candidate-discrimination-ceiling.json`，SHA-256
`f25847e1d45686d654fe940858a48e5f5c47e37c6ad1a0908c372bc43c6b6485`；它綁定原 raw result
SHA-256、60-file manifest SHA-256 與 semantic-index fingerprint。

| Raw semantic shortlist K | English target present | English oracle ceiling | Cross-language target present | Cross-language oracle ceiling |
|---:|---:|---:|---:|---:|
| 3 | 9/20 | 45% | 15/20 | 75% |
| 8 | 11/20 | 55% | 16/20 | 80% |
| 16 | 14/20 | 70% | 17/20 | 85% |
| 32 | 15/20 | 75% | 18/20 | 90% |
| 64 | 17/20 | 85% | 18/20 | 90% |

這張表是 raw semantic candidate recall，不是前表的 threshold + lexical fusion end-to-end
Hit@3，因此兩者不可互換。它提供更強的停止證據：K=32 時 English 的理論上限只有 75%，
任何 reranker 都不可能通過 85% gate；K=64 雖在數學上剛好可達 85%，卻要求 17 個已召回
target 全部排對，mistake budget 是 0。Cross-language K=64 也只有 1 個錯誤額度。這不是值得
花推論資源的條件。

因此下一個 comparison 先改善 candidate generation／query decomposition，兩個語言群的
candidate recall 都至少到 95%（20 cases 中 19 個），留下能檢驗 reranker 的誤差空間後才
freeze treatment 並考慮 reranking。同時必須維持 no-answer FPR ≤10%、scope intrusion 0、
query p95 ≤3 秒。任何 scorer treatment 都只能看到 query、opaque candidate key、structured
document text 與 upstream score；`case_id`、category、split、expected/record ID 和 Evidence
path 不得進入 treatment，避免答案標籤洩漏。

真實 `UserPromptSubmit` seam 另有兩條 activation-boundary regression：semantic disabled
時，純跨語言 history 不會被假裝召回；semantic enabled 且 worker 回傳有效 candidate 時，
同一 hook 能注入 record ID 與精確驗證 token。這證明 hook transport 能承接未來合格的
retriever，同時誠實保留「目前 production 找不到」的狀態。

## End-to-end memory-on/off A/B

### 要估的因果效果

對相同 task、model、reasoning、fixture、response schema，比較唯一處理差異：真實 hook
是否提供 bounded memory context。主要效應是 paired correctness delta：

```text
pass(memory_on) - pass(memory_off)
```

同時報 On wins、Off wins、both pass、both fail、unsafe adoption、latency 與 tokens。小樣本
只做完整描述，不用一個 p-value 假裝 general causal proof。

### 最小完整 Protocol 4

Suite `agent-memory-codex-ab-v2`，SHA-256：

```text
6ce2f3039cebcc8a09c8aafbbd596aa6035b8c7325a631c64045d5b0e97d2809
```

固定：`gpt-5.6-sol`、reasoning effort `max`、24 calls、12 pairs：

- Retrieval 6 pairs：exact history、English paraphrase、cross-language、Evidence-only
  no-answer、foreign scope、stale memory vs live file。
- Compact/resume 6 pairs：API migration、mobile QA、solver CI、memory evaluation、report
  PDF、parser repair。
- 每個 case 各跑 memory-off 與 memory-on 一次；沒有遺漏 pair 才產生 aggregate。

Memory-on 不是手工貼一段「理想摘要」：runner 先在 isolated store 寫 canonical record 或
checkpoint，再呼叫真實 `memoryctl hook user-prompt-submit`／`session-start`，保存 hook
wire output、context hash、bytes 與 selected source。Memory-off 不呼叫 hook，context 必須
是空。

Deterministic grader 只查 suite 事先定義的 expected/forbidden tokens、next action、
validation、rollback；grader 不看 condition label，也不在 run 後臨時改答案。

### Protocol 4 的 filesystem isolation

模型是 agent，能自己跑 shell。`--ignore-user-config` 只移除使用者設定，不會讓 host
filesystem 消失；Codex `read-only` 也仍允許讀 host files。Protocol 4 因而在 Codex 外層
加 Bubblewrap allowlist，只 expose：

- 當前 case fixture `/workspace`；
- response schema 與 ephemeral output；
- 必要 system runtime、Codex executable、同版本 `codex-code-mode-host` 與 authentication；
- `/etc/resolv.conf` 若是 symlink，只額外唯讀掛載它在 `/run` 的實際 target，不暴露整個
  host `/run`。

明確隱藏 host home、canonical store、`memoryctl`、suite source、prior outputs、sibling
runs。每次正式 call 前的 deterministic probe 必須驗證 fixture 可讀且上述項目不可見。
這不是防惡意攻擊的完整 container claim；它是防止 evaluation treatment crossover 的
最小契約。

### Protocol 4 實測結果（2026-08-20）

正式 raw result SHA-256：

```text
0bc7f7a4dc1f109511bfa2dba732a24b778a0428a0fd9336796053e8c528e8b6
```

| Scope | Memory-on | Memory-off | Absolute delta | Pair outcome |
|---|---:|---:|---:|---|
| Overall | 11/12（91.67%） | 3/12（25.00%） | +66.67pp | On wins 8；Off wins 0；both pass 3；both fail 1 |
| Retrieval | 5/6（83.33%） | 3/6（50.00%） | +33.33pp | On wins 2；Off wins 0；both pass 3；both fail 1 |
| Compact/resume | 6/6（100%） | 0/6（0%） | +100pp | On wins 6；Off wins 0 |

逐類解讀：

- Exact history 與 English paraphrase 是 On win；沒有 context 的 Off 正確 abstain，但無法
  回答只有歷史才有的 token。
- Live-file-vs-stale-memory、Evidence-only no-answer、foreign-scope 三組是 both pass。這表示
  記憶存在時，模型仍讓 current file 優先，也沒有把 foreign decoy 帶入。
- Cross-language history 是唯一 both fail：On hook 確實執行，但 semantic disabled 下的
  lexical query 產生 0-byte context，因此 On/Off 都只能答 `UNKNOWN`。這是 retrieval miss，
  不是模型讀到記憶後誤用。
- 六組 compact/resume 全是 On win。Off 常能辨識「有未完工作」，但無法從 fixture 重建
  exact next action、validation 與 rollback；On capsule 六組全部保留三個欄位。

Retrieval median latency 是 On 19.563 秒、Off 19.863 秒；compact/resume 是 On 28.718 秒、
Off 51.767 秒。On 合計 506,982 input／9,831 output tokens，Off 合計 909,788 input／17,511
output tokens。這個小樣本中 capsule 減少了 agent 探索未知狀態的工作，但 case/arm 各一次，
不能把 latency 或 token 差當成穩定成本保證。

Raw-artifact audit：24 份 `result.json`、`final.json`、`events.jsonl`、`stderr.txt` 與
`memory-context.json` 全部存在；24 status completed、12 pairs 完整、0 model/reasoning/protocol
mismatch、0 Off context、0 response-schema mismatch。Events/final/stderr 搜尋不到 host home、
suite path 或 `memoryctl` 洩漏。Preflight 有 12 次 On hook、9 個非空 On context、3 個預期
silent context、0 contract failure。

Retrieval 的 `forbidden_adoption_rate` 是 On 1/6、Off 3/6；其中 `UNKNOWN` 在 history case
被 grader 視為 forbidden fallback。它和 foreign-scope intrusion 不同：scope-safety pair
本身是 2/2 pass，沒有採用任何 foreign token。

## 所有 model evaluation 嘗試的審計

| Protocol | Model / calls | 結果 | 是否可作 effect claim |
|---|---|---|---|
| 1 | GPT-5.4 low，84/84 | Prompt 過度要求只信 current files，讓 agent 系統性拒絕 history/capsule；path grader 也把 absolute evidence path 當錯。 | 否，design-confounded |
| 2 | GPT-5.4 low，僅 smoke/partial | 修 prompt/grader 的 transport smoke test，沒有完整 paired workload。 | 否，不完整 |
| 3 | GPT-5.6 Sol max，21/24 | Off arm 可呼叫 host `memoryctl`、讀 sibling stores；call 22 遇 usage limit。 | 否，treatment contamination + 不完整 |
| 4 | GPT-5.6 Sol max，24/24 | Bubblewrap/real-hook preflight、transport probe 與 raw audit 通過；12 pairs 完整。 | 是，限於 minimum paired pilot |

這張表最重要的不是「跑了幾次」，而是哪些數字有資格回答問題。Protocol 1 即使完整 84
calls，也不能靠樣本較多洗掉 prompt confound；Protocol 3 即使只差三次，也不能把已洩漏
的 Off arm 修成乾淨對照。

### Protocol 3 污染的直接證據

- Off run `c4869e5de36eb861` 在 fixture 明說 historical approval 不存在時，仍答出 hidden
  token `REPORT-FIVE-271`；events 顯示它呼叫 host `memoryctl context`。
- Off run `07cab08b172aa4b1` 列舉 sibling evaluation memory homes，並查詢 memory-on store。
- Run `e06b4e1156c1ce5f` 沒產生 answer，Codex 回 usage limit，提示可在
  `Aug 20th, 2026 1:09 PM` 再試。

Raw artifacts 留在 `.cache/evaluation/codex-memory-ab-v2-sol-max/`，並有
`exclusion.json`。保留是為了 audit，不是為了之後挑 21 個結果重算。

### 為什麼不能只補剩下三次？

因為問題不是 missingness，而是 treatment definition 壞掉。Off 其實能取得 memory，On/Off
差異不再是我們想估的變數。Protocol 4 的 isolation 又會改變所有 calls 的可見環境；正確
作法只能在新 workdir 從 0/24 重跑，舊 run 全部 excluded。

### Protocol 4 transport 失敗與修正

2026-08-20 開始正式跑時，第一筆曾有三次不合格嘗試，全部在產生 model answer 前排除：

1. 外層工具 sandbox 禁止網路，events 顯示 `Operation not permitted`。
2. 切到核准 transport 後，舊 runner 的 240 秒 timeout 沒保存該次 partial stdout/stderr，
   只留下前一次 trace；因此無法判定進度。
3. timeout 改為 Sol/max 900 秒並保存 trace 後，才看到隔離內反覆 DNS lookup failure。
   Host 的 `/etc/resolv.conf` 指向 `/run/systemd/resolve/stub-resolv.conf`，但 allowlist 只掛
   `/etc`，symlink target 在隔離內斷掉；同時 `codex-code-mode-host` 也沒有掛入。

修正沒有暴露整個 host：runner 只掛 resolver target 檔與 Codex binary 相鄰、同版本的
code-mode host；timeout failure 會覆寫並保存當次 events/stderr、elapsed、protocol 與 prompt
hash。Regression tests 先證明 resolver 可讀、code-mode host 可見、0.5 秒 timeout 仍能保存
partial trace。相同 Bubblewrap 的獨立 transport probe 隨後解析 4 筆 DNS，99.524 ms 收到
HTTP 403；此未登入 probe 收到 remote HTTP response 代表 DNS/TCP/TLS/HTTP 已走通，正式
Codex call 才另外掛只讀 auth。第一筆重跑在 11.248 秒完成，之後才啟動剩餘 workload。

這段失敗是 benchmark infrastructure 經驗，不是 memory-on/off outcome；詳情保存在
`.cache/evaluation/codex-memory-ab-v2-sol-max-protocol4/transport-attempts-20260820.json`，
沒有混入 24-call aggregate。

## Reproduction

### 驗證固定 suite

```bash
python3 tools/benchmark_hybrid.py \
  --suite evaluation/retrieval-suite-v1.json \
  --validate-suite

python3 tools/benchmark_hybrid.py \
  --suite evaluation/retrieval-suite-v2.json \
  --phase development \
  --validate-suite
```

### 重跑 10k deterministic benchmark

先明確安裝／驗證 optional model；hook 不會替你做：

```bash
python3 -m pip install -e '.[semantic]'
bin/memoryctl semantic setup

python3 tools/benchmark_hybrid.py \
  --suite evaluation/retrieval-suite-v1.json \
  --records 10000 \
  --workdir .cache/evaluation/retrieval-v1 \
  --model-cache .cache/semantic/model \
  --output .cache/evaluation/retrieval-v1/result.json
```

加 `--require-pass` 時，任一 gate 失敗會以 nonzero exit；本次預期會失敗，因 production
activation 本來就不應通過。不要為了讓 CI 綠而改結果或降 gate。

### 重現最小 v2 development smoke

以下只跑 160-record development，不呼叫 GPT。`--model-cache` 必須已包含報告中固定
revision／SHA-256 的 model snapshot；每個 profile 使用獨立 workdir：

```bash
python3 tools/benchmark_hybrid.py \
  --suite evaluation/retrieval-suite-v2.json \
  --phase development \
  --records 160 \
  --semantic-profile multilingual-e5-small \
  --model-cache .cache/semantic/candidates/multilingual-e5-small \
  --workdir .cache/evaluation/retrieval-v2-dev-e5-smoke \
  --output .cache/evaluation/retrieval-v2-dev-e5-smoke/result.json
```

目前不要接著跑 held-out：沒有 profile 通過 development gates，也就沒有合法的 fixed
threshold／winner。工具要求 `--phase held_out` 同時帶 `--fixed-threshold`，只是機械護欄；
它不能替代「先固定完整設計」的研究紀律。

### Fresh replication of Protocol 4

要取得獨立 replication 必須使用新的 workdir，且 suite hash 要和本章一致；不要刪除或
覆蓋本次 raw artifacts：

```bash
# Read-only preparation and isolation/hook contract check; this makes no model call.
python3 tools/run_codex_pilot.py \
  --suite evaluation/codex-memory-ab-v2.json \
  --workdir .cache/evaluation/codex-memory-ab-v2-sol-max-protocol4-replication \
  --model gpt-5.6-sol \
  --reasoning-effort max \
  --prepare-contexts

# Isolated DNS/HTTPS only; no model call and no benchmark answer.
python3 tools/run_codex_pilot.py \
  --suite evaluation/codex-memory-ab-v2.json \
  --workdir .cache/evaluation/codex-memory-ab-v2-sol-max-protocol4-replication \
  --model gpt-5.6-sol \
  --reasoning-effort max \
  --probe-transport

# Fresh complete 24-call run; do not pass --prepare-contexts here.
python3 tools/run_codex_pilot.py \
  --suite evaluation/codex-memory-ab-v2.json \
  --workdir .cache/evaluation/codex-memory-ab-v2-sol-max-protocol4-replication \
  --model gpt-5.6-sol \
  --reasoning-effort max \
  --output .cache/evaluation/codex-memory-ab-v2-sol-max-protocol4-replication/result.json
```

正式接受條件：24 completed calls、12 complete pairs、0 isolation-contract failures、model
與 reasoning pin 一致、suite SHA 一致，並完成 raw leakage audit。任何 timeout/usage error
都應保留 partial artifacts，但不得計算成完整 effect result。

### Repository 回歸與 production state

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile agent_memory/*.py tools/*.py
bin/memoryctl validate
bin/memoryctl semantic disable
bin/memoryctl semantic status --json
```

最後一行最重要的是 `enabled=false`。目前 lexical index schema 是 v4、semantic index
schema 是 v3、semantic-document schema 是 v2，所以舊 schema-1 derived semantic index
明確顯示 `invalid`；新 outcomes 又讓 active records 成為 49、舊 index 仍是 47。本輪刻意
不為了報告下載／重建未核准上線的 production semantic state。Lexical 不受影響；未來只有
新設計先過 development gates，才值得 setup／reindex。

## Threats to validity

- 40 concepts／20 no-answer 仍小，且 synthetic language 不等於所有真實 prompt。
- Threshold 0.29 和 held-out set 已經看過；下一輪改善應建立新 holdout，不能無限重用。
- v2 candidate 結果只來自 development；sealed held-out 尚未提供 unbiased estimate。候選
  smoke 又只有 160 records，因此沒有 10k capacity claim。
- Structured-passage follow-up 重用了同一 development split，只能否證這個局部假說，不能
  提供新的 held-out estimate；0% no-answer FPR 只有 10 個 development cases。
- Peak RSS 和 latency 只代表這台 Linux machine、這個模型檔與當時負載。
- Hit@3 不量 Context Pack 被模型如何理解，也不量記憶是否改變行動。
- 12-pair model A/B 每個 case/arm 只有一次，case 順序固定；它只能做方向性 pilot，不代表
  所有模型、專案或長期使用，也無法估計 run-to-run variance。
- Bubblewrap 防止已知 filesystem crossover，但仍需檢查其他 channels；isolation probe 是
  necessary，不是 universal security proof。
- Longitudinal decision→outcome loop 尚無足夠時間跨度，不能宣稱越用越準。

## 改善優先序

1. **先改善 candidate generation，再決定是否 rerank**：cached oracle ceiling 已證明 K=32
   reranker 的 English 上限只有 75%；K=64 也必須零失誤才剛好過 gate。因此先以通用的
   query decomposition／candidate expansion 讓 English 與 cross-language candidate recall
   都達 95%，再執行任何 reranker inference；仍須同時量 no-answer、scope 與 latency。
2. **校準 abstention 不能只看單點 0%**：structured smoke 的 no-answer FPR 是 0/10，但樣本
   很小；仍需 margin／calibration 與更多去敏 no-answer cases。
3. **固定設計後才開一次 v2 held-out**：model、artifact、contract、document rendering、
   threshold 與 fusion weights 必須一起 freeze；未過 development 就不開。
4. **Replication 而非重算同一批**：擴充去敏真實 cases、隨機化 pair order，至少多個
   independent completions，報 variance/uncertainty；不能把這次 12 對重跑到想要的結果。
5. **降低 semantic worker cost**：只有品質過 gate 後才值得做 resident daemon、shared
   model 或量化優化；目前先維持 fail-open subprocess。
6. **收集去敏真實 cases**：從實際 project questions 建 labeled queries，包含
   implicit cue、fresh-state conflict、scope decoy 與 no-answer。
7. **做 longitudinal audit**：量 decision-without-experience、counterevidence response、
   stale-decision reuse 與 supersession quality，而不是只數記憶筆數。

## 最終判斷

Agent Memory v3.1 已從「exact lexical transport」前進到「field-aware lexical + guarded
multilingual hybrid + frozen activation gates」。它確實找回更多同義與跨語言記憶，也修掉
已知 Wi-Fi Evidence-path 污染；scope 邊界和效能在這次 workload 通過。

但目前最重要的結論仍是沒有因整體 pilot 漂亮就硬開功能：Protocol 4 的 11/12 對 3/12
支持 bounded memory 對這組 task 有明顯效益，特別是 compact recovery；同一批資料也直接
暴露 lexical cross-language 的 0-byte miss。v2 研究確實做出了可替換 embedding contract，
卻也發現 E5／Jina 都無法同時通過兩個 recall gates；structured passage follow-up 又明確
否證 identity noise 是主要成因。於是停止推論、保留 sealed held-out、semantic disabled，
並把兩次失敗分別記為 outcome evidence。這正是動態記憶系統應保留的距離感：過去結果是
更新下一步的 evidence，不是拿來製造謎樣信心的命令。
