# dwg_batch_tool

目前版本：**SCADA Engineering Data Analyzer — BOQ PDF Row Parsing Integration（`v1.6.0`）**。
人工工程核准與 Production Approval 均為 NOT APPROVED。

| 項目 | 狀態 |
| --- | --- |
| 自動化測試 | 279 collected。無 AutoCAD 的環境為 257 passed / 22 skipped（含缺字體修復、自動分圖層、文字擷取／取代；`accore` 測試在 AutoCAD 2027 上才會執行） |
| Real project validation | SB12 Round 1 FAIL（v1.5）→ Round 2 PASS（P0 修正，v1.5.1）→ Round 3 PASS（P1 evidence metadata，v1.5.2）；見 [docs/REAL_PROJECT_VALIDATION_STATUS.md](docs/REAL_PROJECT_VALIDATION_STATUS.md) |
| Production Approval | NOT APPROVED |
| 版本標籤 | `v1.6.0`（前版 `v1.5.2`、`v1.5.1`、`v1.5-semantic-validation-candidate`） |

功能凍結已「部分解除」（見 [VERSION_STATUS.md](VERSION_STATUS.md)）：允許受控的 BOQ 逐列解析、Designer 可行性測試與品質修正；Retrieval／RAG／Ollama／Dify／Open WebUI 仍凍結。正式狀態、真實資料驗收範圍與恢復條件見 [VERSION_STATUS.md](VERSION_STATUS.md)。

## 用途
DWG 批次轉 DXF，再以 Python + ezdxf 解析圖層 / Block / Attribute / Text / MText / Dimension，輸出 CSV / JSON / log，並搜尋 SCADA 相關關鍵字。

```
DWG -> accoreconsole.exe -> DXF -> ezdxf -> CSV / JSON / log
```

## 需求
- Windows、Python 3.10+
- 本機 Autodesk `accoreconsole.exe`（本機實測：`C:\Program Files\Autodesk\AutoCAD 2027\`，該資料夾含 `acad.exe`，為完整版 AutoCAD 2027，**不是 LT**；未找到 LT 2027）
- Python 套件：`ezdxf`（使用者層級 venv，不需管理員權限）

## 安裝
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```
`config.json` 的 `accoreconsole` 指向實際路徑（找不到時 `run.bat` 直接停止，不會猜路徑）。

## 目錄結構
```
input/        放 DWG（可含子目錄）
output/dxf    轉出的 DXF（保留子目錄結構）
output/csv    file_index / layers / texts / blocks / attribs / dimensions / scada_hits
output/json   每個 DXF 一份 JSON
output/logs   每次轉檔的 stdout/stderr、batch_results.csv/.jsonl、errors.log
scripts/      env_check, convert_single, batch_convert, analyze_dxf, find_scada, summary
tests/        pytest；tests/fixtures/sample.dwg 為本工具用 accoreconsole 產生的測試圖
```

## 環境檢查
```
.venv\Scripts\python scripts\env_check.py      # 寫入 environment_check.txt
```

## 桌面 UI
```
app.bat            （或 .venv\Scripts\python app.py）
```
tkinter 介面：選輸入/輸出資料夾、勾選處理選項、編輯關鍵字（「儲存設定」才寫入 config.json）、掃描、開始/停止、進度條、Log、結果摘要與開啟結果檔案按鈕。
處理在背景 thread 執行，UI 只透過 Queue 更新；「停止」只會終止本程式自己啟動的 accoreconsole。
GUI log 寫入 `output\logs\gui.log`。掃描不會執行 AutoCAD、不修改任何檔案。
「產生 CSV」控制 analyze 的 CSV；`scada_hits.csv` 只要勾選「SCADA 關鍵字搜尋」就會產生。

### DWG / DWT 版本表（免 AutoCAD、免管理員）
來源區的「產生 DWG/DWT 版本表」會直接唯讀檔案前 6 bytes 的 `ACxxxx` header，輸出 `output/csv/dwg_versions.csv`。
- 支援 `.dwg` 與 `.dwt`；DWT 使用相同的 DWG header 判讀方式。
- 不啟動 AutoCAD、不安裝 Shell Extension、不寫 Windows Registry、不需要系統管理員權限。
- 欄位：`file`、`extension`、`format_code`、`format`、`status`。
- `format` 直接以「`ACxxxx`→西元年」顯示，一眼看懂版本與年份；例如 `AC1032` 顯示為 `AC1032→2018`。
- 未知的新 `ACxxxx` 代碼會保留原碼並標記 `UNKNOWN_CODE`，不會猜測版本。

### 缺字體修復
「② 我要做什麼？」選擇「缺字體修復」。使用上方的輸入／輸出資料夾（兩者不可相同，輸出也不可放在輸入裡面），按「開始修復字體」。
每個 DWG 與 DWT（大小寫不分，`.DWT` 也算）會先整檔複製到暫存檔再交給 `accoreconsole` 開啟，所以原始圖不會被改寫；正在 AutoCAD 裡開啟的圖只要還能讀取，也會用複本處理。勾選「包含子資料夾」時，輸出保留相同的資料夾結構。樣板的輸出副檔名維持 `.dwt` / `.DWT`，不會改存成 `.dwg`。暫存檔一律用 `.dwg`：AutoCAD 對 `.dwt` 執行 OPEN 會從樣板另開新圖，而不是編輯該檔。

