#!/usr/bin/env python3
"""
Volter Space 自動記録スクリプト

毎日決まった時刻(JST 00:10 想定)に実行し、
- 前日24:00(=当日00:00)時点の瞬時発電電力(kW)を取得
- data/daily_log.csv に1行追記
- その日のエクスポート生データを data/raw/YYYY-MM-DD.csv として保存

ログイン失敗・要素が見つからない等のエラー時は debug_screenshot.png を保存する
(GitHub Actions側でartifactとしてアップロードしてデバッグに使う)。
"""

import csv
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

JST = ZoneInfo("Asia/Tokyo")

LOGIN_URL = "https://space.volter.fi/login"
UNIT_URL = "https://space.volter.fi/units/094623"

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
LOG_CSV = DATA_DIR / "daily_log.csv"
DEBUG_SCREENSHOT = REPO_ROOT / "debug_screenshot.png"

POWER_COLUMN_HINT = "IEM3255"
ENERGY_COLUMN_HINT = "Produced energy EM1"


def log(msg: str) -> None:
    print(f"[volter] {msg}", flush=True)


def fetch_export_csv(username: str, password: str, start_date: str, end_date: str, dest_path: Path) -> None:
    """Volter SpaceにログインしてData Exportを実行し、CSVをdest_pathに保存する"""
    with sync_playwright() as p:
