# Agent Memory System

給 Claude Code、Codex 這類寫程式 agent 用的本機長期記憶。

它補上 agent 自己沒有的三件事：一份撐得過 context 壓縮與重開的「進度膠囊」；每次對話依問題自動帶進有限篇幅的相關決策、經驗與使用者偏好；以及在 agent 快要重蹈覆轍的那一刻，把當年的教訓放到它眼前。所有資料都是你自己擁有的 Markdown 帳本，用 Git 管版本。執行只需要 Python 3.11、Git 與 SQLite，沒有雲端、沒有向量模型、沒有常駐服務。

它不是會催模型的外掛。沒有相關資料時 hook 保持安靜，永遠不阻擋，也不會把「你還沒寫記憶」這種壓力塞進模型的 context。記憶是資料，不是指令。

- [它做什麼](#它做什麼)
- [為什麼是樹狀路由，不是一個大 RAG](#為什麼是樹狀路由不是一個大-rag)
- [架構](#架構)
- [安裝](#安裝)
- [平常怎麼用](#平常怎麼用)
- [六種紀錄](#六種紀錄)
- [檔案放在哪裡](#檔案放在哪裡)
- [怎麼知道它有沒有用](#怎麼知道它有沒有用)
- [隱私與安全](#隱私與安全)
- [內建的繁體中文文字](#內建的繁體中文文字)
- [限制](#限制)
- [程式結構](#程式結構)
- [授權](#授權)

English: [README.md](README.md)

## 它做什麼

- **跨 session 接續**：每個 agent session 有一份有上限的進度膠囊（進行中的事、暫停的事、下一步、驗證方式、回退方式）。重開、`--resume` 或 context 壓縮之後，SessionStart 精確還原這個 session 自己的膠囊，其他進行中的工作只列一行摘要。
- **偏好記得住**：使用者明講的偏好依優先級在開場載入；優先級低的只在問題命中時才出現。
- **每次對話帶相關記憶**：UserPromptSubmit 把你的問題當唯讀查詢，有命中才注入 Context Pack；問題本身不會被存下來。
- **教訓出現在危險的那一刻**：失敗紀錄有 `retry_when` 欄位。PreToolUse hook 拿 agent 正要執行的指令去比對這些教訓，命中就把那筆失敗附在指令結果旁邊。另外有一份由你維護的危險指令清單（強制推送、`rm -rf`、`reset --hard`……），每個 session 第一次碰到會停一次並顯示教訓，第二次放行。
- **決策靠結果校準**：決策只是「當時的選擇」，不是規則。經驗以 `supports`、`contradicts`、`narrows`、`inconclusive` 連到決策；檢索時決策旁邊會顯示證據計數與最新反證。改決策一定要 `supersede`，附上改變的驅動因素與理由，舊版留在稽核鏈裡。
- **專案知識用聯邦，不複製**：專案自己的 ADR、結算檔、筆記、git log 透過一份設定檔唯讀索引進來。跨報告重複出現的根本原因會自動變成「教訓候選」；不會有東西在你不知情時寫進帳本。
- **自我量測**：每週 `memoryctl doctor` 檢查管線並量「注入的記憶有沒有真的被用到」（從 agent 寫的內容判斷）；`memoryctl rate` 是每週十幾題的簡短問卷。

## 為什麼是樹狀路由，不是一個大 RAG

一張平面索引罩住所有紀錄，資料少的時候沒問題。帳本一大，每個問題都要跟全部紀錄比分數，一套公式要同時服務偏好、失敗、事實、決策，問題越長撞到的無關紀錄越多。用門檻和停用詞補洞只是把上限往後推。

這套系統改成路由。每一輪只打開用得到的節點，每個節點只有幾十到幾百筆，各自有預算、各自用最適合的查法。有些節點根本不是檢索：偏好是規則表，進度膠囊是查表，失敗是由「正要跑的指令」觸發。

```
根（每一輪依 cwd、事件、問題、工具呼叫決定開哪些節點）
├─ rules          偏好：優先級表；低優先級靠問題命中
├─ capsule        本 session 的進度膠囊：依 session id 查表
├─ project/<名稱> cwd 所在的專案（點名才開第二個）
│   ├─ decisions      小索引，證據鏈以指標帶出
│   ├─ experiences    小索引
│   ├─ failures       這個專案自己的失敗教訓
│   └─ sources        聯邦進來的 ADR／結算／筆記 → 根本原因分組 → PROJECT LESSONS
├─ knowledge/<主題>  跨專案的經驗與決策，依主題分
├─ facts          依實體詞查（主機、工具名、別名）
├─ failures       全域教訓：由指令觸發，不由問題字詞觸發
└─ history        存檔、事件、閒置膠囊：不注入，想查再查
```

雜訊不再隨帳本總量成長，成長的是節點數。在作者自己的帳本上，兩個典型問句的注入從 9 筆、8 筆（有用 1 筆、0 筆）變成 2 筆、1 筆（有用 1 筆、1 筆），預算完全相同。

## 架構

### 兩層資料

| 層 | 內容 | 位置 | 可重建 |
|---|---|---|---|
| 正本帳本 | 每筆紀錄一個 Markdown 檔加 JSON 中繼資料；被取代的版本在 `archive/`；每次寫入一個不可變事件；每個 session 一份進度膠囊 | 記憶儲存目錄（預設 `~/agent-memory`），自動 commit 進 Git | 不行，這就是資料 |
| 導出索引 | SQLite FTS5，欄位分 identity、content、evidence，另有路由用的 `node` 欄；幾個小 JSON 快取（失敗觸發片語、事實實體詞） | `<store>/.cache/` | 可以，`memoryctl reindex` |

寫入是原子的（暫存檔加改名），由檔案鎖序列化。每次寫入都算雜湊、追加事件、做一個 Git commit。索引在寫入時增量更新；hook 只讀快照加上有限的最近寫入覆蓋，永遠不自己重建。

### Hook：決定性、出錯就放行的傳輸層

| 事件 | 注入什麼 | 預算（Claude Code／Codex） |
|---|---|---|
| SessionStart | 本 session 的進度膠囊、critical 與 high 偏好、最多三筆其他進行中任務、cwd 在聯邦專案內時的 PROJECT LESSONS | 5000／3400 tokens |
| UserPromptSubmit | 這個問題路由出來的 Context Pack，分成 NON-NEGOTIABLE CONSTRAINTS、WORKING DECISIONS、RELEVANT EXPERIENCES、REFERENCE DATA、PROJECT LESSONS、SOURCE IDS；在 Claude Code 裡，上一則回覆命中某筆失敗的 `outbound_triggers` 時，最前面另有一行 `上一則回覆可能需要更正：…（id）` | 3000／1800 tokens |
| PreToolUse（Bash） | `FOR THIS COMMAND`：最多兩筆觸發片語命中這條指令的失敗教訓；或危險清單指令的一次性暫停 | 600／400 tokens |
| PreToolUse（SendMessage、AskUserQuestion；只有 Claude Code） | `送出的內容可能需要再查：`：最多兩筆 `outbound_triggers` 命中正要送出內容的失敗教訓；從不暫停 | 600 tokens |
| PreCompact | 不注入，只在本機寫一個標記 | - |

每個 hook 出任何錯都以 exit 0 回 `{"continue": true}`；PreToolUse 因為在每個 shell 指令前執行，另外會在 2 秒後自己放棄。錯誤只記在本機的小健康檔，不給模型看。不安裝 Stop hook。

agent 有沒有用到某筆記憶，是從它寫的內容判斷，不再要求它寫：在使用者下一則訊息之前，它的回覆或工具輸入寫出那筆的 id，或重複那筆特有的內容（長路徑、commit hash、帶單位的數字、八個字的中文或四個字的英文片語），而且使用者自己的訊息裡沒有。只有關掉路由時，Context Pack 第一行才仍請 agent 寫「依據 <id>」。

### 證據，不是信心分數

決策紀錄有 value、rationale、constraints、revisit_when。經驗紀錄有情境、預期、行動、實際結果、適用範圍，以及對決策的連結。檢索時決策和它的證據計數、最新反證一起出現。`memoryctl audit` 列出沒有結果的決策與出現反證的決策，但從不自動改任何東西。

### 聯邦與教訓

`~/.config/agent-memory/sources.d/<名稱>.toml` 描述一個專案：目錄、所屬標籤、別名，以及 ADR、結算檔、Markdown 筆記、git log 各自的解析器。`memoryctl sources sync <名稱>` 唯讀索引；專案的 post-commit hook 可以自動跑。結算裡的發現依根本原因分組，兩份以上報告都出現的組標成「泛化候選」，連同次數出現在 PROJECT LESSONS。專案本身不需要為了記憶系統改任何檔案。

### 信任邊界

1. 當下的使用者指示與剛驗證過的本機狀態，永遠高於記憶。
2. 記憶紀錄是資料；每一包都這樣告訴 agent。
3. 問題只是唯讀查詢輸入，不會被保存。
4. 憑證、身分資料、對話逐字稿、未確認的猜測，由驗證規則與政策一起擋在外面。
5. Hook 不會把審查、容量、用量、健康狀態的壓力放進模型看得到的 context。

## 安裝

需求：Linux 或 macOS、Python 3.11 以上、Git、含 FTS5 的 SQLite（CPython 內建就有），以及支援 hook 的 Claude Code 2.1.29x 以上或 Codex CLI。不需要任何第三方 Python 套件。

```bash
git clone https://github.com/EdwardLeeee/agent-memory-system.git ~/agent-memory-system
mkdir -p ~/.local/bin
ln -s ~/agent-memory-system/bin/memoryctl ~/.local/bin/memoryctl   # 確認 ~/.local/bin 在 PATH 裡
```

程式目錄和記憶儲存目錄是兩個不同的地方。儲存目錄預設 `~/agent-memory`，可用 `AGENT_MEMORY_HOME` 改；本機設定在 `~/.config/agent-memory`（`AGENT_MEMORY_CONFIG_HOME`）。

```bash
memoryctl init --git          # 建立儲存目錄、它的 Git 倉庫、這台機器的 id
memoryctl validate
memoryctl adapter install claude   # 或／和：memoryctl adapter install codex
```

`adapter install` 會先備份你的設定，再把 hook 合併進 `~/.claude/settings.json`（或 `~/.codex/hooks.json`），把一小段指引合併進 `~/.claude/CLAUDE.md`（或 `~/.codex/AGENTS.md`）。只動標記之間那一段，其餘內容原樣保留。Claude Code 裡執行一次 `/hooks` 檢查並信任，然後開新的 session：第一則訊息開頭應該有 Durable Memory Context Pack。

從終端驗證：

```bash
memoryctl context --cwd ~ --agent claude --query "記憶是怎麼存的" --explain
memoryctl health
```

可選：

```bash
cp ~/agent-memory-system/docs/router.example.toml ~/.config/agent-memory/router.toml   # 預算、主題詞表、危險清單
memoryctl doctor install-timer --enable      # 每週健康報告（Linux 的 systemd --user）
memoryctl sources sync <專案>                # 先寫好 ~/.config/agent-memory/sources.d/<專案>.toml
cp ~/agent-memory-system/docs/generate.example.toml ~/.config/agent-memory/generate.toml   # 選定紀錄的 Markdown 檔
```

herdr 的整合是選用的：危險清單裡的 `herdr server stop`、agents 主題詞裡的 herdr，以及裝了 herdr 時 doctor 透過它送出的通知。沒有 herdr 時，這幾項都不會有任何作用。

升級：`git -C ~/agent-memory-system pull && memoryctl validate && memoryctl reindex`。索引有版本號；新版本在 `reindex` 時重建，hook 不會自己重建，只會先保持安靜。

## 平常怎麼用

正常工作時不用打指令。agent 讀 hook 注入的內容，並依指引在有意義的時刻自己寫記憶。你可以直接用白話要求 agent：

- 「這個專案上次做到哪？先看記憶，再查 repo。」
- 「這次結果有沒有推翻以前的決策？有的話記一筆經驗，不要直接改決策。」
- 「跑一次記憶稽核，告訴我哪些決策還沒有結果。」
- 「這是可靠的新證據，supersede 舊決策，舊版留著。」

背後的指令：

| 指令 | 用途 |
|---|---|
| `memoryctl record --kind <種類> --input file.json` | 寫入偏好、事實、決策、經驗、失敗或任務 |
| `memoryctl checkpoint --input file.json` | 存本 session 的進度膠囊（session id 由 agent 環境提供）；選填的 `tasks` 一項一行 |
| `memoryctl checkpoint --set 欄位=值` / `--show` | 直接改存好的膠囊，一個欄位或 `tasks.<id>.status=done`；印出膠囊 |
| `memoryctl template <種類>` | 印出 record、supersede、checkpoint 的 JSON 骨架；缺欄位時會一次列出全部 |
| `memoryctl task done <id>` | 把待辦標成完成（task 可帶含時區的 `due`；一天內到期的 open 待辦會在開場列出） |
| `memoryctl supersede --id <id> --input file.json` | 以新版本與理由取代現行紀錄 |
| `memoryctl context --cwd 目錄 --query 文字 [--agent claude|codex] [--explain]` | 看某個問題會注入什麼，每筆附節點與分數 |
| `memoryctl search --query 文字 [--include-archive]` | 查歷史：舊版本、事件、閒置膠囊 |
| `memoryctl sessions list` / `show --session-id ID` | 進度膠囊 |
| `memoryctl audit` | 沒有結果的決策、出現反證的決策 |
| `memoryctl lessons --cwd 目錄` | 聯邦專案算出來的教訓候選 |
| `memoryctl validate` / `reindex` / `health` | 完整性、重建索引、hook 健康 |
| `memoryctl doctor run` / `memoryctl rate` | 每週報告、每週評分 |
| `memoryctl export --output file.gpg` / `import` | 加密搬到另一台機器 |

每種紀錄的輸入範例在 `examples/`。

## 六種紀錄

| 種類 | 內容 | 怎麼被找到 |
|---|---|---|
| preference | 使用者明講、穩定的規則（`priority`：critical、high、normal、low） | 開場依優先級表；normal 與 low 靠問題字詞 |
| fact | 驗證過、可能會過期的事實（`expires_at`）；機器範圍的事實不會離開那台機器 | 實體詞（主機、工具、別名） |
| decision | value、rationale、constraints、alternatives、consequences、revisit_when | 專案或主題節點 |
| experience | 情境、預期、行動、實際結果、適用範圍、決策連結 | 專案或主題節點；或作為決策的證據被帶出 |
| failure | 症狀、嘗試、根本原因、驗證過的修法、retry_when、證據，可選的 `triggers` regex（比對指令）與 `outbound_triggers`（比對 agent 說的話） | 正要執行的指令；Claude Code 正要送出或剛回覆的內容；本專案的失敗另可由字詞 |
| task／checkpoint | 長期任務事實／本 session 的進度膠囊 | 歷史層／精確 session id |

範圍：`global`、`project:<名稱>`、`workspace:<名稱>`、`machine:<uuid>`。機器範圍是硬過濾。

## 檔案放在哪裡

```text
~/agent-memory/                      記憶儲存目錄（預設；AGENT_MEMORY_HOME）
├── manifest.json
├── records/<種類>/<id>.md
├── archive/<種類>/<id>/vN-<時間>.md
├── events/YYYY/MM/<事件id>.json
├── sessions/by-session/<session id>/HOT.md
└── .cache/                          導出資料，可刪
    ├── index.sqlite3  index.dirty  triggers.json  fact_entities.json

~/.config/agent-memory/              本機設定（AGENT_MEMORY_CONFIG_HOME）
├── machine.json                     這台機器的 id、scope_paths、scope_aliases
├── router.toml                      可選：預算、主題詞表、危險清單
├── sources.d/<專案>.toml            聯邦來源
├── doctor/golden.toml               每週報告用的黃金查詢
├── ratings/                         每週評分（只在本機）
└── runtime/                         健康狀態、PreToolUse 狀態、doctor 計數
```

## 怎麼知道它有沒有用

- **`memoryctl doctor run`**（可裝每週計時器）檢查：未推送的 commit、hook 健康、各 agent 開場包大小、偏好成長、過期膠囊、來源同步時間、黃金查詢、記憶體夠時跑測試、自己的峰值記憶體。效果訊號：注入紀錄的使用率（明寫 id 與內容相符）、注入過但從未被用到的紀錄、重複犯錯、重複交代、壓縮後恢復成功率、失敗層命中。
- **`memoryctl rate`**：十幾題，問你這週看到的記憶有沒有用；答案只存本機。
- **黃金查詢**（`doctor/golden.toml`）釘住已知問題必須回、不得回的東西，檢索改動不會悄悄退步。
- 12 個合成案例（6 個檢索、6 個壓縮後接續）的配對 memory-on／off 試驗：memory-on 11/12 通過，memory-off 3/12。每案只跑一次，是方向性的結果，不是母體效果。細節在 `docs/EVALUATION.md`；評估用的題組本身不在這個 repo 裡。

## 隱私與安全

- 全部留在你的機器上。可選的語意檢索（預設關閉，由啟用證據檔把關）也在本機跑，沒有任何遠端模型呼叫。
- 驗證會擋常見的機密格式。不要存憑證、完整身分資料、對話逐字稿；指引區塊也這樣告訴 agent。
- PreToolUse hook 從不拒絕指令，唯一例外是你在 `router.toml` 管理的危險清單在每個 session 的第一次；在 `[failures]` 下設 `speed_bump_enabled = false` 可以連這個也關掉。
- `export`／`import` 用 GPG，密碼在終端互動輸入；另一台機器的機器範圍事實會當歷史保留，不會當成現況注入。

## 內建的繁體中文文字

作者用繁體中文工作，所以有些使用者或 agent 看得到的文字預設是中文。引導 snippet、Context Pack 的區段標題、指令說明是英文。

| 文字 | 出現在哪裡 | 能不能改 |
|---|---|---|
| `採用某筆記憶時，請在回覆寫一次「依據 <id>」。` | 關掉路由時 Context Pack 的第一行 | 沒有設定可改；在 `agent_memory/storage.py` 的 `CITATION_REQUEST`。開著路由時，使用率從 agent 寫的內容判斷。 |
| 危險清單的理由，例如「強制推送會覆蓋遠端歷史；…」 | PreToolUse 暫擋時的訊息 | 可以：把 `docs/router.example.toml` 的 `[[failures.speed_bump]]` 清單複製到 `router.toml`，自己寫 `reason`（這份清單會整個取代內建的）。 |
| 「本 session 尚未確認過這類指令；確認後再執行一次就會放行。」 | 每則暫擋訊息的最後一句 | 沒有設定可改；在 `agent_memory/pretool.py` 的 `RELEASE_LINE`。 |
| 「由記憶系統自動產生，不要手改；新增或修改請用 memoryctl record／supersede」 | 每個產生的 Markdown 檔的開頭 | 沒有設定可改；在 `agent_memory/generate.py` 的 `MARKER`。這行也代表記憶系統可以重寫這個檔案。 |
| 「送出的內容可能需要再查：」與「上一則回覆可能需要更正：」 | 關於正要送出內容的教訓之前，與關於上一則回覆的那一行開頭 | 沒有設定可改；在 `agent_memory/turns.py` 的 `OUTBOUND_HEADER` 與 `CORRECTION_PREFIX`。 |
| 包裡的輔助行：「- 學到的地方：…」、「更多：memoryctl search --query <詞>」、「（其餘 N 條：…）」、PROJECT LESSONS 的導覽行與「目前沒有教訓候選；…」 | Context Pack 裡 | 沒有設定可改；在 `agent_memory/pretool.py`、`router.py`、`core.py`。 |
| 部分預設主題詞（記憶、記憶體、閘門…） | 決定問句會打開哪些知識主題 | 可以：`router.toml` 的 `[knowledge]` 主題。 |
| `memoryctl doctor` 的報告與通知、`memoryctl rate` 的問題 | 終端機與報告資料夾 | 沒有設定可改；在 `agent_memory/doctor.py`、`rating.py`。 |

## 限制

- 記憶是證據，不是保證：模型仍可能忽略一條好教訓。
- 檢索是字面比對（英文切詞加 CJK bigram）。同義詞與跨語言是弱項；語意路徑寫好了，但沒過驗收門檻，出貨時關閉。
- 沒有指令特徵的失敗（判斷錯誤）只能靠專案節點的字詞查或 `memoryctl search`。
- 證據圖要 agent 主動記錄結果才會填滿；`audit` 只能指出洞，不能補。
- 只在 Linux 上以 Claude Code 與 Codex CLI 驗證過。Gemini 與通用 adapter 是手動的、未驗證。

## 程式結構

```text
agent_memory/      core.py（帳本、各種 pack）、router.py（路由樹）、pretool.py（失敗層）、lexical_index.py、
                   federation.py、doctor.py、rating.py、adapters.py、storage.py、cli.py
bin/memoryctl      進入點；PreToolUse hook 的快路徑
adapters/          合併進 CLAUDE.md／AGENTS.md／GEMINI.md 的指引片段
docs/              ARCHITECTURE.md、SCHEMA.md、EVALUATION.md、HANDBOOK.zh-TW.md、router.example.toml、sources.example.toml、generate.example.toml
examples/          每種紀錄的 JSON 輸入範例
tests/             單元與整合測試、逐位元組相同的路由基準 fixture
tools/             基準測試（題組不隨程式發布）、手冊產生器、公開版匯出
```

開發：`python3 -m unittest discover -s tests`（約 340 個測試，不需網路）。

## 授權

PolyForm Noncommercial 1.0.0。非商業用途可自由使用、修改、分享；商業使用須另向作者取得授權。見 `LICENSE`。
