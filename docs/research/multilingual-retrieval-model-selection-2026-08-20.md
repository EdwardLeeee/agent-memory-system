# 本機 CPU 多語非對稱檢索 embedding 選型（2026-08-20）

## 結論

只建議把以下兩個模型帶進 **development-only bakeoff**：

1. **`intfloat/multilingual-e5-small`**：首選。384 維、約 0.1B 參數，和現有索引寬度相同；其訓練與推論合約明確區分 `query:`／`passage:`，最貼合「短查詢找長期記憶」的非對稱檢索問題。官方 multilingual E5 論文同時提供中文、英文 MIRACL 與多語 bitext 結果。
2. **`jinaai/jina-embeddings-v2-base-zh`**：差異化挑戰者。中英雙語、768 維、161M 參數；FastEmbed 0.8.0 原生列入支援，官方另有 162 MB quantized ONNX，且不需要文字 prefix。它的部署路徑比 BGE-M3／multilingual GTE 短，但中文到英文的公開量化證據弱於前者，因此必須靠 development cases 驗證。

不建議把 `multilingual-e5-base`、BGE-M3 或 `gte-multilingual-base` 放進本輪 CPU bakeoff：E5-base 主要增加相同模型家族的尺度；BGE-M3 的官方 ONNX 約 2.29 GB；multilingual GTE 的官方主分支沒有 ONNX、還需要 custom-code 整合。它們可以是後續硬體或 runtime 條件改變時的參考，不是目前最小風險的兩個實驗變因。

本文件只做模型與部署證據選型，**沒有開啟、重跑或調參 sealed held-out**。所有模型選擇、量化校準、threshold 與錯例分析都應只使用 development data；候選、revision、encoding contract 與決策規則凍結後，才由主線流程決定是否執行一次正式 held-out 評估。

## 範圍與判準

目標不是找排行榜最高分，而是在現有本機 durable-memory retrieval 中找出 compact、可重現、CPU 可承受且真正支援中英檢索的 encoder。主要判準是：

- 非對稱 retrieval 合約是否明確；
- 中文、英文與 cross-lingual 證據是否來自正式 benchmark，而非只有「支援多語」宣稱；
- 官方是否提供可離線保存的 ONNX，FastEmbed 0.8.0 是否可直接或經官方 custom-model 介面載入；
- model working set、CPU ISA、延遲與 float32 vector index 成本；
- license 是否允許本專案預期用途。

外部模型庫 HEAD 於 2026-08-20 唯讀確認；實作時不能使用可漂移的 `main`，應固定下表完整 SHA、下載指定檔案並保存 SHA-256。HEAD 是本次研究快照，不代表未來自動升級建議。

## 現況與硬限制

目前 repo 的 operational baseline 是 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，固定 revision [`a3ded7f4d0d725c6fc251504622d65b2516a3aa1`](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/commit/a3ded7f4d0d725c6fc251504622d65b2516a3aa1)、`onnx/model_quint8_avx2.onnx`、384 維、mean pooling + L2 normalization；repo manifest 的預期檔案大小為 118,453,870 bytes。該模型的[官方 model card](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)描述約 50 種語言與 semantic-search 用途，但沒有 E5 那種 query/document 角色合約，也沒有足以回答本案中英非對稱 retrieval 的模型卡 benchmark；因此應保留為 deployment baseline，而不是把既有可運行誤當成最合適的 retrieval objective。

本機唯讀快照為 Intel i5-8250U（4 cores／8 threads、AVX2、無 AVX-512/VNNI）、7.7 GiB RAM、2 GiB swap；檢查命令是 `lscpu`、`rg -m1 '^flags' /proc/cpuinfo`、`free -h`。現有 worker 使用 2 threads、`CPUExecutionProvider`、3 秒 query timeout，available memory 低於 384 MiB 時拒絕啟動；benchmark 另有 40 MiB vector-index 上限。這些是本機與目前 worktree 的實測／程式約束，不是模型供應商保證。

## 候選總表

「模型／ONNX 大小」是官方 repository 顯示的靜態檔案大小，不等於 peak RSS；runtime 還包含 tokenizer、ORT session、activations、輸入長度與 allocator。FastEmbed 狀態是目前 repo 固定的 **0.8.0**，不是對未來版本的推測。

