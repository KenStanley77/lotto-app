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
| `.github/workflows/update.yml` | 每月定時更新、commit，並由 workflow 直接發佈 Pages |
| `.github/workflows/probe.yml` | 手動觸發，只驗證台彩 API 能否從 GitHub 取數，不寫任何檔案 |

## Pages 設定（重要）

Repo 的 Settings → Pages → **Build and deployment → Source 請選「GitHub Actions」**，
不要選「Deploy from a branch」。

原因：由 workflow 使用 `GITHUB_TOKEN` 推送的 commit **不會觸發**分支式的 Pages 重建，
若用分支模式，網站會一直停在最初那一版。改成 GitHub Actions 模式後，由 `update.yml`
自己封裝並部署，每次執行都一定重新發佈。

`update.yml` 只把 `index.html` 放進 `_site` 再上傳，程式與資料檔不會被公開在網站上。

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