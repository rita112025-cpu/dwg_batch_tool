# CAD → Revit 中間資料介面（Phase 1）

這個階段只產生「之後可以放置」的資料。它不在 Revit 裡建模，不產生 Dynamo 圖，也不寫 Revit 附加元件。放置步驟（既有的 MEP Tray Importer 或之後的 Dynamo 腳本）讀這裡的 JSON / CSV 即可，不必再解析 DXF。

座標保持圖面單位，不做原點平移。`$INSUNITS`、`$INSBASE`、`$MEASUREMENT`、`$LUNITS` 原樣放進每張圖的 header，供之後換算。

## 執行

在儲存庫根目錄：

```
python -m cad2revit validate-mapping cad2revit/examples/mapping.example.json
python -m cad2revit run --input drawings --output model_data --mapping cad2revit/examples/mapping.example.json
```

DWG 才需要本機的 `accoreconsole.exe`：

```
python -m cad2revit run --input drawings --output model_data --mapping mapping.json --accore C:\path\to\accoreconsole.exe
```

DXF 直接讀取，不需要 AutoCAD。`--recursive` 預設開啟，`--no-recursive` 只看該層。結束碼：`0` 全部成功（未對上的圖層／圖塊仍是 0，只會印出來）、`1` 有檔案失敗、`2` 對照表或資料夾不合法。

單一檔案時，以其父資料夾當作輸入資料夾，一樣做下面的雙向檢查。

## 原始檔保護

- 不修改、不搬移、不改名、不覆蓋、不刪除來源 DWG／DXF。DWG 先整檔複製到暫存目錄，SHA-256 用儲存庫既有的 `convert_single.sha256` / `copy_hashed` 計算；只有複本會交給 `convert_single.convert`（accoreconsole `/readonly`）。處理前後各算一次來源雜湊，不一致就標 `SOURCE_MODIFIED`、不發布元件，並刪除該張已產生的 DXF。
- 輸入與輸出不可相同、輸出不可在輸入裡面、輸入也不可在輸出裡面。比較前會 `resolve`（含 `..` 與符號連結）。
- 任一輸出路徑解析後若等於任一來源檔，或落在輸入資料夾裡，整批拒絕、不寫入。
- 寫進結果的路徑只有相對於輸入資料夾的檔名，沒有絕對路徑。

共用的 `fontfix.directory_problem` 能用時會一併呼叫。這個套件自己的檢查在共用函式放行時仍然拒絕「輸入在輸出裡面」。

## 對照表 `cad2revit.mapping.v1`

機器可讀的 schema：`cad2revit/schema/mapping.schema.json`。範例（合成名稱，不是任何專案）：`cad2revit/examples/mapping.example.json`。

| 欄位 | 說明 |
| --- | --- |
| `schema` | 固定 `cad2revit.mapping.v1` |
| `layer_rules` | 圖層規則，順序即優先順序 |
| `block_rules` | 圖塊規則，順序即優先順序 |
| `defaults` | 分類名稱 → 預設尺寸。規則沒寫的尺寸才用這裡的值 |

每條規則：

| 欄位 | 圖層規則 | 圖塊規則 |
| --- | --- | --- |
| `id` | 必填，英數開頭，可含 `_` `.` `-` | 同左，且不可與其他規則重複 |
| `pattern` | 必填。Python 正規表示式，對整段名稱做 `fullmatch`。要忽略大小寫請寫 `(?i)` | 同左 |
| `category` | 必填。Revit 分類名稱，原樣輸出，不限定在固定清單 | 同左 |
| `family` / `type` | 可省略 | 必填 |
| `parameters` | 選用。`width`、`height`、`diameter` 為大於 0 的數字；有尺寸就必須有 `unit` | 同左 |

`unit` 只接受 `mm`、`cm`、`m`、`in`、`ft`。這是參數的單位，不是圖面單位。

`Cable Tray` 要有 `width` 與 `height`，`Conduit` 要有 `diameter`，否則該元件 `parameters.complete` 為 false。其他分類沒有強制尺寸。未知欄位會使整份對照表失效。

## 分類規則

只讀 **Model** 空間。紙空間有圖元時，圖名列在 `layouts.skipped_with_entities`，不輸出成元件。圖塊定義裡的線不會拆開；只有 `INSERT` 是一筆設備。