| 模型 | License | 參數／輸出 | 官方模型與 ONNX 快照 | Encoding contract | FastEmbed 0.8.0／本機適配 | 本輪判斷 |
|---|---|---:|---|---|---|---|
| [`multilingual-e5-small`](https://huggingface.co/intfloat/multilingual-e5-small/blob/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md) | MIT | 約 0.1B；384-d | revision [`614241…`](https://huggingface.co/intfloat/multilingual-e5-small/commit/614241f622f53c4eeff9890bdc4f31cfecc418b3)；[`model.safetensors`](https://huggingface.co/intfloat/multilingual-e5-small/blob/614241f622f53c4eeff9890bdc4f31cfecc418b3/model.safetensors) 約 471 MB；[官方 ONNX 目錄](https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3/onnx)含約 470 MB FP32、235 MB O4、118 MB qint8 變體 | 所有字串都必須加英文 prefix；query 用 `query: `，記憶／文件用 `passage: `；mean pooling、normalize，最長 512 tokens | 未列入 0.8.0 built-ins；Qdrant 的[官方 custom-model 範例](https://github.com/qdrant/fastembed#custom-models)正以本模型示範 384-d、mean pooling、normalization。官方 118 MB 檔名標示 AVX512/VNNI；後續本機 ORT smoke 證明它可在這台 AVX2 CPU 執行，且單筆 cosine 與 FastEmbed 相符。這是本機實證，不是供應商對所有 AVX2 CPU 的保證 | **Bakeoff 首選** |
| [`multilingual-e5-base`](https://huggingface.co/intfloat/multilingual-e5-base/blob/d128750597153bb5987e10b1c3493a34e5a4502a/README.md) | MIT | 約 0.3B；768-d | revision [`d12875…`](https://huggingface.co/intfloat/multilingual-e5-base/commit/d128750597153bb5987e10b1c3493a34e5a4502a)；[官方 ONNX 目錄](https://huggingface.co/intfloat/multilingual-e5-base/tree/d128750597153bb5987e10b1c3493a34e5a4502a/onnx)約 1.11 GB FP32、555 MB O4、279 MB qint8 | 與 small 完全相同：`query: `／`passage: `、mean pooling、normalize、512 tokens | 未列入 0.8.0 built-ins；可用 custom model。官方小型 qint8 仍是 AVX512/VNNI 導向；768-d 也讓 index 加倍 | 不進本輪；若 small 明確容量不足，再做第二階段 scale-up |
| [`BAAI/bge-m3`](https://huggingface.co/BAAI/bge-m3/blob/5617a9f61b028005a4858fdac845db406aefb181/README.md) | MIT | 568M；1024-d | revision [`5617a9…`](https://huggingface.co/BAAI/bge-m3/commit/5617a9f61b028005a4858fdac845db406aefb181)；[官方 ONNX](https://huggingface.co/BAAI/bge-m3/tree/5617a9f61b028005a4858fdac845db406aefb181/onnx)由小型 graph 加約 2.27 GB external data 組成，目錄約 2.29 GB | 官方 dense usage 不要求 query instruction；同一 encoder 接收 query/document；normalize。另可輸出 sparse 與 multi-vector，最長 8192 tokens | 未列入 0.8.0 built-ins；即使只取 dense，模型檔已遠超目前 384 MiB 啟動 guard。1024-d index、長序列 attention 與 custom integration 風險高 | 證據強，但不 compact；排除本輪 |
| [`Alibaba-NLP/gte-multilingual-base`](https://huggingface.co/Alibaba-NLP/gte-multilingual-base/blob/9bbca17d9273fd0d03d5725c7a4b0f6b45142062/README.md) | Apache-2.0 | 304–305M；768-d，可截取 32 的倍數至 768-d | revision [`9bbca1…`](https://huggingface.co/Alibaba-NLP/gte-multilingual-base/commit/9bbca17d9273fd0d03d5725c7a4b0f6b45142062)；官方主分支 [`model.safetensors`](https://huggingface.co/Alibaba-NLP/gte-multilingual-base/blob/9bbca17d9273fd0d03d5725c7a4b0f6b45142062/model.safetensors)約 611 MB，主分支[檔案樹](https://huggingface.co/Alibaba-NLP/gte-multilingual-base/tree/9bbca17d9273fd0d03d5725c7a4b0f6b45142062)沒有 ONNX | 官方 reference usage 直接編碼原文、不加文字 prefix；取 CLS token、L2 normalize。最多 8192 tokens；dense 另支援 Matryoshka dimension | 未列入 0.8.0 built-ins；官方載入需要 `trust_remote_code=True`，主分支又無 ONNX，不能把第三方 conversion 當官方 local-ONNX 路徑 | 品質與可縮維很有吸引力，但 integration 變因過多；排除本輪 |
| [`jina-embeddings-v2-base-zh`](https://huggingface.co/jinaai/jina-embeddings-v2-base-zh/blob/c1ff9086a89a1123d7b5eff58055a665db4fb4b9/README.md) | Apache-2.0 | 161M；768-d | revision [`c1ff90…`](https://huggingface.co/jinaai/jina-embeddings-v2-base-zh/commit/c1ff9086a89a1123d7b5eff58055a665db4fb4b9)；[官方 ONNX 目錄](https://huggingface.co/jinaai/jina-embeddings-v2-base-zh/tree/c1ff9086a89a1123d7b5eff58055a665db4fb4b9/onnx)：FP32 約 641 MB、FP16 約 321 MB、quantized 約 162 MB | 不加 query/document prefix；raw text、mean pooling、L2 normalize；最多 8192 tokens | **0.8.0 built-in**，官方 metadata 指向 `onnx/model.onnx`（約 0.64 GB）；若改用官方 162 MB quantized 檔，需 custom entry 並驗證 output parity | **Bakeoff 挑戰者** |

### License 注意事項

上表 license 取自各官方 model card。這是模型 artifact 的授權摘要，不是法律意見；部署前仍應保存當時 README/LICENSE 與固定 revision。`jina-embeddings-v3` 雖由 FastEmbed 支援，但官方模型卡為 CC-BY-NC-4.0、約 570M／1024-d，且官方 ONNX 約 2.29 GB，因此沒有必要在本輪加入一個授權和 footprint 都更差的變因（[官方 model card](https://huggingface.co/jinaai/jina-embeddings-v3)）。

## 中文、英文與 cross-lingual 證據

「中文 benchmark 高分」只證明中文 query 找中文 corpus；「多語支援」也不等於中文 query 能找英文記憶。下表因此分開列證據型態。

| 模型 | 單語中文／英文 retrieval | 真正 cross-lingual 證據 | 可得出的保守結論 |
|---|---|---|---|
| multilingual E5 small/base | [multilingual E5 technical report](https://arxiv.org/html/2402.05672)的 MIRACL：small 中文 nDCG@10 45.9、英文 48.0；base 中文 51.5、英文 51.2。MIRACL 16 語平均 nDCG@10 為 60.8／62.3 | 同一報告在超過 100 語的 bitext mining（Tatoeba 類型）平均為 small 64.2、base 68.1；訓練亦含 NLLB translation pairs。不過論文沒有單獨公布「中文 query → 英文 durable-memory passage」分數 | 有明確跨語對齊與中、英文各自 retrieval 證據；仍須用 development 中的 zh→en、en→zh 記憶案例驗證實際 task |
| BGE-M3 | [BGE-M3 論文](https://arxiv.org/html/2402.03216)報告 MIRACL dense 中文 Recall@100 96.9 | MKQA 是多語 query 檢索 English Wikipedia passages；dense Recall@20：zh_cn 66.4、zh_hk 65.8、zh_tw 64.8，全部語言平均 67.8 | 這組候選中最直接的中→英量化證據之一，但 footprint 不符合本輪 compact CPU 條件 |
| multilingual GTE | [mGTE 論文](https://arxiv.org/html/2407.19669)表 3 的 MTEB retrieval：英文 61.40、中文 62.72；表 4 的 MIRACL 平均 62.1 | 同一篇表 4 的 MKQA cross-lingual Recall@20 平均 65.8；官方 model card 另示範中文問題對英文答案的高相似度，但單一示例不是 benchmark | retrieval 品質很有競爭力，且 Matryoshka 可降低 index 寬度；目前缺官方 main-branch ONNX，部署不確定性壓過潛在收益 |
| Jina v2 base zh | [官方 model card](https://huggingface.co/jinaai/jina-embeddings-v2-base-zh)明列 Chinese-English bilingual、monolingual 與 cross-lingual use case；[Jina v2 論文](https://arxiv.org/html/2402.17016)說明 161M 雙語模型、retrieval pair 訓練與 bidirectional InfoNCE | model card 明確展示中英互搜，但論文的完整量化表主要是德／西語版本，不足以當作中文 checkpoint 的獨立 zh→en benchmark | deployment 證據強、中文量化證據相對弱；正適合當 development-only 挑戰者，而不是直接升為 production winner |
| 現有 multilingual MiniLM | [官方 model card](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)說明 50 語 sentence/paragraph embedding 與 semantic search | model card 未提供本案所需的中→英非對稱 retrieval benchmark | 保留 operational baseline；它不能替代新候選的 task-specific development comparison |

其中 E5 small/base 的 MIRACL Recall@100 分別為：英文 85.3／86.4、中文 87.9／92.1；這支持 base 的容量增益，但不是 cross-lingual 指標。BGE-M3 與 mGTE 的 MKQA 才是「非英文 query 找英文 passage」的直接 evidence。分數來自不同論文設定，不應橫向當作同一 leaderboard。

## Query／passage 合約是不可互換的模型介面

### multilingual E5

官方 model card 明確要求每筆輸入都加 prefix，即使內容不是英文：

```text
query: 使用者目前的查詢
passage: durable memory record 的可檢索文字
```

錯漏 prefix 不是小型 prompt 差異，而是違反訓練合約。Bakeoff 必須在 tokenization 前加上 prefix；不能只對 query 加、也不能把 `passage:` 存進 canonical record。索引 manifest 應記錄 model id、完整 revision、ONNX SHA-256、dimension、pooling、normalization、query prefix、passage prefix 與 max length，任一欄改變就要求完整 reindex。

### Jina v2 base zh

官方 card 與 FastEmbed metadata 均說 prefix 不必要。query 與 passage 保持原文，使用 mean pooling 與 L2 normalization。不要為了和 E5 共用管線而加 `query:`／`passage:`；那會製造模型未宣告的輸入分布。8192 tokens 是能力上限而非本案預設值，development bakeoff 應使用相同、受控的 record text budget，避免「多吃文字」成為隱藏變因。

### BGE-M3 與 multilingual GTE

BGE-M3 dense reference usage不要求 instruction；multilingual GTE reference usage則取 CLS token而不是mean pooling。FastEmbed custom registration若 pooling 設錯，即使模型能成功載入也會產生語意不同的向量。因此「有一個 ONNX」不等於「可正確替換現有 encoder」。

## CPU、RAM 與 index 風險

### 靜態向量成本

現有 SQLite 儲存 float32 vectors，未含 SQLite page、row metadata、id mapping 或 ANN 結構時：

| Dimension | 每 record raw bytes | 100k records raw size | 40 MiB 純向量理論上限 |
|---:|---:|---:|---:|
| 128 | 512 B | 48.8 MiB | 81,920 |
| 256 | 1,024 B | 97.7 MiB | 40,960 |
| 384 | 1,536 B | 146.5 MiB | 27,306 |
| 768 | 3,072 B | 293.0 MiB | 13,653 |
| 1024 | 4,096 B | 390.6 MiB | 10,240 |

因此 E5-small 維持現有 384-d 風險面；E5-base 與 Jina v2 把 index 約加倍；BGE-M3 約為現況 2.67 倍。GTE 的 Matryoshka 128/256/384-d 很有價值，但論文只證明 elastic representation 的整體設計；本案仍要重新量測指定截斷維度，不能假設 128-d 無品質損失。40 MiB 欄只是向量算術上限，實際 SQLite 上限一定更低。

### Model working set 與 ISA

- E5-small 的官方最小 qint8 ONNX 檔名標示 AVX512/VNNI，而 i5-8250U 只有 AVX2；不能只從檔名推論支援或不支援。後續以固定 SHA 的 artifact 實跑成功，單筆向量相似度與 FastEmbed 相符，development query peak RSS 為 484,968 KiB。這只確認本機 ORT 路徑，不外推成通用 ISA 相容承諾。
- Jina v2 的 FastEmbed built-in 預設是約 641 MB FP32；官方 quantized 約 162 MB，但切換檔案必須被視為另一個 immutable model artifact。它可能比 FP32 更適合 RAM，卻不能只憑檔案大小推論 peak RSS 或 ranking parity。
- E5-base、BGE-M3 與 GTE 的模型檔及 768/1024 維 index 都顯著提高 7.7 GiB 機器上的 concurrent pressure。BGE-M3／GTE 的 8192-token 能力若真的啟用，activation 與 attention 成本也會增加；本案短記憶不應用最大 context 作預設。
- 現有 384 MiB available-memory guard 是啟動前保護，不是「模型 peak RSS 必須低於 384 MiB」的等價敘述。每個 artifact 都要實測 cold load、reindex peak RSS 與 warm query peak RSS。

## FastEmbed／local ONNX 可用性

FastEmbed 的[官方文件](https://qdrant.github.io/fastembed/)以 ONNX Runtime、CPU-first 為核心；[官方 repository](https://github.com/qdrant/fastembed)公開 `TextEmbedding.list_supported_models()` 與 `TextEmbedding.add_custom_model()`。對目前固定 0.8.0 的唯讀 enumeration：

- 內建：Jina v2 base zh、現有 multilingual MiniLM、multilingual E5 **large**；
- 未內建：multilingual E5 small/base、BGE-M3、Alibaba multilingual GTE。

「未內建」不必然等於不能用，但 custom registration 只解決 metadata／artifact routing，不能自動證明 tokenizer、pooling、normalization、external-data ONNX、prefix 與量化結果正確。E5-small 是最合理的 custom integration，因為 Qdrant 官方 README 本身用它示範；GTE 則同時有 remote custom architecture、CLS pooling 與官方主分支無 ONNX三個變因，不適合在同一輪 bakeoff 中混進來。

所有入選 artifact 必須預下載到本機 model cache，固定 revision 與 SHA-256，然後以 offline/local-only 模式驗證；不應讓 benchmark 執行時自動跟隨 Hub `main` 或下載新檔。

## Development-only bakeoff 設計

### 固定三個 arm

1. operational baseline：現有 MiniLM revision、AVX2 quantized ONNX、384-d；
2. candidate A：E5-small revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`；優先以官方 FP32 ONNX建立 correctness reference，再評估 AVX2 int8 artifact；
3. candidate B：Jina v2 base zh revision `c1ff9086a89a1123d7b5eff58055a665db4fb4b9`；FP32 built-in 是 correctness reference，官方 quantized ONNX是資源挑戰者。

不要把 E5-small FP32 與 AVX2 int8 算成兩種語意模型；它們應先通過同一模型的 quantization parity gate，再選一個 artifact 參賽。同理 Jina FP32／quantized 也不可在看到 retrieval 結果後任意挑最好者。

### 只使用 development cases

development set 至少分桶報告：

- 中文 query → 中文記憶；
- 英文 query → 英文記憶；
- 中文 query → 英文記憶；
- 英文 query → 中文記憶；
- paraphrase；
- unrelated／generic-word collision／應 abstain；
- stale memory 與 freshly verified state 衝突。

尤其要保留「Evidence filename 或 generic `hook` 字樣不應壓過真正 topic/value relevance」這類 false-positive case。不能只提高 hit rate，卻用更多無關 context pollution 換來。

每個 arm 使用完全相同 record rendering、corpus、development queries、top-k、hybrid lexical 權重與後處理；每次完整重建獨立 index。先凍結 primary metric 與 tie-break，再看結果：

- retrieval：Hit@1／Hit@3、MRR 或 nDCG、target rank；
- safety：unrelated intrusion rate、abstention precision、stale-vs-live 錯誤；
- cross-lingual：zh→en、en→zh 分開，不只報總平均；
- systems：model bytes、index bytes、cold load、reindex wall time、query p50/p95、reindex/query peak RSS、timeout/failure count；
- quantization：FP32 對 quantized 的 embedding cosine、top-k overlap 與 task metric delta。

硬性 operational gates 至少沿用目前 3 秒 query timeout、available-memory preflight 與 40 MiB index cap；若候選要求改 gate，應先判定它不符合目前部署目標，而不是為了讓模型過關而事後放寬。不要讓 8192-token capability、不同 text rendering 或不同 hybrid 權重污染 encoder comparison。

### 選擇規則

先要求 safety／abstention 不退步，再比較 cross-language 與 paraphrase retrieval，最後才以 latency、RSS、index bytes 決勝。若 E5-small 在 development 上達到品質門檻且資源可接受，優先選它；Jina 必須以明顯的中英 retrieval 或資源優勢補償 768-d index。若兩者都沒有可靠改善，就保留現有 MiniLM，不因做過研究而強迫換模。

完成 development bakeoff 後，才可固定 winner 的 model id、完整 revision、artifact SHA-256、pooling、normalization、prefix contract、dimension、max length、threshold 與 hybrid weights。**本研究不授權讀取 sealed held-out，也不建議用 held-out 反覆選模型。**

## Development bakeoff 結果補記

2026-08-20 依上述規則完成最小本機 smoke。使用 frozen
`agent-memory-retrieval-v2-sealed-20260820` development split；suite SHA-256 是
`bfaa35f36f1fd895e8d5356b5df25b77afbeb80a43431f92e6b7bf61fbfc0d09`。每個候選只建立
160-record temporary store、查 80 個 development cases；沒有 GPT／remote inference call。

| Profile | English paraphrase Hit@3 | Cross-language Hit@3 | No-answer FPR | Scope intrusion | 結論 |
|---|---:|---:|---:|---:|---|
| E5-small | 35% | 85% | 10% | 0% | English gate 失敗 |
| Jina v2 base zh | 45% | 60% | 0% | 0% | 兩個 recall gate 失敗 |

兩者都沒有通過預先固定的 85%／85% recall gates，因此沒有 winner、沒有讀取 v2 sealed
held-out，也沒有更換 production default。這個結果支持原先的停止規則：研究選模只能縮小
候選，不能取代 task-specific development evidence。

同日另以相同 E5 artifact 做一次、也只做一次 structured-passage follow-up：排除 record ID、
scope、tags、Evidence 與 Decision Links，保留 labeled content、Observed Outcome 與
Applicability。English／cross-language Hit@3 仍是 35%／85%，所以否證了 identity noise 是
主要 recall 成因；no-answer FPR 由 10% 降至 0%，成本略降。這進一步顯示：primary-source
model selection 與乾淨 input contract 都是必要工程條件，但仍不能取代 task-specific
candidate discrimination。Held-out 繼續未讀。

## Primary sources

- Wang et al., [Multilingual E5 Text Embeddings: A Technical Report](https://arxiv.org/html/2402.05672)；[E5-small official model card/files](https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3)、[E5-base official model card/files](https://huggingface.co/intfloat/multilingual-e5-base/tree/d128750597153bb5987e10b1c3493a34e5a4502a)。
- Chen et al., [BGE M3-Embedding: Multi-Lingual, Multi-Functionality, Multi-Granularity Text Embeddings Through Self-Knowledge Distillation](https://arxiv.org/html/2402.03216)；[official BGE-M3 model repository](https://huggingface.co/BAAI/bge-m3/tree/5617a9f61b028005a4858fdac845db406aefb181)。
- Zhang et al., [mGTE: Generalized Long-Context Text Representation and Reranking Models for Multilingual Text Retrieval](https://arxiv.org/html/2407.19669)；[official multilingual GTE repository](https://huggingface.co/Alibaba-NLP/gte-multilingual-base/tree/9bbca17d9273fd0d03d5725c7a4b0f6b45142062)。
- Günther et al., [Jina Embeddings 2: 8192-Token General-Purpose Text Embeddings for Long Documents](https://arxiv.org/html/2402.17016)；[official Jina v2 base zh repository](https://huggingface.co/jinaai/jina-embeddings-v2-base-zh/tree/c1ff9086a89a1123d7b5eff58055a665db4fb4b9)。
- Qdrant, [FastEmbed official documentation](https://qdrant.github.io/fastembed/) and [official repository/custom-model API](https://github.com/qdrant/fastembed)。