替換規則與已在 AutoCAD 2027 驗證過的 FONTFIX 原型相同：
- 逐一讀取文字樣式的主字體（DXF group 3）與大字體（DXF group 4）。
- 字體算「找不到」必須同時失敗：AutoCAD 支援路徑（`findfile`）、`%WINDIR%\Fonts`、`%LOCALAPPDATA%\Microsoft\Windows\Fonts`，以及（`.ttf/.ttc/.otf`）登錄的 TrueType 字型名稱。AutoCAD 會把已安裝字型記成 `Microsoft JhengHei.ttf`，實際檔案卻是 `msjh.ttc`，所以不能只比檔名。
- 沒有副檔名（例如 `txt`、`genie`）當成 `.shx`。`@` 開頭的直排字體略過。
- 找不到的主字體改成預設主字體（預設 `romans.shx`），找不到的大字體改成預設大字體（預設 `chineset.shx`）。「字體對照」可寫 `舊字體=新字體`，每行一筆，大小寫不分。已安裝的字體不動。
- 存檔前再檢查一次，仍然找不到的樣式寫進結果。

結果在輸出資料夾的 `logs\`：
- `fontfix_results.csv`：每個 DWG / DWT 一列（檔案、狀態、替換明細、仍缺字、錯誤）。
- `fontfix_results.jsonl`：同一批結果的結構化內容。
- `fontfix.log`：繁中文字紀錄（替換了哪個樣式、舊字體、新字體）。
- `fontfix_batch_*.stdout.log`：該次 accoreconsole 的主控台輸出。

「另存版本」預設「維持目前格式」（`QSAVE`，不改 DWG 版本）。選 2018／2013／2010／2007／2004／2000／R14 時改走 `SAVEAS` 的格式關鍵字。這個版本切換在沒有 AutoCAD 的環境無法執行，請在本機 AutoCAD 2027 用下方步驟確認。

實作取捨：accoreconsole 不能穩定使用 COM／ObjectDBX，所以樣式是用 AutoLISP `entmod` 改 STYLE 表，而不是原型裡的 ActiveX。TrueType 字型名稱由 Python 讀登錄後寫進 UTF-8（含 BOM）的 LISP；`findfile` 與 Windows 字型資料夾仍在 AutoCAD 裡檢查。批次開始前會把 `FONTALT` 設成 `txt.shx`，只為了避免缺字對話框卡住主控台，不會改寫樣式表。`XREF|樣式` 這類被鎖住的外部參考樣式若 `entmod` 被拒絕，會出現在「仍缺字」或錯誤欄，不會改到外部參考的來源檔。

修復結果用 `|=|` 分隔欄位。accoreconsole 會把 LISP 印出的定位字元（tab）展開成不定數量的空白，空的大字體欄位會消失，含空白的字型名稱（例如 `Microsoft JhengHei.ttf`）也會被切碎。用定位字元解析時，三張圖都會變成「沒有收到修復結果」，暫存 DWG 也不會搬進輸出資料夾。解析器仍讀得懂舊的定位字元，以及這次實測留下的空白分隔輸出。

本機確認（Windows、AutoCAD 2027，不是 LT）：
1. 選「缺字體修復」，輸入資料夾放含缺字 SHX 的 DWG，並放至少一個 `.dwt` 與一個 `.DWT`。輸出選另一個資料夾。可讓其中一個檔案維持在 AutoCAD 開啟。
2. 維持預設 `romans.shx`／`chineset.shx`，可加一筆對照（例如 `sino2.shx=txt.shx`），勾選子資料夾後開始。
3. 進度會逐檔前進。原始檔時間與內容不變，被開啟的那張圖也沒被存回。
4. 輸出的 DWG 在 AutoCAD 2027（zh-TW）打開時，被替換的樣式不再跳缺字；`Microsoft JhengHei.ttf`、`arial.ttf`、`txt` 與 `@` 開頭的樣式維持原樣。輸出的樣板檔名仍是 `.dwt` / `.DWT`（不會多出同名 `.dwg`），開啟後仍可當樣板，樣式替換與 DWG 相同。
5. `logs\fontfix_results.csv` 與 `fontfix.log` 有舊字體 → 新字體，以及存檔後仍缺字的樣式。
6. 「另存版本」選 AutoCAD 2013，再用「DWG / DWT 版本表」確認該 DWG 與該 DWT 都是 `AC1027→2013`，且 DWT 輸出副檔名沒有變成 `.dwg`。
7. 處理中按「停止」，批次會在目前檔案後停下，已完成的輸出保留。

### 文字擷取
「② 我要做什麼？」選擇「文字擷取」。使用上方的輸入／輸出資料夾（兩者不可相同，輸出也不可放在輸入裡面），輸入搜尋文字後按「開始擷取文字」。

這個模式只讀圖，不輸出 DWG，也不修改原始圖檔。每個 DWG 先複製到暫存檔再交給 `accoreconsole`，所以正在 AutoCAD 裡開啟的圖只要還能讀取，也會用複本處理。

AutoLISP 沒有正規表示式。LISP 只負責走訪文字並印出清單，Python 再套用規則：
- 物件：`TEXT`、`MTEXT`、`ATTRIB`、`ATTDEF`。模型空間與紙空間配置都掃。`ATTRIB` 若掛在圖塊插入上，會帶該圖塊名稱。
- 「包含圖塊定義裡的文字」預設關閉。打開後才會走圖塊定義（含匿名圖塊），座標是圖塊內部座標，空間欄位寫「圖塊定義」。取代模式也預設關閉，避免一改定義就改到每一個插入。
- 比對方式：包含文字、萬用字元（`*` 與 `?`，比對整段文字）、正規表示式。預設不區分大小寫。
- `MTEXT` 先去掉格式碼再比對（`\H`、`\C`、`\f`、顏色與大括號等）。堆疊分數 `\S...;` 與欄位碼裡的文字不納入比對。

結果在輸出資料夾的 `logs\`（CSV 為 UTF-8 含 BOM，Excel 可直接開中文）：
- `textextract_hits.csv`：一筆符合一列。欄位為檔案、空間/版面、圖層、物件類型、圖塊名稱、文字內容、符合部分、X、Y、handle。
- `textextract_results.csv`：每個 DWG 一列（狀態、符合筆數）。沒有符合的檔案狀態是「無符合」，仍算成功。
- `textextract.log`、`textextract_batch_*.stdout.log`。

### 文字取代
「② 我要做什麼？」選擇「文字取代」。尋找／取代成可以是一般文字，或正規表示式（取代內容可用 `\1`、`\2`）。範例：尋找 `^(W\d+)\s.*$`、取代成 `\1`，會留下假梁編號、去掉後面的尺寸。

修改只發生在暫存複本。有變更的 DWG 才寫進輸出資料夾；原始圖的時間與內容不變。
- 「只產生變更清單，不輸出 DWG」是預覽。狀態為「預覽」，仍然寫出變更清單。
- 沒有符合的檔案狀態是「無變更」，預設不複製。勾選「沒有變更的檔案也複製到輸出資料夾」才會複製。
- 「把變更過的文字改成 ACI 色號」只改有動到的文字（0=BYBLOCK，256=BYLAYER）。沒勾就保持原色。
- 已存在的輸出 DWG 會略過，除非勾選覆寫。

結果：
- `textreplace_changes.csv`：檔案、圖層、物件類型、handle、原文字、新文字、次數。
- `textreplace_results.csv`：每個 DWG 的狀態與變更筆數。
- `textreplace.log`，以及擷取與寫回各一份 `textreplace*_batch_*.stdout.log`。

MTEXT 會把新的純文字嵌回原本的格式碼，所以沒改到的字仍保留字高與字型。若格式碼對不回去（例如取代碰到堆疊分數），就改寫成已跳脫的純文字，狀態仍是「已輸出」或「預覽」，錯誤欄與 log 會寫「MTEXT 格式碼無法完整保留」。`TEXT` 的 DXF 文字約 250 字一個群組，較長的結果會切段寫回。多行 `ATTRIB` 只處理群組 1。

不在範圍內：標註文字、引線、表格、外部參考裡的文字、欄位碼的計算結果。`FONTALT` 只設成 `txt.shx` 以避免缺字對話框，不會改文字樣式。版本維持 `QSAVE` 的目前格式。

本機確認（Windows、AutoCAD 2027，不是 LT）：
1. `git pull` 這個分支後，選「文字擷取」，搜尋一段已知的梁編號或中文註解。
2. 開啟 `logs\textextract_hits.csv`，核對空間、圖層、圖塊名稱與座標。原始 DWG 的修改時間不變。
3. 選「文字取代」，用上面的 `^(W\d+)\s.*$` → `\1`，先勾「只產生變更清單」。確認沒有輸出 DWG，變更清單的新文字只剩編號。
4. 取消預覽再跑一次。輸出 DWG 用 AutoCAD 打開，被改的 MTEXT 仍保留字高；沒有符合的檔不會出現在輸出，除非有勾複製。
5. 讓其中一個 DWG 維持在 AutoCAD 開啟，再跑一次，原始檔仍不被存回。

### 自動分圖層
「② 我要做什麼？」選擇「自動分圖層」。圖檔常常整張都在圖層 0。這個模式依物件類型把實體搬到對應圖層，使用和缺字體修復相同的 accoreconsole 批次、輸出資料夾、`logs\`、`|=|` 結果列與暫存複本。原始圖檔不會被改寫；正在 AutoCAD 裡開啟的圖只要還能讀取，也會用複本處理。

預設對照（可在畫面裡改，圖層名稱空白表示不搬該類型）：
- `LWPOLYLINE`、`POLYLINE` → `PL`
- `TEXT`、`MTEXT` → `TST`
- `HATCH` → `H`
- `LINE` → `LINE`，`ARC` → `ARC`，`CIRCLE` → `CIRCLE`
- `DIMENSION` → `DIM`，`LEADER` 與 `MULTILEADER` → `LEAD`（對照表寫 `MLEADER` 也會當成 `MULTILEADER`）
- 圖塊 `INSERT` 預設搬到「圖塊有效名稱」的圖層，不寫在對照表。動態圖塊用 `getpropertyvalue` 的 `EffectiveName`（AutoCAD 2012 以後的內建函數，不是 `vl-*`）。名稱仍是 `*U`、`*D` 這類匿名圖塊時，圖層名為 `ANON_` 加上去掉星號後的名稱（例如 `*U12` → `ANON_U12`）。
- 「其他物件圖層」空白時，沒列在表上的類型（含 `ATTRIB`）留在原圖層。

範圍，預設都偏保守：
- 只處理目前在圖層 0 的實體。可改成指定來源圖層（逗號或空白分隔），或全部圖層。
- 只處理模型空間。勾選「包含配置」才處理紙空間。
- 圖塊定義裡的物件預設不改。勾選之後，定義裡的文字、線、巢狀圖塊都會搬層，每一個插入都會跟著變。沒有配置代碼的圖塊定義物件，在「只處理模型空間」時仍會處理。
- 可加圖層前綴，例如 `A_` 會讓 `PL` 變成 `A_PL`、圖塊 `Door` 變成 `A_Door`。
- 圖層名稱裡的 `<>/\:";?*|,=`、反引號與控制字元會換成 `_`。

