"""Minimal signed REST client for the Kalshi trade API (read-only endpoints)."""

from __future__ import annotations

import configparser
import time
from base64 import b64encode
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

PROD = "https://api.elections.kalshi.com"
DEMO = "https://demo-api.kalshi.co"
BASE_PATH = "/trade-api/v2"


class KalshiClient:
    def __init__(self, key_id: str, private_key_path: str | Path, use_demo: bool = False):
        self.key_id = key_id
        self.host = DEMO if use_demo else PROD
        with open(private_key_path, "rb") as fh:
            self.private_key = serialization.load_pem_private_key(fh.read(), password=None)
        self.session = requests.Session()

    @classmethod
    def from_config(cls, path: str | Path = "config.ini") -> "KalshiClient":
        cfg = configparser.ConfigParser()
        cfg.read(path)
        k = cfg["kalshi"]
        key_path = Path(k["private_key_path"])
        if not key_path.is_absolute():
            key_path = Path(path).resolve().parent / key_path
        return cls(
            key_id=k["api_key_id"],
            private_key_path=key_path,
            use_demo=k.getboolean("use_demo", fallback=True),
        )

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        message = f"{ts}{method}{path}".encode()
        sig = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Accept": "application/json",
        }

    def get(self, path: str, **params) -> dict:
        """`path` is relative to /trade-api/v2. The signature covers the path only."""
        full = f"{BASE_PATH}{path}"
        r = self.session.get(
            self.host + full,
            headers=self._headers("GET", full),
            params={k: v for k, v in params.items() if v is not None},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def series(self, *, category=None) -> list[dict]:
        """Series metadata: ticker, title, frequency. Not paginated by the API."""
        return self.get("/series", category=category).get("series", [])

    def events(self, *, series_ticker=None, status="open", limit=200) -> list[dict]:
        """Open events for a series. Carries `strike_date`, so the expiry menu
        can be built without pulling every market on every ladder."""
        out: list[dict] = []
        cursor = None
        while True:
            page = self.get(
                "/events",
                series_ticker=series_ticker,
                status=status,
                limit=min(limit, 200),
                cursor=cursor,
            )
            out.extend(page.get("events", []))
            cursor = page.get("cursor")
            if not cursor or len(out) >= limit:
                break
        return out

    def markets(self, *, series_ticker=None, event_ticker=None, status="open", limit=1000) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            page = self.get(
                "/markets",
                series_ticker=series_ticker,
                event_ticker=event_ticker,
                status=status,
                limit=min(limit, 1000),
                cursor=cursor,
            )
            out.extend(page.get("markets", []))
            cursor = page.get("cursor")
            if not cursor or len(out) >= limit:
                break
        return out
