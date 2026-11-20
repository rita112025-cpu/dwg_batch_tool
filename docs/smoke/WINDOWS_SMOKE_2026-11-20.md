# Windows 實機冒煙測試 — 2026-11-20

## 環境
| 項目 | 值 |
| --- | --- |
| Commit | `23dd9bb`（含 PR #22、#23） |
| AutoCAD | AutoCAD 2027，`accoreconsole.exe` 26.0.118.0.0 |
| Windows ANSI code page | 950（繁中 Big5） |
| Python / ezdxf | 3.12.10 / 1.4.4 |
| 腳本 | [smoke_windows.py](smoke_windows.py)（`python -I smoke_windows.py <工作資料夾> [案例...]`） |

## 輸入檔（從 `D:\drawings\input` 複製，原檔未動）
| 代稱 | 原檔 | 大小 | sha256 前 16 碼 |
| --- | --- | --- | --- |
| SMALL | `DN-AR-SB12-0000_v29-圖紙 - AR 2021 - 夾層(2F)-1150903-20261119.dwg` | 330,560 B | `de37b430cec0cbcc` |
| LARGE | `DN-AR-SB12-0000_v29-圖紙 - AR 2031 - 穿堂-月台層(3F)-1150903-20261119.dwg` | 1,588,416 B | `260d7696b6d869d5` |

## 結果
| 案例 | 路徑 / 設定 | 走的程式路徑 | 結果 | 來源 hash | 結束後殘留程序 |
| --- | --- | --- | --- | --- | --- |
| A 正常 | ASCII 路徑，2 檔 | `batch_convert.run`（單一 accoreconsole） | 2/2 `DXF_VALID`（13.75 MB、2.28 MB），約 4 秒 | 不變 | 無 |
| B 繁中 | `車站圖面\<原繁中檔名>`，2 檔 | 同上；cp950 可編碼，直接寫進 `.scr` | 2/2 `DXF_VALID`，DXF 大小與 A 相同 | 不變 | 無 |
| C 日文 | `C_ja_日本語パス\図面フォルダ\図面_テスト_夾層.dwg`、`ずめん_大.dwg` | 同上；`図` 無法以 cp950 編碼，改走「複製到 ASCII 暫存再轉」 | 2/2 `DXF_VALID`，輸出檔名保留日文，大小與 A 相同 | 不變 | 無 |
| D 批次逾時 | `timeout_seconds=0.3`，2 檔 | 批次：逾時後終止並為下一檔重啟 | 2/2 `DXF_INVALID: timeout after 0.3s`，重啟流程正常 | 不變 | 無 |
| E 單檔逾時 | `timeout_seconds=2`，LARGE | `convert_single.convert` | `DXF_INVALID: timeout after 2s`（2.08 秒） | 不變 | 無 |

- 每次 accoreconsole 會帶出子程序 `upi.exe` 與 `conhost.exe`；所有案例結束 2 秒後，三者都已不在（另以 `tasklist` 再確認一次）。
- 暫存的 `.scr`、`.dxf`、複製的 `.dwg` 在所有案例結束後都已清掉。
- `timeout_seconds=2` 的批次案例沒有觸發逾時：批次的逾時是「多久沒有進度」，LARGE 在 2 秒內就轉完，所以改用 0.3 秒強制觸發。

## 發現的問題（已修正，見下節）
**重轉失敗時，舊 DXF 會留在輸出資料夾，被後續分析當成新結果。**
案例 D 在同一個輸出資料夾先成功轉過一次（11:23:16 產生 `t_large.dxf`），再以 `overwrite=true` 重轉且逾時失敗（11:23:40），舊的 `t_large.dxf` 仍在。`analyze_dxf.py` 用 `rglob("*.dxf")` 掃整個資料夾，不看 `batch_results` 的狀態，所以會分析舊檔、不會提示。