新圖層的顏色依建立順序循環 ACI 1 到 9。已經存在的圖層不改顏色、不改鎖、不改線型。CSV 的「新建圖層」是實體搬去的圖層名稱；「顏色」是該圖層目前的 ACI 色（新建的才是 1–9，舊圖層維持原色）。

外觀：實體若是 BYLAYER（沒有顏色群組，或顏色 256）又搬到顏色不同的圖層，看起來會變。預設勾選「保持原外觀」：把 BYLAYER 的顏色與線型改成原圖層的明確值（圖層沒寫顏色時用 7，沒寫線型時用 Continuous）再搬層。已經是明確顏色、或 BYBLOCK（色號 0）的實體不會改。取消這個選項後，BYLAYER 會跟著新圖層的顏色走，方便用顏色檢查分類結果。

鎖定圖層：預設略過其上的實體，並在結果的「鎖定圖層」欄寫 `圖層:略過`。勾選「暫時解鎖」會先解鎖、搬完再鎖回，欄位寫 `圖層:已暫時解鎖`。

「只產生清單，不輸出 DWG」是預覽：仍寫 CSV，不寫 DWG，狀態為「預覽」。沒有實體要搬的檔案狀態是「無變更」，不複製。已存在的輸出 DWG 會略過，除非勾選覆寫。

結果（CSV 為 UTF-8 含 BOM）：
- `logs\layerclass_moves.csv`：全部檔案的明細。欄位為檔案、新建圖層、顏色、物件類型、搬移數量、來源圖層。
- `logs\layerclass\<相對路徑>.csv`：每個 DWG 一份相同欄位的明細。
- `logs\layerclass_results.csv`：每個 DWG 一列，含搬移數量、新建圖層數、鎖定圖層與狀態。`layerclass.log` 末行有合計搬移數。

不在這個模式裡改的東西：外部參考的來源檔、圖塊定義（除非勾選）、沒列在對照表且沒填「其他物件圖層」的類型。`FONTALT` 只設成 `txt.shx` 以避免缺字對話框。存檔用 `QSAVE`，不改 DWG 版本。accoreconsole 會把定位字元展開成空白，所以結果用 `|=|`；解析器仍讀得懂每個欄位都是單字時的空白輸出。

本機確認（Windows、AutoCAD 2027，不是 LT）：
1. `git pull` 這個分支後，選「自動分圖層」。輸入資料夾放一張圖層 0 上什麼都有的 DWG，輸出選另一個資料夾。
2. 先勾「只產生清單」。確認沒有輸出 DWG，`logs\layerclass_moves.csv` 的數量與類型合理。
3. 取消預覽再跑。原始檔時間不變。輸出 DWG 裡聚合線在 `PL`、文字在 `TST`、填充在 `H`、圖塊在以圖塊名稱命名的圖層。新圖層顏色為 1–9，舊圖層顏色沒變。
4. 取消「保持原外觀」再輸出到另一個資料夾，BYLAYER 的線應跟著新圖層變色；勾著時外觀應接近原圖。
5. 放一個鎖住的圖層，沒勾解鎖時該層物件留在原地，結果列出略過的圖層；勾選後物件會搬走，圖層仍是鎖定。

### 桌面捷徑
雙擊 `create_shortcut.bat` 會在桌面建立「DWG分析」捷徑（圖示為 `dwg_analysis.ico`）。
捷徑存的是絕對路徑：**搬移或改名資料夾後，捷徑與圖示都會失效**，在新位置再執行一次 `create_shortcut.bat` 即可。
若 `.venv` 不存在，`app.bat` 會跳出對話框提示安裝步驟（而不是靜默無反應）。

