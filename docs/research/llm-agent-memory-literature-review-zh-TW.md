# LLM／Agent Memory 文獻回顧

## 從「存得下」走向「能更新、會懷疑、可驗證、真的改善行動」

**研究快照：2026-08-13**  
**範圍：外部 durable memory、agent experience、working-memory compaction、retrieval、evaluation 與 safety**  
**不在本次範圍：修改 `agent-memory` runtime、hook、schema 或 retrieval 實作**

---

## 摘要

這份報告的核心結論是：**目前沒有一個公認、在所有情境都最好的 LLM memory 架構。**
研究已經證明許多局部方法有效，但它們處理的是不同問題：有些讓長文件能被摘要與回看，
有些保存多輪對話中的人物事實，有些把失敗轉成下一次重試的反思，有些保存可重用的
workflow 或程式技能；這些結果不能互相冒充。

跨越這些文獻後，最穩健的共同方向不是「把所有歷史塞回 prompt」，也不是「讓模型永遠
相信自己寫過的經驗」，而是：

1. **有限工作上下文＋外部長期儲存**：當下只攜帶 bounded working state，其餘資料按需取回。
2. **記憶分型**：事實、事件、決策、主觀反思、程序／workflow、當前任務狀態不能混成一池。
3. **摘要只負責導航**：重要結論必須能回指原始 observation、trajectory、文件或目前 repo。
4. **更新不能等於無痕覆寫**：時間、來源、scope、版本、反證與 supersession history 都有價值。
5. **檢索與使用是兩個問題**：Hit@k 很高，只代表候選被找到；不代表模型真的理解、採納或
   因此做出更好的決策。
6. **記憶是 untrusted evidence，不是 executable instruction**：舊經驗可能過期、偏誤、被污染，
   甚至只因反覆成功便強化不安全的「一定要執行」傾向。
7. **評估必須到 downstream state 與 action**：除了 recall，還要測更新、時間推理、拒答、
   無關記憶侵入、過期決策採用、工具參數、成本，以及 correct／no／corrupted memory 的因果介入。

以這些研究來看，現有 `agent-memory` 的方向不是閉門造車。它和 CoALA 的記憶分型、MemGPT
的分層、ReadAgent 的 gist＋回指原文、ExpeL 的成功／失敗經驗、LongMemEval 的更新與拒答、
RealMem 的長期專案狀態有清楚交集。它目前較有特色的地方，是把 decision 當 working
evidence、保留 supersession 與 counterevidence，並要求用 live repo／runtime 驗證當前真相。

但文獻也確認了目前最重要的缺口：**同義與跨語言 candidate generation、記憶對模型決策的
因果效用、程序性記憶、選擇性遺忘，以及 compact 後的真實專案恢復 benchmark。** 其中本機
semantic-gap probes 的 Hit@3 = 0/4，根因不是 ranking 分數不夠漂亮，而是 lexical index 在
沒有共享字詞時根本沒有產生正確候選。這是架構邊界，不是調高權重就能解決的小問題。

---

# 1. 研究問題與方法

## 1.1 本報告要回答什麼

本次調查聚焦六個問題：

1. 學界如何定義 agent memory，而不是把所有 long context 都叫 memory？
2. 現有方法以什麼為記憶單位，又如何寫入、組織、檢索、使用與更新？
3. 哪些作法已有 peer-reviewed 實驗支持，哪些仍只是 preprint 或 system demo？
4. benchmark 實際測到什麼，又漏掉什麼？
5. 過去經驗如何幫助 agent，同時又可能使它誤信、僵化或變得不安全？
6. 這些結果如何解讀目前 `agent-memory` 的優點、缺口與下一輪研究優先序？

## 1.2 資料來源與證據分級

本報告優先使用官方 conference／journal 頁、ACL Anthology、PMLR、NeurIPS Proceedings、
OpenReview、AAAI Proceedings 與作者 arXiv。廠商頁面只在說明其系統時輔助使用，不把行銷數字
當成獨立重現。調查不是完整的 systematic review，也沒有做 meta-analysis；它是以目前系統的
研究問題為導向的 structured literature review。

為避免把不同證據混在一起，文中採下列標記：

| 標記 | 意義 | 可以支持什麼 |
|---|---|---|
| **期刊／主會議** | 已出現在 TMLR、UIST、AAAI、ACL、ICML、NeurIPS 等正式論文集 | 支持該論文設定中的方法與比較，仍不等於 production 普遍有效 |
| **Findings／Demo** | 經同儕審查，但定位可能偏探索、分析或系統展示 | 支持重要觀察；效果數字要看 benchmark、backbone 與成本 |
| **Preprint** | arXiv 或尚未正式出版的工作 | 可當新方向與待驗證假說，不宜作成熟結論 |
| **本機觀察** | 使用者回饋、rollout、repo 與本機 benchmark | 可證明這台機器與這次案例；不能替代跨模型、跨任務研究 |

## 1.3 這份報告刻意不做的事

- 不因某篇論文分數高，就宣布它是「世界最佳 memory」。
- 不把人類記憶的神經科學類比當作工程正確性證明。
- 不把單一 LLM-as-a-judge 分數當作 ground truth。
- 不把 retrieval recall、回答正確率、行動正確率與長期安全混成一個「有效」指標。
- 不在研究尚未比較前，直接把目前 lexical index 換成 embedding、graph 或 RL controller。

---

# 2. 先釐清：Long context、RAG 與 durable memory 不相同

LLM 本身通常是 stateless inference：這一回合之外的資訊，必須再次放進 context、寫入外部
store，或透過參數更新才能影響未來。三種常被混用的解法，其責任不同：

| 機制 | 主要問題 | 常見失敗 |
|---|---|---|
| Long context | 讓模型一次讀更多 token | 成本高、lost-in-the-middle、舊狀態與新狀態混在一起 |
| RAG | 依 query 從外部語料找相關片段 | query 沒有共享詞、召回錯片段、沒有更新與身份語意 |
| Durable agent memory | 跨 session 保存使用者、任務、事件、決策、經驗與程序 | 寫錯、過期、誤合併、污染、過度依賴、忘記使用 |