| 來源 | 對照 | 幾何 |
| --- | --- | --- |
| `LINE` | 圖層規則，第一條符合的 | `kind=linear`，起點／終點。Z 即高程；兩端 Z 相同時另給 `elevation` |
| `LWPOLYLINE`、`POLYLINE` | 圖層規則 | 每一段一筆。凸度（bulge）不為 0 時 `kind=bulge`，否則 `linear`。2D 聚合線的 Z 用圖元高程，不用頂點裡的 0 |
| `ARC` | 圖層規則 | `kind=arc`，起點、終點、圓心、半徑、起迄角（度） |
| `INSERT` | **只**用圖塊規則，不退回圖層 | `kind=point`，插入點、旋轉（度）、縮放、屬性 |
| `TEXT` / `MTEXT` | 不放置 | 放在 `annotations`：文字、插入點、旋轉（度）、字高 |

圖層規則對不上的線段，以及圖塊規則對不上的 `INSERT`，不進 `elements`，改列在 `unclassified`，並彙總到 `unmatched`。`CIRCLE` 等其他型別只出現在 `skipped_entity_types` 的計數。

寬度只有在「該段起迄寬度相同且大於 0，或只有常數寬度」而且 `$INSUNITS` 是已知長度時，才換算成參數的 `unit` 並標 `origin=entity`。規則若已寫死 `width`，不以圖元覆蓋。高程中心線推不出高度或管徑，所以 `height` 與 `diameter` 永遠來自規則或 `defaults`。`$INSUNITS` 為 0（無單位）或無法換算時，保留對照表的預設值，原始寬度仍放在 `derived_width`（圖面單位）。

## 輸出

| 路徑 | 內容 |
| --- | --- |
| `json/<相對路徑>.cad2revit.json` | 單張圖的完整文件，schema `cad2revit.elements.v1` |
| `csv/<相對路徑>.elements.csv` | 該圖已分類元件 |
| `csv/elements.csv` | 所有已分類元件 |
| `csv/manifest.csv` | 每張圖一列，含 SHA-256、工具版本、單位、插入基點 |
| `csv/unmatched.csv` | 未對上的圖層與圖塊 |
| `batch_summary.json` | 整批索引，schema `cad2revit.batch.v1` |
| `dxf/<相對路徑>.dxf` | 只有 DWG 轉換成功才留下 |

JSON schema：`cad2revit/schema/elements.schema.json`。CSV 為 UTF-8（含 BOM），方便試算表開啟。

每份 JSON 的重點欄位：

- `tool`、`tool_version`、`generated_at`（UTC）
- `source.relative_path`、`source.sha256`、`source.sha256_after`、`source.unchanged`
- `drawing.insunits`、`drawing.insunits_name`、`drawing.insbase`、`drawing.measurement`、`drawing.lunits`、`drawing.dxf_version`
- `mapping.file_name`、`mapping.sha256`（沒有對照表的目錄）
- `elements[]`：`element_id`（來源 SHA-256 前 12 碼 + handle + 段號）、`source_handle`、`layer`、`entity_type`、`rule_id`、`revit_category`、`revit_family`、`revit_type`、`geometry`、`parameters`、`attributes`
- `unmatched.layers[]` / `unmatched.blocks[]`：`name`、`entity_count`

`element_id` 在同一檔案內容不變時固定，方便之後的放置步驟對帳。

CSV 一列一個已分類元件。線性元件填 `x1,y1,z1,x2,y2,z2`；圓弧另有圓心、半徑與角度；設備填 `insert_*`、`rotation_deg`、`scale_*`。每一列都重複 `source_sha256`、`tool_version`、`units_code`、`units_name` 與插入基點，所以只拿 CSV 也能對回來源與單位。屬性欄是 JSON 陣列字串。

DWG 轉換的 `conversion.real_accoreconsole_verified` 在這個階段恆為 `false`。回傳碼或測試替身不能當成真實 AutoCAD 已經跑過。

## 未驗證

真實 `accoreconsole.exe` 把 DWG 轉成 DXF（含 AutoCAD 2027、Revit 2025、Dynamo 3.3.2）沒有在這個套件的自動測試裡執行。測試使用程式產生的 DXF，以及一個只負責寫出 DXF 的轉換替身，用來確認複本、雜湊與「不碰來源」。
