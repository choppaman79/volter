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
import json
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
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            log(f"open {LOGIN_URL}")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.get_by_text("LOGIN", exact=False).first.wait_for(timeout=30000)

            user_input = _find_input(page, ["Username", "username", "email"])
            pass_input = _find_input(page, ["Password", "password"])
            user_input.click()
            user_input.fill(username)
            pass_input.click()
            pass_input.fill(password)

            login_btn = page.get_by_text("LOGIN", exact=False).first
            login_btn.click()

            try:
                page.wait_for_url(lambda url: "login" not in url, timeout=20000)
            except PWTimeout:
                raise RuntimeError(
                    "ログインに失敗しました。VOLTER_USER/VOLTER_PASSが正しいか確認してください。"
                    f" (現在のURL: {page.url})"
                )
            log(f"login ok, url={page.url}")

            page.goto(UNIT_URL, wait_until="domcontentloaded", timeout=60000)
            log(f"opened {UNIT_URL}")
            if "login" in page.url:
                raise RuntimeError(f"ユニットページを開けませんでした: {page.url}")

            page.get_by_text("DATA EXPORT", exact=False).first.wait_for(timeout=30000)

            start_input = _find_input(page, ["StartDate", "Start Date", "start"])
            end_input = _find_input(page, ["EndDate", "End Date", "end"])

            _set_date_field(page, start_input, start_date)
            _set_date_field(page, end_input, end_date)

            captured = {}
            all_responses = []

            def handle_response(response):
                try:
                    ctype = response.headers.get("content-type", "").lower()
                except Exception:
                    ctype = ""
                all_responses.append((response.url, ctype))
                if len(all_responses) > 100:
                    all_responses.pop(0)
                if "data" in captured:
                    return
                url_lower = response.url.lower()
                if "dataservers.lcp.io" in url_lower or "csv" in ctype or "octet-stream" in ctype or "csv" in url_lower or "export" in url_lower:
                    try:
                        body = response.body()
                        if body:
                            captured["data"] = body
                            captured["url"] = response.url
                            captured["ctype"] = ctype
                    except Exception:
                        pass

            context.on("response", handle_response)

            downloads = {}

            def handle_download(download):
                downloads["obj"] = download

            context.on("download", handle_download)

            export_handle = page.evaluate_handle(
                """
                async () => {
                    const norm = s => (s || '').trim().toUpperCase();
                    const all = Array.from(document.querySelectorAll('*'));
                    const heading = all.find(el => norm(el.textContent) === 'DATA EXPORT' && el.children.length === 0);
                    if (!heading) throw new Error('DATA EXPORT見出しが見つかりません');
                    const headingTop = heading.getBoundingClientRect().top + window.scrollY;

                    const findBtn = () => {
                        const candidates = Array.from(document.querySelectorAll('button, div, span, a, input'))
                            .filter(el => el.children.length === 0 && norm(el.value || el.textContent) === 'EXPORT');
                        const below = candidates.filter(el => (el.getBoundingClientRect().top + window.scrollY) >= headingTop);
                        below.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
                        return below[0];
                    };

                    for (let i = 0; i < 30; i++) {
                        const btn = findBtn();
                        if (btn) {
                            btn.scrollIntoView({block: 'center'});
                            window.__exportDebugInfo = {
                                tag: btn.tagName,
                                cls: btn.className,
                                html: btn.outerHTML.slice(0, 500),
                                parentHtml: btn.parentElement ? btn.parentElement.outerHTML.slice(0, 800) : ''
                            };
                            return btn;
                        }
                        await new Promise(r => setTimeout(r, 500));
                    }
                    throw new Error('EXPORTボタンが見つかりません');
                }
                """
            )
            export_el = export_handle.as_element()
            if export_el is None:
                raise RuntimeError("EXPORTボタンの要素ハンドルが取得できませんでした")

            debug_info = page.evaluate("window.__exportDebugInfo")
            log(f"export element debug info: {debug_info}")
            page.wait_for_timeout(300)
            export_el.click(force=True, timeout=15000)

            for _ in range(60):
                if "data" in captured or "obj" in downloads:
                    break
                page.wait_for_timeout(500)

            if "obj" in downloads:
                downloads["obj"].save_as(str(dest_path))
                log(f"saved export (download event) -> {dest_path}")
            elif "data" in captured:
                dest_path.write_bytes(captured["data"])
                log(f"saved export (network response) -> {dest_path}")
            else:
                raise RuntimeError("EXPORTクリック後、ダウンロードもCSVレスポンスも検出できませんでした")

        except Exception:
            try:
                page.screenshot(path=str(DEBUG_SCREENSHOT), full_page=True)
                log(f"debug screenshot saved -> {DEBUG_SCREENSCREENSHOT}")
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()


def _find_input(page, label_candidates):
    for label in label_candidates:
        loc = page.get_by_placeholder(label, exact=False)
        if loc.count() > 0:
            return loc.first
        loc = page.locator(f"xpath=//*[contains(text(), '{label}')]/following::input[1]")
        if loc.count() > 0:
            return loc.first
        loc = page.get_by_label(label, exact=False)
        if loc.count() > 0:
            return loc.first
    raise RuntimeError(f"入力欄が見つかりません: {label_candidates}")


def _set_date_field(page, input_locator