## SCADA 搜尋規則（單一實作：`scripts\scada_rules.py`）
- 英文關鍵字以「完整 token」比對、不分大小寫：`RACK` 命中 `RACK`、`rack`、`RACK-01`、`RACK_01`、`RACK01`、`RACK1`；不命中 `TRACK`、`TRACK01`、`BRACKET`、`RACKS`（關鍵字前面必須是邊界，後面可接邊界或數字，不可接英文字母）。以數字結尾的關鍵字（如 `SB12`）後面不可再接數字：`SB123` 不命中。`PANELBOARD` 不會命中 `PANEL`。
- 工程尺寸尾碼只套用在 `engineering_suffix_keywords`（預設 `TRAY`）：`TRAYX600`、`trayX600`、`TRAYW300`、`TRAYH150`、`TRAY-300`、`TRAY_X600` 命中；`BETRAYAL`、`ENTRYWAY`、`XTRAY` 不命中。
- `_`、`-`、空白、中文字元都視為邊界；含空白的片語（`SCADA PANEL`）中間可為空白、`_` 或 `-`。含中文的關鍵字用子字串比對。
- 信心等級：`high_confidence`（`high_confidence_phrases`：SCADA/CONTROL/PLC/RTU/UPS/DDC/MCC PANEL）、`normal`、`excluded_noise`（`generic_keywords` 中的 PANEL 若落在 `exclude_phrases`：System Panel、Curtain Wall Panel、Curtain Panel、Architectural Panel、Glazed Panel 內）。已被高信心片語涵蓋的單獨 PANEL 不重複列出。三個清單皆可在 `config.json` 調整。
- 同一文字內同一關鍵字重複出現只記一筆。

## 輸出檔（csv）
`file_index / layers / texts（新增 text_quality）/ blocks / block_summary / attribs / dimensions / scada_hits（新增 confidence）/ object_hits / scada_excluded`
- PDF 表格 BOQ 逐列解析（選用 `pdfplumber`，見 `requirements-pdf-tables.txt`）：每列寫入既有 `boq_items`（`pdf_page`、`table_index`、`row_number`、`quantity_text`、`parse_warnings_json`、`tags`、`citation`），位置為 `page:P/table:T/row:R`；表頭只在同一個表內有效，只有「下一頁的第一個表且欄數相同」才視為續表，欄數不同的列不套用而列入 `pdf/boq_skipped.csv`；數量無法解析時保留原文並記警告；重跑會重建該來源的列。設定 `pdf_boq_tables`（`config.json`，預設 `auto`），三種模式：
  - `auto`：缺 pdfplumber 時只略過 BOQ 步驟，一般 PDF 文字解析照常，`pdf_summary` 記 `boq_status = SKIPPED_DEPENDENCY`，檔案狀態仍為 OK（多數 PDF 不是 BOQ，不因選用套件缺少就標成 skipped）。
  - `required`：缺套件時該 PDF 的檔案狀態為 `SKIPPED_DEPENDENCY`（文字證據仍保留）。
  - `off`：完全不做表格 BOQ 解析。
  已知啟發式（Known heuristic，尚未經真實 BOQ 驗證）：續表判斷只看「下一頁第一個表且欄數相同」，欄數相同但其實不是續表的表會被誤接。**僅完成合成資料驗證；真實 BOQ 驗證 NOT TESTED。**
- `hit_source`（`scada_hits` / `object_hits`）：命中的 CAD 元素類型（TEXT / MTEXT / ATTRIB / BLOCK_NAME / LAYER_NAME），欄位附加在最後，原欄位不變。多格式輸出的 `source_type` 維持來源大類 `CAD`；單獨執行 `find_scada.py` 時 `source_type` 仍是舊值（TEXT / MTEXT …）。
- PDF：`pdf_page`（實體頁碼）、`printed_page`（從頁面頁首／頁尾實際讀到的頁碼，讀不到為 NULL，不做推算）、`citation`（例：`PDF p.9 / Printed 附錄C-8 / § 四(十五)`）。
- `project_files`：`revision`（正式版次，未知為 NULL）、`revision_label`（older / newer / order_i_of_n）、`revision_status`（inferred_order / conflicting_order / unknown）、`revision_basis`（filename、mtime、core_modified）。只推論新舊順序，不產生 RevA / RevB；依據互相矛盾時不給標籤。
- `object_hits.csv`：每個 entity 一列，`matched_keywords` 以 `|` 合併（如 `SCADA|TRAY`）；無 handle 時以「檔案+類型+圖層+座標+文字」為 key。
- `block_summary.csv`：依 file、count（大到小）、block_name 排序；`scada_related` 依 block 名稱或圖層名稱是否命中。`blocks.csv` 仍保留每個 instance。
- `scada_excluded.csv`：被判為雜訊而未列入 `scada_hits.csv` 的命中，供人工抽查。
- `text_quality`：`OK` / `EMPTY` / `SUSPECT_ENCODING`（純 `?`、含 U+FFFD、控制字元、或 `?` 緊鄰中日文字）。文字原樣保留，不影響檔案狀態。
- 統計文字：`Keyword Hits`（scada_hits 列數）與 `Unique Matched Objects`（object_hits 列數）為不同數字，均由實際資料計算。

