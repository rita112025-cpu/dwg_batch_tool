# mdlinkcheck

`mdlinkcheck` 是只使用 Python 3.10+ 標準函式庫的 Markdown 本地連結檢查工具。它不會發出網路請求。

## 用法

```console
python -m mdlinkcheck <路徑> [<路徑> ...]
```

路徑可為 Markdown 檔案或目錄；目錄會遞迴掃描副檔名為 `.md` 的檔案。相對連結以包含該連結的 Markdown 檔案所在目錄為基準。`http://`、`https://` 與 `mailto:`（不分大小寫）會略過。

錯誤輸出格式如下，行號與欄位皆從 1 開始；欄位指向 `[`，圖片則指向 `!`：

```text
<檔案路徑>:<行號>:<欄位>: <錯誤類型> <目標>
```

錯誤類型只有 `missing-file` 與 `missing-anchor`。無錯誤時結束碼為 0，有錯誤為 1，參數錯誤、路徑不存在或檔案無法讀取時為 2。

## 錨點規則

支援 `file.md#section` 與同檔的 `#section`。ATX 標題（`#` 到 `######`）會依下列規則產生錨點：

1. 轉為小寫。
2. 移除 Unicode 標點，但保留 `-` 與 `_`。
3. 每個空白字元轉為 `-`。
4. 同名標題依出現順序加上 `-1`、`-2` 等後綴。

路徑中的 `%20` 等百分比編碼會先解碼，含空白的路徑也可直接使用。

## 測試

```console
python -m unittest discover -s tests -v
python -m mdlinkcheck tests/fixtures
```

## 已知限制

- 僅解析單行的 inline link/image 語法：`[文字](目標)` 與 `![替代文字](目標)`。
- 不解析 reference-style links、autolinks、HTML links 或跨行連結。
- 不解析連結 title；括號內內容會整體視為目標，目標本身也不能含未跳脫的 `)`。
- 錨點來源僅支援 ATX 標題，不支援 Setext 標題；標題內的 Markdown 格式不會額外正規化。
- fenced code block 支援反引號與波浪號；行內 code 支援同一行內成對且等長的反引號 delimiter。
