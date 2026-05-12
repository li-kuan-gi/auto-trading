# Alpaca + FMP Swing Trader Guard

這是一個「每小時檢查一次，但不會每小時亂下單」的 Alpaca paper trading 範例。

目前版本的事件避險方式：

1. **FMP Earnings Calendar**：避開個股財報日前後。
2. **手動 blackout CSV**：先手動填 CPI、FOMC、NFP 等總經事件，避免因 FMP economic calendar 權限不足而失敗。
3. **最多一個持倉**：只要帳戶已有任何 open position，就不會開新倉。
4. **已有 open order 也不開新倉**：避免 GitHub Actions 每小時重複送單。
5. **風險定義下單**：用 `帳戶 equity × RISK_FRACTION ÷ 每股風險` 計算 qty。
6. **Bracket order**：進場時同時帶 take-profit 與 stop-loss。

> 預設 `ENABLE_TRADING=false`，只會 dry-run，不會真的送單。

---

## 1. GitHub Secrets

到 GitHub repo：

`Settings → Secrets and variables → Actions → Secrets`

新增：

```text
ALPACA_API_KEY
ALPACA_SECRET_KEY
FMP_API_KEY
```

先用 Alpaca paper trading key。

---

## 2. GitHub Variables

到：

`Settings → Secrets and variables → Actions → Variables`

建議先放：

```text
ENABLE_TRADING=false
PAPER=true
WATCHLIST=SPY
STRATEGY=disabled
DATA_FEED=iex
```

測試時不要急著開交易。先手動跑 workflow 看 log。

---

## 3. 每小時排程

workflow 使用：

```yaml
- cron: "17 * * * *"
```

GitHub Actions 的 schedule 是 UTC。選 17 分是為了避開整點高峰。

---

## 4. 怎麼開始測試

### 4.1 本機測試

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 編輯 .env，填 key

python src/swing_trader.py
```

### 4.2 GitHub Actions 手動測試

到 GitHub repo 的 Actions 頁面，選 `Alpaca FMP Swing Trader`，按 `Run workflow`。

---

## 5. 預設不會開倉

`STRATEGY=disabled` 時永遠不開新倉，只做：

- 帳戶檢查
- 市場時鐘檢查
- positions / open orders 檢查
- FMP earnings calendar 檢查
- manual blackout 檢查

若要測試完整下單流程，可以先設：

```text
ENABLE_TRADING=false
STRATEGY=manual_once
```

這會產生 dry-run order payload，但不送出。

確認 payload 合理後，才考慮：

```text
ENABLE_TRADING=true
STRATEGY=manual_once
```

---

## 6. 策略選項

### disabled

不開倉。最安全。

```text
STRATEGY=disabled
```

### manual_once

只要通過 guards，就產生一筆 bracket order。適合 paper 測試下單流程。

```text
STRATEGY=manual_once
```

### sma_trend

範例策略：使用日線資料，條件大致是：

- fast SMA > slow SMA
- 最新收盤價 > fast SMA
- 前一根收盤價 <= 前一根 fast SMA

這只是範例，不代表可獲利。

```text
STRATEGY=sma_trend
SMA_FAST=20
SMA_SLOW=50
```

---

## 7. 事件避險

### 7.1 FMP 財報日曆

預設避開財報日前 1 天到後 1 天：

```text
EARNINGS_BLOCK_DAYS_BEFORE=1
EARNINGS_BLOCK_DAYS_AFTER=1
```

若 FMP earnings calendar 也被限制，程式會印出 warning，然後只用 manual blackout，不會讓整個 workflow 失敗。

### 7.2 手動總經事件

編輯：

```text
config/manual_blackout_events.csv
```

格式：

```csv
start_utc,end_utc,reason,symbols
2026-05-13T12:00:00Z,2026-05-13T15:00:00Z,US CPI,*
```

`symbols`：

- `*`：全部標的都避開
- `"SPY,QQQ"`：只避開這些 symbols（CSV 內有逗號時要加雙引號）

---

## 8. 重要限制

這不是投資建議，也不是完整策略。

你目前的 FMP 權限若無法查 economic calendar，這版只能做到：

- 財報事件避開
- 你手動輸入的總經事件避開
- 交易時段/持倉/重複下單防呆
- 風險定義的 bracket order

若你之後取得可用的 economic calendar API，再把 `get_macro_events()` 接上即可。
