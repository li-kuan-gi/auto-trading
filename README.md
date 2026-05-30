# Alpaca + FMP Swing Trader Guard

這是一個「被觸發時檢查一次，通過 guards 才可能下單」的 Alpaca paper trading 範例。

排程不由程式碼或 GitHub Actions `schedule` 處理。若要定時執行，請用外部服務呼叫 GitHub Actions workflow dispatch API；也可以自己到 GitHub Actions 頁面手動觸發。

目前版本的事件避險方式：

1. **FMP Earnings Calendar**：避開個股財報日前後。
2. **手動 blackout CSV**：先手動填 CPI、FOMC、NFP 等總經事件，避免因 FMP economic calendar 權限不足而失敗。
3. **最多一個持倉**：只要帳戶已有任何 open position，就不會開新倉。
4. **已有 open order 也不開新倉**：避免外部服務或手動重複觸發時重複送單。
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
SYMBOL_SELECTION_METHOD=best_signal
DATA_FEED=iex
```

測試時不要急著開交易。先手動跑 workflow 看 log。

---

## 3. 觸發方式

workflow 只保留手動觸發：

```yaml
on:
  workflow_dispatch:
```

這個 repo 不依賴 GitHub Actions `schedule`，也不在 `src/swing_trader.py` 裡做長駐輪詢。

### 3.1 手動觸發

到 GitHub repo 的 Actions 頁面，選 `Alpaca FMP Swing Trader`，按 `Run workflow`。

### 3.2 外部服務呼叫 API

外部排程服務只需要在你想檢查交易的時間呼叫 GitHub workflow dispatch API。Token 請放在外部服務的 secret / credential manager，不要寫進 repo。

```bash
curl -L \
  -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <YOUR_GITHUB_TOKEN>" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  https://api.github.com/repos/li-kuan-gi/auto-trading/actions/workflows/alpaca-fmp-swing-trader.yml/dispatches \
  -d '{"ref":"main","return_run_details":true}'
```

fine-grained token 需要這個 repo 的 `Actions: write` 權限。GitHub 文件：<https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event>

也可以用 GitHub CLI：

```bash
gh workflow run alpaca-fmp-swing-trader.yml --ref main
```

### 3.3 Cloudflare Workers Cron

這個 repo 已附一個 Cloudflare Worker：

```text
cloudflare/worker.js
cloudflare/wrangler.toml
```

這個 Worker 是 scheduled-only，`workers_dev = false`，不需要公開 `*.workers.dev` URL。

預設排程在 `cloudflare/wrangler.toml`：

```toml
crons = [ "*/15 13-22 * * mon-fri" ]
```

Cloudflare Cron Triggers 使用 UTC。這個設定會在週一到週五、13:00-22:59 UTC 每 15 分鐘觸發一次，涵蓋美股開盤附近的夏令/冬令時間差；實際是否交易仍由 GitHub workflow 裡的 Alpaca market clock 檢查決定。

Cloudflare 的 weekday 數字是 `1 = Sunday` 到 `7 = Saturday`，和部分 crontab 不同。為了避免誤解，這裡刻意使用 `mon-fri`，不要改成 `1-5`。

若你想降低 GitHub Actions 次數，可以改成例如每 30 分鐘：

```toml
crons = [ "*/30 13-22 * * mon-fri" ]
```

部署步驟：

1. 到 GitHub 建立 fine-grained personal access token，repo 選 `li-kuan-gi/auto-trading`，權限給 `Actions: Read and write`。
2. 登入 Cloudflare：

```bash
cd cloudflare
npx wrangler@latest login
```

3. 把 GitHub token 存成 Cloudflare Worker secret：

```bash
npx wrangler@latest secret put GITHUB_TOKEN
```

4. 部署 Worker 與 Cron Trigger：

```bash
npx wrangler@latest deploy
```

5. 端到端測試 scheduled handler：

```bash
npx wrangler@latest dev --remote --test-scheduled
curl "http://localhost:8787/__scheduled"
```

這會使用 Cloudflare 上的 Worker secret。若改成本機模式測試，才需要用 `.dev.vars` 放本機 secret；`.dev.vars` 已放進 `.gitignore`，不要 commit。

Cloudflare 免費方案目前有 Workers 100,000 requests/day，Cron Triggers 免費帳號最多 5 個；這個設定只用 1 個 trigger。Cloudflare 官方頁面也標示可以免費開始且不需要信用卡：<https://www.cloudflare.com/plans/>

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

### 4.2 本機回測

回測目前支援 `sma_trend` 策略，使用 Alpaca 日線資料。它不會送單，也不需要 FMP key；只需要 Alpaca market data 權限。

```bash
python src/backtest.py \
  --start 2025-01-01 \
  --end 2025-12-31 \
  --watchlist SPY,AAPL \
  --initial-equity 10000
```

也可以用 `.env`：

```text
WATCHLIST=SPY,AAPL
DATA_FEED=iex
BACKTEST_START=2025-01-01
BACKTEST_END=2025-12-31
BACKTEST_INITIAL_EQUITY=10000
RISK_FRACTION=0.01
REWARD_RISK_RATIO=2.0
STOP_LOSS_PCT=0.05
SMA_FAST=20
SMA_SLOW=50
```

回測假設：

- 用收盤後確認的 SMA 訊號，下一個交易日開盤進場。
- 每次最多一個持倉，和實盤 guard 一致。
- 每筆交易用 `equity × RISK_FRACTION ÷ 每股風險` 算張數。
- 日線內若 stop-loss 和 take-profit 同時碰到，保守視為 stop-loss 先成交。
- 趨勢反轉出場使用當日收盤價，stop-loss / take-profit 使用設定價。

### 4.3 GitHub Actions 手動測試

照「3.1 手動觸發」執行 workflow。

### 4.4 GitHub Actions 回測

如果 Alpaca credentials 已經放在 GitHub Secrets，可以直接到 Actions 頁面選 `Alpaca Strategy Backtest`，按 `Run workflow`，填：

```text
start=2025-01-01
end=2025-12-31
watchlist=SPY,AAPL
initial_equity=10000
output_json=false
```

這個 workflow 只跑回測，不會送單，也不需要 `FMP_API_KEY`。它會使用：

- Secrets: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`
- Variables: `PAPER`, `DATA_FEED`, `RISK_FRACTION`, `REWARD_RISK_RATIO`, `STOP_LOSS_PCT`, `ALLOW_FRACTIONAL`, `SMA_FAST`, `SMA_SLOW`, `LOG_LEVEL`

