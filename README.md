# 大樂透三年大數據選號器（每月自動更新版）

單一 HTML 檔的樂透選號器，資料內嵌在檔案裡，因此可以直接用 GitHub Pages 發佈成網址；
每到開獎月份，GitHub Actions 會自動跑 `update_lotto.py` 抓台彩官方資料、改寫 HTML 並 commit，
Pages 隨後自動重新發佈。手機打開網址就是最新資料。

## 檔案說明

| 檔案 | 用途 |
| --- | --- |
| `index.html` | 選號器本體，資料內嵌於 `const DRAWS = [...];` 這一行 |
| `update_lotto.py` | 更新引擎：抓官方 API、去重合併、改寫 HTML、更新封存檔 |
| `data/lotto_history.json` | 完整歷史封存（目前 457 期） |
| `data/last_run.json` | 上一次執行結果（是否有變更、期數、時間） |
| `.github/workflows/update.yml` | 每月定時更新並 commit |
| `.github/workflows/probe.yml` | 手動觸發，只驗證台彩 API 能否從 GitHub 取數，不寫任何檔案 |

## 手動測試

```bash
python update_lotto.py --check-only --verbose   # 只抓資料、不寫檔
python update_lotto.py --verbose                # 真的更新
```

在 Actions 頁面選「驗證台彩 API 是否可連線」→ Run workflow，即可確認境外機器能否取數。

## 排程時間

`update.yml` 的 cron 為 `17 1 2 * *`，即每月 2 日 UTC 01:17（台灣時間 09:17）。
GitHub 的定時任務採 UTC，且在尖峰時段可能延遲數十分鐘，偶爾會跳過，屬平台特性。

## 資料來源

台灣彩券官方 API：
`https://api.taiwanlottery.com/TLCAPIWeB/Lottery/Lotto649Result?month=YYYY-MM&pageNum=1&pageSize=200`

`drawNumberSize` 前 6 個為升冪主號，第 7 個為特別號。

## 注意

本程式只做歷史分布的描述性統計，**不預測中獎號碼**。每組號碼中獎機率相同，
大樂透頭獎機率為 1 / 13,983,816。