import json
import os

import query_alpaca_rest as q


def main() -> None:
    env = "PAPER" if q.PAPER else "LIVE"

    positions = q.get_positions()
    orders = q.get_open_orders()

    lines = []
    lines.append(f"## Alpaca Positions & Orders ({env})")
    lines.append(f"Base URL: `{q.BASE_URL}`")
    lines.append("")

    lines.append(f"### Positions ({len(positions)})")
    if positions:
        lines.append("| Symbol | Side | Qty | Market Value | Unrealized P&L | Avg Entry |")
        lines.append("|--------|------|-----|-------------|----------------|-----------|")
        for p in positions:
            lines.append(
                f"| {p.get('symbol')} | {p.get('side')} | {p.get('qty')} "
                f"| {p.get('market_value')} | {p.get('unrealized_pl')} "
                f"| {p.get('avg_entry_price')} |"
            )
    else:
        lines.append("_No open positions._")

    lines.append("")
    lines.append(f"### Open Orders ({len(orders)})")
    if orders:
        lines.append("| Symbol | Side | Type | TIF | Qty | Status | SL | TP | Client Order ID |")
        lines.append("|--------|------|------|-----|-----|--------|----|----|-----------------|")
        for o in orders:
            legs = o.get("legs") or []
            sl = next((l.get("stop_price") for l in legs if l.get("type") == "stop"), "-")
            tp = next((l.get("limit_price") for l in legs if l.get("type") == "limit"), "-")
            lines.append(
                f"| {o.get('symbol')} | {o.get('side')} | {o.get('type')} "
                f"| {o.get('time_in_force')} | {o.get('qty')} | {o.get('status')} "
                f"| {sl} | {tp} | {o.get('client_order_id')} |"
            )
        lines.append("")
        lines.append("<details><summary>Full JSON</summary>")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(orders, indent=2))
        lines.append("```")
        lines.append("</details>")
    else:
        lines.append("_No open orders._")

    summary = "\n".join(lines)
    print(summary)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write(summary + "\n")


if __name__ == "__main__":
    main()
