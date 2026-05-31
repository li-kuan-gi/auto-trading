# query_alpaca_rest.py

import os
import requests


BASE_URL = "https://paper-api.alpaca.markets/v2"


def alpaca_get(path: str, params: dict | None = None):
    response = requests.get(
        f"{BASE_URL}{path}",
        headers={
            "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        },
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_positions():
    return alpaca_get("/positions")


def get_position(symbol: str):
    try:
        return alpaca_get(f"/positions/{symbol}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def get_open_orders(symbol: str | None = None):
    params = {
        "status": "open",
        "limit": 100,
        "direction": "desc",
        "nested": "true",
    }

    if symbol:
        params["symbols"] = symbol

    return alpaca_get("/orders", params=params)


def get_all_recent_orders():
    return alpaca_get(
        "/orders",
        params={
            "status": "all",
            "limit": 100,
            "direction": "desc",
            "nested": "true",
        },
    )


if __name__ == "__main__":
    print("positions:")
    print(get_positions())

    print("open orders:")
    print(get_open_orders())

    print("AAPL position:")
    print(get_position("AAPL"))

    print("AAPL open orders:")
    print(get_open_orders("AAPL"))