## 批次轉檔機制（單一 accoreconsole 視窗）
整批 DWG 由**同一個** accoreconsole 程序處理（`scripts\convert_batch.py`）：產生一份 script，對每個 DWG 執行 `_OPEN` → `_SAVEAS DXF`，並用 stdout 標記回報每檔進度。
- 遇到損壞的 DWG，AutoCAD 會跳出訊息框並卡住；程式從 stdout 偵測到後，把該檔標為失敗、只終止自己啟動的那個 accoreconsole，再為剩餘檔案重開一個（此時才會多出一個視窗）。
- 每檔以 `DWGNAME` 標記確認實際開啟的是預期的圖，避免開檔失敗時把上一張圖存成這一檔。
- 路徑可直接以雙引號放進 script（含空白、中文皆已實測）；若檔名含 ANSI 碼頁（此機為 cp950）無法表示的字元（例如 emoji），會先複製唯讀副本到暫存資料夾再開啟（此情況下該圖的外部參考相對路徑可能失效）。
- 視窗因輸出被程式接走而只會顯示空白黑底，屬正常。
- `convert_single.py` 仍為每檔獨立一個程序。

## 單檔測試
```
.venv\Scripts\python scripts\convert_single.py input\a.dwg output\dxf\a.dxf
```
結果為 `DXF_VALID` / `DXF_INVALID`（以 ezdxf 實際讀取判定，不只看 return code）。

## 批次執行
```
run.bat
```
流程：env_check -> batch_convert -> analyze_dxf -> find_scada -> summary。
`config.json`：`overwrite`（預設 false，已存在的 DXF 不覆寫）、`recursive`、`keywords`、`timeout_seconds`。

## 輸出說明
- `file_index.csv`、`layers.csv`、`texts.csv`（含 TEXT/MTEXT/ATTRIB）、`blocks.csv`、`attribs.csv`、`dimensions.csv`、`scada_hits.csv`
- `logs/batch_results.csv`：source / output / status / return_code / file_size / elapsed_seconds / error（每檔轉完即寫入）
- `logs/errors.log`：所有失敗紀錄

## 錯誤排除
- `DXF not produced`：看 `output/logs/<name>_<id>.stdout.log`（accoreconsole 輸出為 UTF-16，已解碼）。
- 捷徑沒有圖示或點了沒反應：資料夾被搬移/改名，重新執行 `create_shortcut.bat`。
- 測試：`.venv\Scripts\python -m pytest tests -v`
  - 暫存目錄固定在專案內 `.pytest_tmp\`（見 `pytest.ini`），避免 `%TEMP%\pytest-of-<user>` 權限問題。
  - 需要 AutoCAD 的測試標記為 `accore`；`config.json` 的 `accoreconsole` 路徑不存在時會顯示為 skipped（原因列在結果最後），不算失敗。未安裝 `ifcopenshell` 時 IFC 測試同樣 skip。

## 限制（皆已實測）
- accoreconsole 讀 script 使用系統 ANSI 碼頁；script 內的非 ASCII 字元（例如中文）會亂碼。因此 SAVEAS 目標使用 ASCII 暫存檔名，轉完再由 Python 改名到含中文/空白的最終路徑。
- SAVEAS DXF 的版本沿用「目前檔案格式」（本機為 2018，`AC1032`）；`config.json` 的 `dxf_version` 目前僅為紀錄，未用來切換版本。
- SAVEAS 檔名提示以空白分隔，故暫存 DXF 放在「純 ASCII、無空白」的資料夾（優先 `output\logs\tmp`，否則 8.3 短路徑、`%TEMP%\dwg_batch_tool`、`C:\Users\Public\dwg_batch_tool`），最終路徑由 Python 移動。輸出資料夾含空白時這點才會出現（GUI 測試中發現並修正）。
- DIMENSION 的 `dimtype` 輸出為 DXF 原始整數碼。
- 尚未測試：路徑超長、檔案被其他程式鎖定、無寫入權限（程式有處理與記錄，但未實測）。
- 測試 DWG 為 AutoCAD 2027 自產，尚未用公司實際專案圖驗證。

## 安全規則
此工具不修改原始 DWG。

所有 DWG 使用唯讀方式處理（`/readonly`，並在轉檔前後比對 SHA-256）。

所有衍生檔案輸出至 output 目錄。

## 多格式工程資料分析

現有 `run.bat` 與 CAD「開始處理」保留。GUI 新增「資料分析」區，可選多個檔案或使用輸入資料夾；按「分析工程資料」後由背景 thread 呼叫同一 pipeline，非 DWG 格式不需要 AutoCAD。停止會在目前檔案解析完成後生效；DWG 轉檔可中止本工具啟動的程序。結果檔不存在時按鈕停用。

安裝至專案虛擬環境：

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
# IFC 為選用套件；安裝失敗不影響其他格式
.venv\Scripts\python -m pip install -r requirements-ifc.txt
```