Durable memory 因此不是一個 vector database 的別名。它至少包含一個生命週期：

> **Observe → Decide what to write → Represent → Consolidate／link → Retrieve → Read／reason →
> Act → Observe outcome → Correct／supersede／forget → Evaluate**

任何只優化其中一步的論文，都不能單獨解決整條鏈。尤其是「寫到了」不等於「搜得到」，
「搜到了」不等於「模型真的使用」，「使用了」也不等於「行動更好或更安全」。

---

# 3. 一個較穩定的記憶分型

[CoALA](https://openreview.net/forum?id=1i6ZCvflQJ)（TMLR 2024）提供了目前很有用的共同語彙：
working、episodic、semantic 與 procedural memory。它是認知架構框架，不是 production
memory 的效果證明；價值在於把問題拆開。

| 類型 | 回答的問題 | 適合的記憶單位 | 不應混入 |
|---|---|---|---|
| Working／task state | 我現在做到哪、下一步是什麼？ | objective、current state、next action、validation、rollback | 整個歷史 archive |
| Episodic | 某次具體發生了什麼？ | situation、action、observation、outcome、時間、來源 | 無來源的普遍規則 |
| Semantic | 目前相信哪些事實、概念與關係？ | atomic fact、entity relation、版本、有效期 | 暫時任務游標 |
| Decision／belief | 為何當時選這個？何時重看？ | rationale、constraints、alternatives、consequences、counterevidence | 永久命令或未校準 confidence |
| Procedural | 類似工作怎麼做？ | workflow、skill、program、preconditions、verification | 使用者當前偏好與專案現況 |

這個分型對現有系統有一個直接含義：`resume capsule` 不只是短版 semantic memory；它是一個
有 liveness 責任的 working-state 物件。`experience` 也不只是 fact；它包含一次行動與後果。
同樣地，workflow 不能因為曾經成功，就被當成「目前專案一定仍使用這個架構」。

---

# 4. 主要方法家族

## 4.1 分層 working context：有限前景、外部長期儲存

[MemGPT](https://arxiv.org/abs/2310.08560) 把有限 context 與外部儲存類比為 virtual memory，
讓模型透過操作在不同記憶層間搬移資料。它在長文件與多 session 對話中展示可行性，奠定了
「不要把所有歷史永遠塞在 main context」的設計方向。限制是早期實驗沒有完整處理 provenance、
錯誤更新、跨專案隔離與長期安全，也沒有證明模型應擁有無限制的 memory mutation 權限。

[HiAgent](https://aclanthology.org/2025.acl-long.1575/)（ACL 2025）更聚焦單次長任務的 working
memory：先形成 subgoal，完成後把舊 action-observation 壓成摘要，只保留目前 subgoal 的細節。
五種 long-horizon tasks 中，作者報告整體成功率約為 baseline 兩倍、平均少 3.8 steps。這支持
「按任務結構 compact」，但不代表所有任務都應固定週期壓縮。

這一族方法支持三個工程判斷：

- context 應是 bounded working set，而不是 archive dump；
- compact 邊界最好跟 task／subgoal 邊界有關，而不只看 token 百分比；
- working summary 必須承認自己是有損表示，不能冒充 canonical truth。

## 4.2 摘要、gist 與可回指的原始證據

[ReadAgent](https://proceedings.mlr.press/v235/lee24c.html)（ICML 2024）把長文分成 episodes、
生成 gist memories，模型需要精確細節時再 lookup 原始 passage。在 QuALITY、NarrativeQA 與
QMSum 上，作者報告 3.5–20 倍 effective context。它最重要的設計不是「摘要很強」，而是
**摘要沒有消滅原文**。

這個結論對 durable project memory 特別重要：摘要適合告訴 agent「應該去哪裡查」，不適合在
幾個月後單獨證明「repo 現在就是這樣」。好的 capsule 應保存 objective、狀態與 back-reference；
真正回答架構、依賴與 runtime 問題時，仍應打開 canonical record 或 live repo。

2026 年的新 preprint [CompactionRL](https://arxiv.org/abs/2607.05378) 進一步把 summary
generation 和 long-horizon task execution 一起做 RL，在 SWE-bench Verified 與 Terminal-Bench
2.0 報告改善。它直接關聯 coding-agent compact，但截至本報告日期仍是極新的 preprint；不能用
一篇結果就推論 learned compaction 已解決跨 session、跨專案、版本演化與隱私問題。

## 4.3 Episodic memory、reflection 與經驗學習

[Generative Agents](https://doi.org/10.1145/3586183.3606763)（UIST 2023）保存自然語言 memory
stream，以 recency、LLM 估計的 importance 與 embedding relevance 組合檢索，並從多筆記憶
產生 higher-level reflection。這證明 observation、reflection、planning 可以形成可運作架構，
但其主要評估是小型社會模擬中的 believability，不是長期事實正確、provenance 或安全更新。
LLM 自評 1–10 importance 與固定 threshold 也只是 heuristic，不能直接當校準後機率。

[Reflexion](https://papers.nips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html)
（NeurIPS 2023）讓 actor 在任務回饋後產生 verbal reflection，放進 episodic buffer，供下一次
trial 使用。它在 coding、reasoning 與 sequential decision tasks 有顯著改善，包括論文報告的
HumanEval 91% pass@1。可是這是相近任務的 repeated trial，不是多年、多專案 durable memory；
一次自我反思仍可能錯。

[ExpeL](https://ojs.aaai.org/index.php/AAAI/article/view/29936)（AAAI 2024 Oral）同時收集成功與
失敗 trajectories，從對照中抽取 insights，並在新任務回想過往經驗。它最接近「決策需要後果
與反例」的方向：只保存成功步驟，容易把偶然相關誤認為規則。另一方面，ADD／EDIT／UPVOTE／
DOWNVOTE 很適合管理 insight，但票數仍不是統計 confidence；重複相似成功也可能製造假信心。

因此較可靠的 experience record 應包含：

- 當時 situation 與 preconditions；
- 原本 expectation；
- 實際 action；
- observed outcome 與 evidence；
- applicability／不適用邊界；
- 它支持、反駁、縮小或無法判定哪一個 decision。

## 4.4 Semantic／atomic／graph memory

[A-MEM](https://proceedings.neurips.cc/paper_files/paper/2025/hash/19909c36f51abc4856b4560aff3d36d6-Abstract-Conference.html)
（NeurIPS 2025）借用 Zettelkasten：每筆新記憶形成帶 context、keywords、tags 的 atomic note，
再建立動態連結；新資訊也可能更新鄰近 note 的描述。優點是能從平坦片段長成關聯網路，缺點是
若模型直接 in-place 改寫舊 note，provenance 與 correction history 容易消失。對稽核型系統，
比較安全的作法是保留原版本，以 supersession 或 derived enrichment 表示演化。

[Mem0](https://arxiv.org/abs/2504.19413)（2025 preprint）從近期對話抽取 candidate facts，
先找相似舊記憶，再讓 LLM 選擇 ADD／UPDATE／DELETE／NOOP；graph variant 另保存 entities 與
relations，遇到關係衝突時可把舊 relation 標為 invalid，而不是直接消失。這個 operation taxonomy
很實用，但 paper 主要在只有十組長對話的 LoCoMo 上以 LLM judge 評估，且排除 adversarial／
unanswerable 類別；base 與 graph 版對歷史的處理也不同。它是 production-oriented 的重要候選，
截至本報告日期仍不應把自身 benchmark 數字當成獨立重現或普遍最優證明。

[HippoRAG](https://proceedings.neurips.cc/paper_files/paper/2024/hash/6ddc001d07ca4f319af96a3024f6dbd1-Abstract-Conference.html)
（NeurIPS 2024）以 entity／relation graph 加 Personalized PageRank 支援 multi-hop retrieval。
它顯示 graph 對跨片段關係推理有潛力，但靜態 QA graph 不自然等同於持續變動的 project state；
若沒有時間、版本、來源與 supersession，圖只會把過期關係連得更漂亮。

更重要的是，[Does Memory Need Graphs?](https://aclanthology.org/2026.acl-long.1232/)（ACL 2026）
以統一框架控制比較 flat 與 graph dialog memory，發現許多差異來自 representation、key/value、
extraction model 與 retrieval 實作，而不是「用了 graph」本身。其成本分析也顯示 graph 建構與
大規模 retrieval 通常更昂貴。這對工程決策的訊息很明確：

> Graph 是需要以 multi-hop、temporal、update 與 downstream action 實驗證明的資料結構，
> 不是只要加上去就會自動變成更聰明的 memory。

2026 年 ACL 的 [GAM](https://aclanthology.org/2026.acl-long.1600/) 則提出 event progression graph
與 topic associative network 的階層分離，在語意轉折後再 consolidation。這支持「快速事件流」
與「較穩定長期知識」分層；但同樣需要看 ingestion cost、錯誤 relation 與版本衝突。

## 4.5 Procedural／workflow memory

[Voyager](https://openreview.net/forum?id=P8E4Br72j3)（TMLR 2024）把 Minecraft 中的 procedural memory 寫成可執行
code skills，搭配環境錯誤與 self-verification 反覆改進。它在有明確環境回饋的單一世界中展現
強力技能累積，提醒我們「怎麼做」應有獨立的 skill library；但成功程式跨環境重用仍需檢查
preconditions、權限與副作用。

[Agent Workflow Memory](https://proceedings.mlr.press/v267/wang25bx.html)（ICML 2025）從 web
agent trajectories 歸納重複 workflow，離線或線上累積後選擇性注入。在 Mind2Web、WebArena
等任務中有改善，也能減少成功任務的步數。它支持一個對現有系統很重要的缺口判斷：
decision／experience ledger 並不自動等於 procedural memory。若未來加入 workflow，至少應保存：

- 適用的任務與環境版本；
- 前置條件、所需工具與權限；
- 可觀測的成功條件；
- 已知 failure modes 與 rollback；
- 來源 trajectory 與最後驗證時間。

## 4.6 Learned memory controller：有潛力，但不應太早取代硬邊界

[Agentic Memory／AgeMem](https://aclanthology.org/2026.acl-long.981/)（ACL 2026）讓 agent 以工具
共同管理 LTM 的 add／update／delete 與 STM 的 retrieve／summarize／filter／discard，再用
多階段 RL 學習何時操作。[Memory-R1](https://aclanthology.org/2026.acl-long.583/)（ACL 2026）
也以 outcome-driven RL 訓練 Memory Manager 與 Answer Agent，操作 ADD／UPDATE／DELETE／NOOP。

這一族方法的重要進步，是 memory policy 不再全靠人工固定 heuristic，而是以 downstream reward
學習。然而它也帶來三個問題：

1. reward 可能只偏好 benchmark answer，而忽略 privacy、provenance、liveness 與 audit；
2. DELETE／UPDATE 的錯誤代價比一次 retrieval miss 更難恢復；
3. 訓練分布中的好 policy，不保證在未知專案、工具權限或高風險操作中安全。

因此「何時檢索、如何摘要」可以逐步學習；「不能存 secrets、不能讓 hook 阻塞工作、不能無痕
刪除 correction history、目前使用者指示優先」仍應維持 deterministic hard constraints。

---

# 5. 論文比較：它們究竟證明了什麼

## 5.1 架構與對話記憶

| 工作 | 狀態 | 核心單位／操作 | 主要證據 | 外推限制 |
|---|---|---|---|---|
| [CoALA](https://openreview.net/forum?id=1i6ZCvflQJ) | TMLR 2024 | working／episodic／semantic／procedural taxonomy | 統整認知架構與 language agents | 是框架，不是 production 效果實驗 |
| [Generative Agents](https://doi.org/10.1145/3586183.3606763) | UIST 2023 | memory stream、reflection、planning | 25-agent 模擬與 believability | 不直接測事實更新、provenance、安全 |
| [MemGPT](https://arxiv.org/abs/2310.08560) | Preprint | virtual context、tiered memory operations | 長文件、多 session chat | 早期、缺完整治理與版本模型 |
| [MemoryBank](https://ojs.aaai.org/index.php/AAAI/article/view/29946) | AAAI 2024 | event memory、user portrait、decay／reinforcement | companion dialogue | 人類遺忘類比不證明安全 forgetting |
| [A-MEM](https://proceedings.neurips.cc/paper_files/paper/2025/hash/19909c36f51abc4856b4560aff3d36d6-Abstract-Conference.html) | NeurIPS 2025 | atomic notes、links、memory evolution | 多模型與長對話 benchmark | link／update 品質依賴抽取模型 |
| [Mem0](https://arxiv.org/abs/2504.19413) | 2025 Preprint | candidate facts；ADD／UPDATE／DELETE／NOOP；graph variant | LoCoMo accuracy／latency／token | 小型 benchmark、主要依賴 LLM judge，base／graph 歷史語意不同 |
| [Hindsight](https://aclanthology.org/2026.acl-demo.27/) | ACL 2026 Demo | world／experience／observation／opinion networks；hybrid recall | LoCoMo、LongMemEval 與可操作 demo | demo paper；比較受 backbone 與系統設定影響 |

## 5.2 經驗與程序

| 工作 | 狀態 | 保存內容 | 已見改善 | 最重要限制 |
|---|---|---|---|---|
| [Reflexion](https://papers.nips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html) | NeurIPS 2023 | feedback 後的 verbal reflection | retry coding／reasoning／decision tasks | 相近 trial，不是通用 durable memory |
| [ExpeL](https://ojs.aaai.org/index.php/AAAI/article/view/29936) | AAAI 2024 | 成功／失敗 trajectories 與抽象 insights | ALFWorld、WebShop、HotpotQA transfer | insight vote 不等於校準 confidence |
| [Voyager](https://openreview.net/forum?id=P8E4Br72j3) | TMLR 2024 | executable code skills | Minecraft lifelong exploration | 單一環境、驗證信號明確，外部效度有限 |
| [Agent Workflow Memory](https://proceedings.mlr.press/v267/wang25bx.html) | ICML 2025 | induced workflows | web navigation success／steps | 抽象錯誤可能把錯流程固化 |
| [AgeMem](https://aclanthology.org/2026.acl-long.981/) | ACL 2026 | learned LTM＋STM tool policy | 長程 agent／QA tasks | 固定工具與訓練分布，不替代治理規則 |
| [Memory-R1](https://aclanthology.org/2026.acl-long.583/) | ACL 2026 | learned ADD／UPDATE／DELETE／NOOP | LoCoMo、MSC、LongMemEval | outcome reward 可能漏掉安全與稽核品質 |

## 5.3 Compact 與 retrieval

| 工作 | 狀態 | 方法 | 對目前系統的直接訊息 |
|---|---|---|---|
| [ReadAgent](https://proceedings.mlr.press/v235/lee24c.html) | ICML 2024 | episodic gist＋lookup 原文 | 摘要負責導航，原始證據仍須存在 |
| [HiAgent](https://aclanthology.org/2025.acl-long.1575/) | ACL 2025 | 以 subgoal chunk／summarize working memory | compact 應接近任務結構，不只是 token 閾值 |
| [HippoRAG](https://proceedings.neurips.cc/paper_files/paper/2024/hash/6ddc001d07ca4f319af96a3024f6dbd1-Abstract-Conference.html) | NeurIPS 2024 | graph＋Personalized PageRank | graph 可補 multi-hop，不自動處理時間與版本 |
| [Does Memory Need Graphs?](https://aclanthology.org/2026.acl-long.1232/) | ACL 2026 | 受控比較 flat／graph stages | 先建立強 flat baseline，再用 ablation 證明 graph 價值 |
| [CompactionRL](https://arxiv.org/abs/2607.05378) | 2026 Preprint | 聯合學習 execution／summary | 對 coding compact 很相關，但仍屬新證據 |

---

# 6. Benchmark：不能再只看「有沒有答對一個問題」

## 6.1 主要 benchmark 覆蓋面

| Benchmark | 狀態 | 測什麼 | 沒充分測什麼 |
|---|---|---|---|
| [LoCoMo](https://aclanthology.org/2024.acl-long.747/) | ACL 2024 | 長對話 QA、事件摘要、multimodal generation；平均約 600 turns、最多 32 sessions | 真實專案依賴、程序、工具 action |
| [LongMemEval](https://openreview.net/pdf?id=pZiyCaVuti) | ICLR 2025 | extraction、multi-session、temporal、knowledge update、abstention | 真實多人專案與 tool execution |
| [MemBench](https://aclanthology.org/2025.findings-acl.989/) | Findings ACL 2025 | factual／reflective memory；participation／observation；effectiveness／efficiency／capacity | 長期真實 project state |
| [MemoryAgentBench](https://arxiv.org/abs/2507.05257) | 2025 Preprint | accurate retrieval、test-time learning、long-range understanding、selective forgetting | production privacy、完整專案行動 |
| [RealMem](https://aclanthology.org/2026.findings-acl.703/) | Findings ACL 2026 | 11 種 project scenarios；static retrieval、dynamic update、temporal、proactive alignment | 真實使用者資料與 tool-use；資料主要為合成 |
| [LoCoMo-Plus](https://aclanthology.org/2026.acl-long.1150/) | ACL 2026 | cue–trigger semantic disconnect、隱性 constraint consistency | 跨語言 durable project memory |
| [Mem2ActBench](https://aclanthology.org/2026.acl-long.370/) | ACL 2026 | 從分散長期記憶重建 tool 與 arguments；400 tasks／2,029 sessions | 開放式 coding repo 修改與長期安全 |

## 6.2 RealMem 為何特別重要

RealMem 不只模擬「昨天喜歡什麼電影」，而是跨 session 追蹤會演化的目標與 project state。
官方論文包含超過 2,000 組跨 session dialogues、十一種場景；其中 Code Architecture 類別對現有
memory systems 特別困難，論文分析顯示 QA、recall 與 helpfulness 都顯著低於 0.4。這正是
`project-c` compact 後恢復模組、依賴、決策與下一步所屬的問題類型。

不過 RealMem 仍主要由 multi-agent pipeline 合成，而且 paper checklist 明確指出尚未涵蓋
tool-use。它很適合當 project-memory baseline，不足以單獨證明 coding agent 會正確修改 repo。

## 6.3 LoCoMo-Plus 為何能解釋 semantic gap

LoCoMo-Plus 特別測 **cue 與 trigger 之間沒有表面語意對齊**：使用者先前講過的狀態、目標或
價值，後來不會直接用同樣字詞再問。論文顯示傳統 string matching 與明示 task-type prompt
會漏掉這種 cognitive memory。

這與本機四個 semantic-gap probes 同源：只要 query 不再複製 record 的關鍵詞，lexical
retrieval 便可能完全看不到正確候選。跨語言又把 vocabulary overlap 降得更低。鄰近研究
[XRAG](https://arxiv.org/abs/2505.10089) 也指出，cross-lingual RAG 不只難在找資料，模型還要
跨語言理解 retrieved evidence；因此加入 multilingual embedding 也只是解決第一半。

---

# 7. 為什麼「Context Pack 出現了」仍不能證明記憶有效

2026 年 ICML 收錄的 [Large Language Model Agents Are Not Always Faithful Self-Evolvers](https://arxiv.org/abs/2601.22436)
用 controlled causal interventions 改動 raw 與 condensed experience。研究發現 agent 通常會受
raw experience 影響，卻可能忽略或誤解 condensed insight；原因包含摘要的 semantic loss、
模型處理偏差，以及 pretrained prior 本來就足以完成任務。

所以至少要分開四層：

1. **Storage correctness**：資料真的被保存且版本正確。
2. **Retrieval correctness**：目標記憶進入 top-k，無關資料沒有大量侵入。
3. **Utilization faithfulness**：改動／移除記憶時，模型行為是否按預期改變。
4. **Outcome improvement**：使用正確記憶後，最終 state／action 是否更準、更安全。

最有說服力的測試不是只做 memory-on／off，而是至少加入：

- **correct memory**：提供正確、適用的 capsule／record；
- **no memory**：完全不提供；
- **corrupted memory**：修改一個關鍵 dependency、constraint 或 next action；
- **stale memory**：提供曾經正確但已被 supersede 的版本；
- **irrelevant memory**：提供語意相似、project 不同的 decoy；
- **full history control**：在成本允許時比較完整 transcript／repo。

若 correct memory 改善答案，而 corrupted／stale memory 以可預期方向傷害決策，才較能證明
模型真的使用它；若有無記憶都一樣，可能只是模型重新掃 repo 得到相同答案。

---

# 8. 記憶的失敗與安全風險

## 8.1 表面成功也可能傳播錯誤

[How Memory Management Impacts LLM Agents](https://aclanthology.org/2026.acl-long.27/)
（ACL 2026）以 memory addition／deletion 的 controlled experiments 觀察到 experience-following：
當新 task 和 retrieved experience 很像，agent output 也容易高度相似。這會造成兩種失敗：舊錯誤
被反覆傳播，以及表面上成功的 execution 對新情境其實沒有價值、甚至誤導。

這篇研究比「記成功與失敗都好」再多走一步：**記憶品質應由後續任務結果持續重新標記。**
曾經成功只代表某個 situation 中有正面 observation，不代表它已成為普遍規則；當未來結果不符
原 expectation 時，系統應新增 contradict／narrows evidence，或在證據充足時 supersede，而不是
因相似度高便加強 replay。

## 8.2 成功經驗可能強化 action bias

[On Safety Risks in Experience-Driven Self-Evolving Agents](https://aclanthology.org/2026.findings-acl.2091/)
（Findings ACL 2026）顯示，即使 experience 全來自 benign tasks，也可能讓 agent 在高風險任務
更傾向執行而不是拒絕；加入 refusal experience 雖能減少這種傾向，又可能造成 over-refusal。

這正對應使用者擔心的距離感：經驗太弱，記憶沒有用；經驗太像命令，agent 便可能無腦重播。
所以 outcome graph 應保存支持與反證，但不能把 evidence count 轉成未校準的 confidence，
也不能只用「過去成功率」決定是否執行高風險動作。

## 8.3 Memory poisoning 可以在 benign 測試下隱藏

[AgentPoison](https://proceedings.neurips.cc/paper_files/paper/2024/hash/eb113910e9c3f6242541c1652e30dfd6-Abstract-Conference.html)
（NeurIPS 2024）對 generic／RAG agents 的 memory 或 knowledge base 注入少量 poisoned instances，
在 trigger 出現時操控行為，同時讓一般 benign performance 幾乎不變。這表示：

- retrieved text 永遠是 data，不應因位於 memory 就升格成 instruction；
- source、scope、writer、time、hash／version 與 permission 都是安全語意；
- 外部頁面、tool output、別的 agent 筆記與模型自我反思，需要不同 trust level；
- 安全 benchmark 必須包含 poisoned、stale、cross-project 與 prompt-injection cases。

## 8.4 Forgetting 不是單純的時間衰減

[MemoryBank](https://ojs.aaai.org/index.php/AAAI/article/view/29946) 使用類 Ebbinghaus decay 與
reinforcement，說明 recency／frequency 可以幫助縮小 context。但在工程 memory 中，少見卻致命
的 failure、使用者 correction、法律／安全 constraint、未完成任務，可能很久不被召回仍很重要。

較安全的區分是：

- **retrieval decay**：降低平常排名，仍可被 exact query／audit 找到；
- **compression**：保留 provenance 的前提下改變預設表示；
- **archive**：退出正常 retrieval，但仍能稽核；
- **physical deletion**：只因 privacy、policy、明確使用者要求或受控 retention 規則執行。

不要把「最近沒用」直接等同「可以永遠刪除」。

---

# 9. 截至 2026 年較強的實務綜合

下面不是某一篇論文的照抄，而是跨方法與 benchmark 後較穩健的 synthesis。

## 9.1 高信心：現在就值得維持

### A. Bounded working state 與 canonical long-term store 分離

當前 context 只放任務游標、必要 constraints 與少量 evidence；完整歷史在外部 store。這同時受到
MemGPT、ReadAgent、HiAgent 與 long-context benchmark 的支持。

### B. 摘要附 back-reference

任何 capsule、reflection、insight 或 derived fact，都應能回到原 observation、trajectory、
record 或 repo。摘要可以錯；source pointer 讓錯誤可被發現與修正。

### C. Typed memory 與不同更新規則

preference、fact、episode、decision、workflow 與 task state 應有不同 schema、有效期與 trust。
例如 preference 可以由使用者 correction supersede；project fact 要 fresh verify；workflow 要重驗
preconditions；experience 只提供 evidence。

### D. Current instruction 與 fresh state 優先

記憶不能覆蓋目前使用者要求，也不能替代 repo、process、network、法律或 API 的最新狀態。這不只是
產品偏好，也是對 stale memory、poisoning 與 self-evolution bias 的必要防線。

### E. 評估到 downstream behavior

至少分 storage、retrieval、utilization、outcome 四層；除了 Hit@k，再報 temporal／update／
abstention／intrusion／stale adoption／action correctness／token／latency。

## 9.2 中等信心：值得實驗，不應直接全面替換

### F. Hybrid candidate generation 與 late fusion

Lexical search 對 error code、路徑、identifier 與 exact technical term 很強；multilingual semantic
retrieval 對 paraphrase／cross-language 較有機會；graph／temporal traversal 對 multi-hop 有價值。
較合理的研究方向是保留各自 candidates，再用 scope、time、source、active version 與 task intent
重排，而不是只留一種 retriever。

### G. Atomic notes＋derived relations

Atomic memory 讓更新與引用較精確，graph 讓關係可走訪；但 relation 應可重建、可標來源，且不要
讓 derived graph 成為唯一 canonical truth。

### H. Offline consolidation／audit

將重複、矛盾、缺 outcome 與 stale facts 放到非阻塞的離線流程，比在 Stop／PreCompact 強迫模型
反思更安全。合併與 supersede 仍須保留 audit trail。

## 9.3 仍屬研究性：先有 benchmark 再採用

### I. Learned write／delete／compact controller

AgeMem、Memory-R1、CompactionRL 顯示 downstream learning 有潛力，但 reward、OOD、安全與可逆性尚未
成熟到可以取代所有 deterministic guardrails。

### J. 自動 selective forgetting

MemoryAgentBench 已把 selective forgetting 列為核心能力，但真實系統還需先定義：是 ranking decay、
archive、summarize、unlearn，還是 physical deletion？不同操作不能共用一個「忘掉」按鈕。

---

# 10. 對目前 `agent-memory` 的研究對照

## 10.1 已經走在正確方向的部分

| 目前設計 | 文獻對照 | 判斷 |
|---|---|---|
| Session capsule 與 knowledge retrieval 分離 | MemGPT、HiAgent、ReadAgent | 強：working state 不再被 archive 淹沒 |
| Bounded、adaptive Context Pack | long-context／working-memory literature | 強：避免固定硬截斷與無限制注入 |
| Canonical human-readable ledger＋可重建 SQLite index | source-backed retrieval、ReadAgent back-reference | 強：derived index 壞掉不等於 evidence 消失 |
| Decision 是 evidence，不是 command | ExpeL、self-evolution safety 研究 | 強：直接回應過度迷信一次成功的風險 |
| Experience 保存 expectation／outcome／applicability | Reflexion、ExpeL | 強：不只記「當初為何這樣選」 |
| Contradict／narrows 優先展開 | knowledge update、counterevidence | 有價值且較少見；仍需 end-to-end A/B 證明模型真的使用 |
| Supersession 保留舊版 | temporal／update benchmarks | 強：比無痕 UPDATE／DELETE 更適合稽核 |
| Silent fail-open hook | liveness 與 prompt contamination 邊界 | 工程上合理；代價是可能漏記 outcome，應離線量測 |
| Memory＋live repo verification | RealMem 的 evolving project state | 強：明確區分歷史脈絡與當前真相 |

## 10.2 目前不能宣稱已解決的部分

### A. Semantic／cross-language retrieval

目前 FTS5、Unicode normalization 與 CJK bigrams 只能在 query 和 record 有共享 lexical clue 時
穩定工作。四個刻意移除共享詞的 probes 全部 Hit@3 失敗，已證明 candidate generation 的邊界。

### B. Model behavior usefulness

現有 tests 證明資料、scope、liveness、graph expansion 與 lexical retrieval 正確；它們尚未證明
同一模型因 memory 更少重犯錯誤、更願意推翻 obsolete decision，或在 unrelated task 不退步。

### C. Procedural memory

Decision／experience 可描述做法，但還沒有 Voyager／AWM 那種 first-class workflow、precondition、
verification 與 executable skill 契約。

### D. Longitudinal learning quality

尚未有數週／數月資料證明 silent mode 下 outcome capture 足夠、重複成功不會污染 evidence graph、
agent 會在反證後主動重看 decision。

### E. Selective forgetting

目前保守保存 audit chain 是合理起點，但對大量低價值、重複、過期記憶，還沒有經 benchmark 驗證的
decay／archive／consolidation policy。

## 10.3 為什麼同義與跨語言是 0/4

流程可以精確寫成：

1. Query 被 NFKC、casefold、英文 tokenization／CJK bigram 正規化。
2. FTS5 只會從這些字面 term 產生 candidate rows。
3. 英文 paraphrase 若換掉核心名詞，繁中 query 若對應英文 record，兩邊 term intersection 可能為零。
4. 正確 record 沒進 candidate set，後面的 priority、scope、graph-link bonus 與 token budget 完全沒有機會救回。
5. 所以問題在 **recall gate**，不是把 BM25 或 scope 權重調高就會好。

較合理的假說是：

- 保留 lexical route，因為 exact identifier／path／error message 是它的強項；
- 加一個 local multilingual semantic candidate route；
- 對 project alias、縮寫與時間詞做 query expansion；
- 候選合併後再套 active-only、machine hard filter、scope、supersession、time 與 budget；
- 用真實去敏 query set 比較 recall、false positive、intrusion、latency 與 downstream correctness。

這只是研究結論，不是本次已實作的變更。

---

# 11. `project-c` compact 成功案例：有價值，但要正確解讀

使用者回饋：昨天 `project-c` 專案在 compact 後，新的上下文成功找回專案架構與當前工作，體感非常好。
本次已將它記成一筆帶 applicability 的本機 experience，而不是直接把它升格為永久設計定律。

從 rollout 與本機狀態可確認的鏈條是：

1. 同一工作歷經多次 compact；
2. 最近一次 compact 後，SessionStart 注入了 bounded capsule，包含當前 contract、已完成狀態、
   下一步與驗證方式；
3. agent 接著重新打開相關 repo 檔案與 tests，沒有只靠摘要宣稱現況；
4. 使用者明確回饋恢復結果很好。

這是一個高價值的 **ecological success case**，因為它命中 RealMem 所說的 evolving project state／
Code Architecture 類型，也符合 ReadAgent 的「gist 導航＋原始資料查證」。但它還不是 causal proof：
沒有同時跑 no-memory、corrupted-capsule 與 full-history controls，無法量出有多少效果來自 capsule，
有多少來自 agent 重新讀 repo。

最好的後續不是否定這個成功，而是把它變成 regression：

- 固定模組、依賴、constraint、已否決方案、當前 state、next action 等 10–20 題；
- 在 pre-compact、post-compact、新 session 測；
- 比 correct capsule、capsule only、explicit retrieval only、no memory、stale／corrupted capsule、
  full history；
- 加入其他 project decoys；
- 同時評分 fact／dependency correctness、source back-reference、stale adoption、intrusion、abstention、
  token、latency，以及下一個實際 action 是否正確。

---

# 12. 下一輪研究優先序（不是本次 coding plan）

## P0：先把真實效果測出來

建立去識別化 project-memory suite，優先採 RealMem、LongMemEval、LoCoMo-Plus、Mem2ActBench 的題型：

- static recall；
- dynamic update／supersession；
- temporal reasoning；
- implicit cue／semantic disconnect；
- no-answer／abstention；
- project decoy／intrusion；
- tool argument／next action；
- compact recovery。

每一題至少跑多次、condition randomized，先用 deterministic rubric，再用 blind human／calibrated
judge 輔助。不要用同一批題一邊調 ranking、一邊宣稱泛化。

## P1：只改 candidate generation，不破壞已證明有效的 lexical path

比較三個條件：lexical only、semantic only、hybrid late fusion。Semantic route 應支援繁中／英文，
而且整個模型、index 與資料流的 privacy／offline 邊界要明確。成功條件不能只有 semantic Hit@k；
還要確保 no-answer false positive、cross-project intrusion、context tokens 與最終 correctness 不惡化。

## P2：把 compact recovery 做成一級 benchmark

測 capsule 是否保存 objective、current state、exact next action、validation、paused work、rollback 與
source pointer。特別加入在 subgoal 中途 compact、capsule 缺欄位、capsule 過期、另一 session 同時
更新等 cases。

## P3：獨立研究 procedural memory

先用不可執行的 workflow spec 做 prototype，再決定是否需要 executable skill。必須測 precondition
drift、工具版本、權限、副作用、rollback 與跨 project 誤套用；不能因 AWM／Voyager 成功就直接讓
舊 workflow 自動執行。

## P4：Graph 要由 multi-hop／temporal ablation 贏得複雜度

先建立有 entity、time、supersession 與 dependency ground truth 的 cases。若 graph 對 downstream
state／action 沒有超越強 flat／hybrid baseline，就不應只為了架構新潮承擔 ingestion、privacy、
debug 與 stale-edge 成本。

## P5：最後才考慮 learned controller

當 write／retrieve／update／forget 的評估集與硬限制成熟後，再比較 heuristic 與 AgeMem／Memory-R1
式 learned policy。Reward 必須同時含 task、memory quality、intrusion、safety、cost 與可逆性；
不能只獎勵最終答案。

---

# 13. 可否證的研究主張

為避免文件變成信念宣言，下一步應直接嘗試推翻下列主張：

1. **Capsule 因果主張**：correct capsule 在 post-compact project QA／action 顯著優於 no-memory，
   corrupted capsule 會造成對應、可檢出的錯誤。
2. **Hybrid retrieval 主張**：hybrid 在 semantic／cross-language recall 顯著改善，同時沒有增加
   no-answer false positive、跨 project intrusion 或 downstream regression。
3. **Counterevidence 主張**：retrieving contradictory／narrowing experiences 能降低 obsolete-decision
   adoption，而不是只讓答案變長。
4. **Silent hook 主張**：不顯示 maintenance pressure 能維持 liveness／工作品質，且長期 outcome
   capture 不低到使 memory 停止學習。
5. **Audit-chain 主張**：保留 supersession 與原 evidence，使 correction／stale-state tests 比
   in-place UPDATE／DELETE 更可靠，成本仍可接受。
6. **Graph 主張**：只有在需要 multi-hop、temporal dependency 的題型，graph 才應帶來超越 strong
   flat baseline 的 downstream gain。

只要實驗不支持，就應保留舊證據、記錄 change driver，再修改 decision；這本身就是動態記憶應有的
學習方式。

---

# 14. 結論

學術文獻支持使用者一開始抓到的精髓：**記憶的價值不在保存一份永遠正確的答案，而在讓不同
session 快速取得可查證、可反駁、會隨後果更新的經驗地板。**

它同時也支持使用者的警告：hook message 會影響模型；一次成功可能被誤信；經驗越多不一定越好；
模型可能根本沒有真正使用自己寫的摘要；某些 benign experience 還會造成 action bias。這些不是
邊角問題，而是 agent memory 的核心難題。

因此目前最好的總體設計立場可以濃縮為：

> **摘要負責導航，canonical evidence 負責查證；typed memory 負責分工，supersession 負責演化；
> current state 負責推翻過期記憶，causal evaluation 負責確認它是否真的改善行為。**

現有 `agent-memory` 已經有可工作的 transport、session recovery、audit chain 與 decision-outcome
語意；`project-c` compact 成功是值得保留的真實正面證據。下一步最值得做的不是立刻追逐最複雜的
graph 或 RL controller，而是把這個成功變成可重複的 project-memory causal benchmark，補上
semantic／cross-language candidate generation，再讓每一個新架構用 state 與 action 的結果證明自己。

---

# 參考文獻與官方入口

## 架構、記憶與經驗

1. Sumers et al. [Cognitive Architectures for Language Agents (CoALA)](https://openreview.net/forum?id=1i6ZCvflQJ). TMLR, 2024.
2. Park et al. [Generative Agents: Interactive Simulacra of Human Behavior](https://doi.org/10.1145/3586183.3606763). UIST, 2023.
3. Packer et al. [MemGPT: Towards LLMs as Operating Systems](https://arxiv.org/abs/2310.08560). Preprint, 2023.
4. Zhong et al. [MemoryBank: Enhancing Large Language Models with Long-Term Memory](https://ojs.aaai.org/index.php/AAAI/article/view/29946). AAAI, 2024.
5. Shinn et al. [Reflexion: Language Agents with Verbal Reinforcement Learning](https://papers.nips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html). NeurIPS, 2023.
6. Zhao et al. [ExpeL: LLM Agents Are Experiential Learners](https://ojs.aaai.org/index.php/AAAI/article/view/29936). AAAI, 2024.
7. Wang et al. [Voyager: An Open-Ended Embodied Agent with Large Language Models](https://openreview.net/forum?id=P8E4Br72j3). TMLR, 2024.
8. Wang et al. [Agent Workflow Memory](https://proceedings.mlr.press/v267/wang25bx.html). ICML, 2025.
9. Xu et al. [A-MEM: Agentic Memory for LLM Agents](https://proceedings.neurips.cc/paper_files/paper/2025/hash/19909c36f51abc4856b4560aff3d36d6-Abstract-Conference.html). NeurIPS, 2025.
10. Gutiérrez et al. [HippoRAG: Neurobiologically Inspired Long-Term Memory for Large Language Models](https://proceedings.neurips.cc/paper_files/paper/2024/hash/6ddc001d07ca4f319af96a3024f6dbd1-Abstract-Conference.html). NeurIPS, 2024.
11. Hu et al. [Does Memory Need Graphs?](https://aclanthology.org/2026.acl-long.1232/). ACL, 2026.
12. Wu et al. [GAM: Hierarchical Graph-based Agentic Memory for LLM Agents](https://aclanthology.org/2026.acl-long.1600/). ACL, 2026.
13. Yu et al. [Agentic Memory: Learning Unified Long-Term and Short-Term Memory Management](https://aclanthology.org/2026.acl-long.981/). ACL, 2026.
14. Yan et al. [Memory-R1](https://aclanthology.org/2026.acl-long.583/). ACL, 2026.
15. Latimer et al. [Hindsight: Structured Agent Memory that Retains, Recalls, and Reflects](https://aclanthology.org/2026.acl-demo.27/). ACL System Demonstrations, 2026.
16. Chhikara et al. [Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory](https://arxiv.org/abs/2504.19413). Preprint, 2025.

## Working context 與 compact

17. Lee et al. [A Human-Inspired Reading Agent with Gist Memory of Very Long Contexts](https://proceedings.mlr.press/v235/lee24c.html). ICML, 2024.
18. Hu et al. [HiAgent: Hierarchical Working Memory Management](https://aclanthology.org/2025.acl-long.1575/). ACL, 2025.
19. Li et al. [CompactionRL](https://arxiv.org/abs/2607.05378). Preprint, 2026.

## Benchmark 與評估

20. Maharana et al. [Evaluating Very Long-Term Conversational Memory of LLM Agents (LoCoMo)](https://aclanthology.org/2024.acl-long.747/). ACL, 2024.
21. Wu et al. [LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory](https://openreview.net/pdf?id=pZiyCaVuti). ICLR, 2025.
22. Tan et al. [MemBench](https://aclanthology.org/2025.findings-acl.989/). Findings ACL, 2025.
23. Hu et al. [MemoryAgentBench](https://arxiv.org/abs/2507.05257). Preprint, 2025.
24. Bian et al. [RealMem: Benchmarking LLMs in Real-World Memory-Driven Interaction](https://aclanthology.org/2026.findings-acl.703/). Findings ACL, 2026.
25. Li et al. [LoCoMo-Plus](https://aclanthology.org/2026.acl-long.1150/). ACL, 2026.
26. Shen et al. [Mem2ActBench](https://aclanthology.org/2026.acl-long.370/). ACL, 2026.
27. Zhang et al. [XRAG: Cross-lingual Retrieval-Augmented Generation](https://arxiv.org/abs/2505.10089). Preprint, 2025.（鄰近領域證據）

## Faithfulness 與安全

28. Zhao et al. [Large Language Model Agents Are Not Always Faithful Self-Evolvers](https://arxiv.org/abs/2601.22436). ICML, 2026.
29. Xiong et al. [How Memory Management Impacts LLM Agents](https://aclanthology.org/2026.acl-long.27/). ACL, 2026.
30. Zhao et al. [On Safety Risks in Experience-Driven Self-Evolving Agents](https://aclanthology.org/2026.findings-acl.2091/). Findings ACL, 2026.
31. Chen et al. [AgentPoison: Red-teaming LLM Agents via Poisoning Memory or Knowledge Bases](https://proceedings.neurips.cc/paper_files/paper/2024/hash/eb113910e9c3f6242541c1652e30dfd6-Abstract-Conference.html). NeurIPS, 2024.

---

**版本說明**：本文件是 2026-08-13 的研究快照。快速演進中的 preprint、benchmark 排名與軟體
實作可能更新；任何後續架構決策都應回到最新版論文、released code 與本機可重現實驗重新查證。