### 4.5 GitHub Actions 參數最佳化

到 Actions 頁面選 `Alpaca Backtest Optimize`，按 `Run workflow`。預設會用目前科技股 watchlist，測這些組合：

```text
SMA_FAST: 10,20,30
SMA_SLOW: 50,100,150,200
STOP_LOSS_PCT: 0.02,0.03,0.05,0.08
REWARD_RISK_RATIO: 1.5,2.0,2.5,3.0
RISK_FRACTION: 0.001
```

本機也可以跑：

```bash
python src/backtest_optimize.py \
  --start 2025-01-01 \
  --end 2025-12-31 \
  --watchlist QQQ,SMH,NVDA,AVGO,AMD,MU,MSFT,GOOGL,META,AMZN,ORCL,SPY
```

排名表會顯示：

```text
rank sma stop rr risk final return max_dd trades win_rate profit_factor score
```

`score` 是排序輔助值，偏好較高 profit factor / 報酬，並懲罰較高 drawdown；不要只看第一名，應挑交易數足夠、跨年份仍穩定的組合。

### 4.6 GitHub Actions 1H swing 回測

到 Actions 頁面選 `Alpaca Intraday Swing Backtest`，按 `Run workflow`。預設策略是：

```text
daily trend filter: daily SMA 30 > SMA 100, close > SMA 30
market filter: QQQ daily SMA 30 > SMA 100
entry trigger: 1Hour close crosses above 1Hour SMA 20
stop_loss_pct=0.03
reward_risk_ratio=2.5
risk_fraction=0.001
```

它會額外輸出 day trade 壓力：

```text
day_trades
max_day_trades_in_5_business_days
same_day_exit_rate
avg_holding_hours
median_holding_hours
exit_reasons
```

這個回測先不禁止 same-day exit；目標是看 1H swing 策略自然會產生多少同日進出，再決定是否需要 2H、minimum holding period 或 day-trade 上限。

### 4.7 GitHub Actions 1H swing 參數最佳化

到 Actions 頁面選 `Alpaca Intraday Swing Optimize`，按 `Run workflow`。預設會測：

```text
daily_fast_values=20,30
daily_slow_values=100,150
intraday_sma_values=10,20,30
breakout_lookback_values=0,3,5
stop_loss_values=0.02,0.03,0.05
reward_risk_values=1.5,2.0,2.5,3.0
risk_fraction_values=0.001
```

排名會把 day trade 壓力納入懲罰；`max_day_trades` 預設是 `3`，也就是 `max_day_trades_in_5_business_days` 超過 3 的組合會被扣分。

---

## 5. 預設不會開倉

`STRATEGY=disabled` 時永遠不開新倉，只做：

- 帳戶檢查
- 市場時鐘檢查
- positions / open orders 檢查
- FMP earnings calendar 檢查
- manual blackout 檢查

如果還沒有任何持倉，也沒有 open order，程式會從 `WATCHLIST` 依策略分數選出交易標的。範例：

```text
WATCHLIST=AAPL,SPY
SYMBOL_SELECTION_METHOD=best_signal
```

`best_signal` 會檢查所有候選標的，排除不可交易、被事件 blackout、或策略沒有 signal 的標的，再選策略分數最高者。`sma_trend` 的分數目前由趨勢強度與突破強度組成，log 會輸出 `SYMBOL_CANDIDATE`、`SYMBOL_RANKING` 與 `SELECTED_SYMBOL` 方便檢查。若改成 `first_signal`，才會照 `WATCHLIST` 順序選第一個符合條件的標的。

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
2026-06-10T12:00:00Z,2026-06-10T15:00:00Z,US CPI,*
2026-06-17T17:30:00Z,2026-06-17T20:30:00Z,FOMC Rate Decision,*
```

`symbols`：

- `*`：全部標的都避開
- `"SPY,QQQ"`：只避開這些 symbols（CSV 內有逗號時要加雙引號）

目前已先預填 2026-05-28 到 2026-12-23 的常見美國總經事件，包括 CPI、PPI、Employment Situation / NFP、FOMC、GDP / PCE。時間一律寫 UTC：

- 08:30 ET 發布的資料，預設 blackout 為發布前 30 分鐘到發布後 2.5 小時。
- FOMC 預設覆蓋 14:00 ET statement 與 14:30 ET press conference。

這份 CSV 是手動維護的 guardrail。上線交易前，請依官方行事曆核對最新日期與時間：

- BLS release calendar: <https://www.bls.gov/schedule/2026/home.htm>
- Federal Reserve FOMC calendar: <https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm>
- BEA release schedule: <https://www.bea.gov/news/schedule/full>

---

## 8. 重要限制

這不是投資建議，也不是完整策略。

你目前的 FMP 權限若無法查 economic calendar，這版只能做到：

- 財報事件避開
- 你手動輸入的總經事件避開
- 交易時段/持倉/重複下單防呆
- 風險定義的 bracket order

若你之後取得可用的 economic calendar API，再把 `get_macro_events()` 接上即可。