使用 `ezdxf`、`openpyxl`、`pandas`、`PyMuPDF`、`python-docx`；IFC 使用 `ifcopenshell`。`pdfplumber` 只在需要解析 PDF 表格 BOQ 時安裝（選用）：`.venv\Scripts\python -m pip install -r requirements-pdf-tables.txt`，不放進主 `requirements.txt`。套件只在各 parser 執行時載入；缺少套件記為 `SKIPPED_DEPENDENCY` 並寫入錯誤紀錄。IFC 可單獨使用 `pip install ifcopenshell` 補裝。

```powershell
# 混合資料夾
.venv\Scripts\python scripts\pipeline.py input --output output_multi
# 指定多個檔案
.venv\Scripts\python scripts\pipeline.py input\model.ifc input\boq.xlsx --output output_multi
# 明確指定舊版與新版 BOQ；不由檔名推斷 revision
.venv\Scripts\python scripts\analyze_excel.py new.xlsx --compare old.xlsx --output output_compare
# 各 parser 也可單獨執行，使用相同參數格式
.venv\Scripts\python scripts\analyze_pdf.py appendix.pdf --output output_pdf
```

CSV 以 `config.json/excel_aliases` 辨識標頭；保留 sheet、實際 row number、原始資料列，包含非 BOQ 工作表。CSV 逐列讀取，Excel 使用 read-only 工作表迭代；merged cell 的原始錨點及空白位置保留，不把空白猜成數值。數量、單價與金額轉為數值，未知值留空。巨集不執行，公式使用檔案儲存的快取結果，工具不重算公式。

BOQ 比對使用唯一 item/model 組合，缺少時以完整 description 作候選；重複或空 key 標 `MATCH_UNCERTAIN`。支援新增、移除、數量、文字、價格改變及未改變；多種變更以 `|` 保留。指定 `--compare` 時只能有一份新版 BOQ；舊版仍保留於資料庫，但不參與目前 CAD/IFC 對 BOQ 的比對。

### 輸出與來源追溯

```text
output_multi/
  cad/             layers, texts, blocks, attribs, dimensions, file_index,
                   block_summary, scada_hits, object_hits, scada_excluded, json/, dxf/
  ifc/             ifc_objects, ifc_systems, ifc_spaces, ifc_summary
  excel/           excel_sheets, excel_tables, boq_items, boq_summary, boq_compare
  pdf/             pdf_pages, pdf_sections, pdf_hits, pdf_summary
  docx/            docx_sections, docx_tables, requirements
  navisworks/      navis_clashes, navis_summary
  database/        project.db 與八張 normalized table 的 CSV
  cross_reference/ cross_reference.csv
  logs/            errors.log
```

以上表格名稱均輸出 `.csv`。`project.db` 包含 `project_files`、`engineering_objects`、`requirements`、`boq_items`、`documents`、`document_sections`、`clashes`、`cross_reference_results`；`exports` 保存額外 parser 報表的完整 JSON。物件建立 normalized_name、source_type、object_type、system 索引。固定 schema 以外的原始欄位仍保存在 `payload_json`，可還原完整解析紀錄。

每筆資料保留 `source_file`、`source_type`、`source_location`、`evidence_level`。一般解析為 `PARSED`，跨來源及版本比對為 `DERIVED`；不自動產生人工確認，也不改寫 `SOURCE`。檔案索引記錄 SHA-256、UTC 解析時間與狀態；每次成功發布的資料庫代表**此次選定輸入的快照**，不是歷次累加。重跑不重複新增資料列，標準 CSV 無資料時仍輸出表頭；歷次 CAD 轉檔及 JSON 檔會保留，因此本次清單以 `project_files` 為準。

單檔失敗會 rollback 該檔解析資料，繼續其他檔案；`logs/errors.log` 記錄 file、stage、exception type、message。CLI 遇到失敗、缺少 dependency、OCR_REQUIRED 或停止時回傳非零。GUI 顯示 `Completed with warnings`。輸出不可等同或包含選定的來源；輸出位於輸入資料夾內時會自動排除輸出樹，避免重複解析自己的結果。

### 證據式比對與已知範圍