## 修正與重測（同日）
- 保留上次成功的 DXF：新 DXF 先移到輸出旁的 `.part` 暫存檔、驗證通過才替換正式檔；驗證失敗就丟棄，舊檔不動（`convert_single.install_dxf`，批次與單檔共用）。
- 重轉失敗且舊檔仍在時，結果的 `error` 會加上「重轉失敗，既有 DXF 為上次結果」。
- `pipeline.run_pipeline` 轉檔後只把本次 `DXF_VALID`／`SKIPPED_EXISTS` 的路徑交給 `analyze_dxf.run` 和 `find_scada.run`（新參數 `files=`）；本次沒有任何成功時，記錄「本次沒有成功轉出的 DXF，略過分析與 SCADA 搜尋」並跳過。只勾分析、不勾轉檔時仍掃整個資料夾。
- `run.bat`（README 記載的主要入口）：`batch_convert.py` 開始時先清掉 `logs/converted_this_run.json`，完成後寫入本次成功清單；`analyze_dxf.py`、`find_scada.py` 以 `--this-run` 只讀該清單，清單為空就印出原因並略過，清單不存在（未完成的轉檔）回傳 3。
- 停止：`run_pipeline` 在轉檔被停止時直接返回，不會接續分析（程式層確認；GUI 的「停止」按鈕尚未實機補測）。
- 回歸測試 [tests/test_stale_dxf.py](../../tests/test_stale_dxf.py)（12 個，不需 AutoCAD，修正前全部失敗）：含案例 D 的批次與單檔版本、一成一敗的受控結果、全部失敗跳過分析、只分析時掃整個資料夾，以及 `run.bat` 路徑的 4 個（只讀本次清單、全部失敗略過、沒有轉檔紀錄回傳 3、中斷的轉檔不留下舊清單）。
- 全套 `pytest`：516 passed（原 504 + 12）。
- 冒煙新增案例 F：依 `run.bat` 的順序呼叫 `batch_convert.main` → `analyze_dxf.main --this-run` → `find_scada.main --this-run`，兩階段（正常 → 0.3 秒逾時重轉），驗收回傳碼 0/0/0 → 1/0/0、舊 DXF 雜湊不變、分析未被呼叫。
- 冒煙腳本改版：每個案例先清空自己的資料夾；D 改為同次執行的兩階段（先正常轉檔記 DXF 的 SHA-256，再以 0.3 秒逾時重轉），驗收「重轉回報失敗、訊息含保留說明、舊 DXF 雜湊不變、自動分析未被呼叫」；任一檢查失敗回傳 1、程序查詢失敗回傳 2；殘留統計排除測試前已存在的 accoreconsole 程序樹。重跑結果 A–F 全部 PASS，exit 0。

## 摘要與 GUI 停止（同日第二輪）
- `summary.py --this-run`（`run.bat` 最後一步）：本次沒有成功轉出的 DXF 時，印出「本次未執行分析（本次沒有成功轉出的 DXF）；csv/、json/ 仍是上次的結果，不列入本次摘要」，不再列出舊 CSV 的筆數；上次的 CSV／JSON 保留不動。案例 F 第二階段驗收：摘要含該句、不含 `file_index.csv:`，`csv/`、`json/` 全部檔案雜湊與第一階段相同。第一階段摘要列出本次筆數（`file_index.csv: 2 rows`）。
- 案例 G（GUI 停止）：開啟真正的 `App` 視窗，指向已有 8 份舊 DXF 的輸出資料夾，勾選覆寫、分析、SCADA，按「開始處理 DWG」，在第一個 DWG→DXF 進度出現時按「停止」（以 `invoke()` 觸發按鈕，不是滑鼠點擊）。結果：分析與 SCADA 搜尋都沒有啟動；本次沒有任何檔案被重轉，8 份舊 DXF 雜湊全部不變；沒有錯誤對話框；視窗顯示「⚠ 警告：已停止（結果不完整）」；沒有殘留程序。
- 全套 `pytest`：518 passed（原 504 + 14）。冒煙 A–G 全部 PASS，exit 0。

## 已知限制（未在此修正）
- 部分成功時，失敗那張的舊 `json/<名稱>.json` 仍在 `json/`（`csv/` 會依本次結果重寫）。
- GUI 停止後，結果區的「DXF：0」是因為停止時不統計資料夾裡的 DXF，不代表舊 DXF 被刪除。

## 未涵蓋
- 只測了兩張 SB12 AR 圖；沒有損壞檔、Xref、含 proxy 物件的大型 MEP 圖。
- 沒有測 GUI（`app.py`）的「停止」按鈕；只測了程式層的逾時。
- 監看子程序的方式是每約 1 秒輪詢一次，可能漏掉存活不到 1 秒的程序；結束後的殘留檢查不受影響。
- ANSI code page 只測了 950；日文系統（932）上的行為沒有測。