- CAD/IFC 對 BOQ：原字串相同為 `EXACT_MATCH`，名稱正規化相同為 `NORMALIZED_MATCH`（confidence < 1）；`RTU01`、`RTU-01`、`RTU_01` 可比較，`TRACK01` 不會變成 `RACK01`。重複 key 與有界 prefix 候選為 `UNCERTAIN`，不把同一候選另宣稱為確定缺項。IfcSpace、IfcSystem、IfcDistributionSystem、IfcBuildingStorey 為 `REFERENCE_ONLY`，比較輸出 `NOT_APPLICABLE`；設備資格使用 IFC inheritance 與明確類別清單。
- CAD keyword、IFC system 對 requirement 使用既有 `scada_rules.py`，只輸出 `RELATED`／`compliance_status=INSUFFICIENT_EVIDENCE`，不表示需求已滿足。Clash GUID 使用原字串精確比對，不做大小寫或分隔符正規化；handle 必須有明確來源檔案才可確定匹配，無來源或多個候選時為 `UNCERTAIN`。名稱另使用名稱索引。
- PDF 逐頁取得文字、頁碼、段落/標題/表格候選與 keyword excerpt。無文字但有影像的頁面標 `OCR_REQUIRED`，不執行 OCR；空白頁為 `EMPTY_PAGE`。表格欄位僅為候選，尚未重建複雜表格格線。
- DOCX 保留本文 paragraph/table 順序、Heading 層級、list item 及 requirement 原文。表格以 row 為最小需求單位：保留 table/row、column headers、subject、各 cell 原文及合併格原始位置；多個 requirement cell 存於 `requirement_fragments_json`。`requirement_text/original_text` 保留第一個 requirement cell 的原字串，完整列關係在 `row_context_json`，不合成句子冒充原文。所有 requirement 都是 `CANDIDATE`；目前不抽取頁首頁尾或批註，DOCX 未渲染因此不推測頁碼。
- IFC 提取語意物件、GlobalId、Tag、system/container、property sets、關係、local placement 座標及已提供的 Width/Height/Length。來源缺值為 Python None／JSON null／SQL NULL，來源明確給空字串則保留空字串；CSV 空欄無法區分兩者，需查看 DB／payload_json。座標與尺寸沿用 IFC 專案單位，未做幾何網格重建、尺寸推測或跨格式單位換算。多個 property set 的同名尺寸互相衝突時留 NULL，不任取第一值。
- Navisworks 支援 `clashtests/clashresult` XML、具有 canonical 欄位的 Clash CSV，以及明確提供 Clash Name/Status/Item 1/Item 2 欄位的 HTML 表格（別名見 `navisworks_columns`）。任意 XML/HTML 及 `.nwd` 回報 `UNSUPPORTED_FORMAT`；不推測格式、不解析 NWD binary。
- Cross-reference 使用索引與有界候選，仍需記憶體保存待比對物件及 BOQ 索引；未用公司大型專案驗證容量上限。

### 可重現驗證

```powershell
.venv\Scripts\python -m pytest tests -q
# 建立新的合成資料資料夾，執行所有格式及明確 BOQ 版本比對
.venv\Scripts\python scripts\demo_multiformat.py --output demo_new --with-dwg
```

`--with-dwg` 使用原有測試 DWG 及本機 AutoCAD；示範程式拒絕覆寫既有 demo 資料夾。已執行結果見 `demo_multiformat/summary.json`，完整開發驗證紀錄見 `MULTIFORMAT_REPORT.md`。

### 工程語意 acceptance gate

最新驗證見 `SEMANTICS_REPORT.md` 與 `regression_semantics/index.html`。先前 `demo_multiformat/` 及 `reports/acceptance_review/` 保留作為舊版證據，不能拿其舊 result taxonomy 當作目前結果。新輸出在 `regression_semantics/existing_output/`、`regression_semantics/acceptance_output/`。

Cross-reference 的結果分類為：`EXACT_MATCH`、`NORMALIZED_MATCH`、`RELATED`、`MISMATCH`、`MISSING_A`、`MISSING_B`、`UNCERTAIN`、`NOT_APPLICABLE`，不再產生含糊的 `MATCH`。新增 `match_basis`、`compliance_status`、`specification_conflicts_json`、`candidate_count`。舊資料庫採新增欄位 migration，不會回填或猜測舊紀錄的 compliance；需重跑來源至新 output 才有新版語意。

`compliance_status` 可表示 NOT_EVALUATED／SUPPORTED／NOT_SUPPORTED／INSUFFICIENT_EVIDENCE；目前不自動宣告 SUPPORTED 或 NOT_SUPPORTED。一般名稱／識別碼比對為 NOT_EVALUATED，需求關聯為 INSUFFICIENT_EVIDENCE。`confidence` 只適用於 `match_basis`，不是設計符合率。

規格 MISMATCH 僅針對已具名稱對應、兩側明確給出的同屬性／同單位數值衝突。例如 CAD Block 的 `SPECIFICATION` attribute=`Voltage: 220 V`，BOQ Specification=`Voltage: 110 V`。CAD 規格 attribute tags 由 `specification_attribute_tags` 設定；IFC 使用明確的 `Specification` property。支援 Voltage/電壓(V)、Current/電流(A)、Power/功率(W 或 kW)、Width/寬度(mm)，不換算不同單位、不從名稱猜數值，也不由兩份自由文字不同就宣稱規格矛盾。缺資料或不可比較的屬性不建立規格結論。

```powershell
.venv\Scripts\python reports\semantics_gate\run_acceptance.py --output regression_semantics_new
```

目前 Retrieval 開發已依使用者指示暫停：evidence_schema、evidence_builder、structured_query、evidence_search 為尚未整合的開發模組，未接入 pipeline／GUI；router、resolver、Ollama、embedding、Dify/API 均未繼續。尚未宣稱 v2.0 完成。既有 pipeline 仍是本次輸入的資料快照，歷史 evidence 不會由尚未整合的 builder 自動保存。

不使用 ODA。

不使用 COM / ActiveX。

不使用外部 API。

DWG -> DXF 由本機 Autodesk accoreconsole.exe 執行（實測為 AutoCAD 2027，非 LT）。
