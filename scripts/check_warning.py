#!/usr/bin/env python3
"""
Volter Space 事前警告チェック(30分ごと実行想定)

現在値がロック(停止)しきい値にどれくらい近づいているかを確認し、
新たにしきい値へ接近した項目があればメールで通知する。
一度警告した項目は、しきい値を十分下回るまで再通知しない(スパム防止)。
data/warning_state.json に現在の警告状態を保存し、次回実行時と比較する。

日次の記録(data/daily_log.csv 等)には影響しない、独立したチェック専用スクリプト。
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

JST = ZoneInfo("Asia/Tokyo")

LOGIN_URL = "https://space.volter.fi/login"
UNIT_URL = "https://space.volter.fi/units/094623"

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
STATE_PATH = DATA_DIR / "warning_state.json"
DEBUG_SCREENSHOT = REPO_ROOT / "debug_screenshot_warning.png"
TMP_EXPORT_PATH = Path("/tmp/warning_check_export.json")

DP_BEFORE_FIELD = "1208"
DP_AFTER_FIELD = "1206"
ENGINE_FIELD = "1242"

WARNING_RATIO_HIGH = 0.85      # 高温/高圧/低油圧系: しきい値の85%に到達したら警告
WARNING_RATIO_NOLIMIT = 0.5    # 冷却循環圧力の低下率: 50%低下したら警告
CLEAR_MARGIN = 0.10            # 警告解除のヒステリシス(warning比率-10%を下回ったら解除)

# fetch_volter.py の停止原因診断ロジックと同じ定義(数値フィールドコード)
STOP_DIAG_PAIRS = [
    dict(value="1232", limit="1266", label="ガス化炉スロート温度", code="TE003", page=8, unit="\u2103", type="high"),
    dict(value="1224", limit="1268", label="ガス化炉トップ温度", code="TE002", page=7, unit="\u2103", type="high"),
    dict(value="1226", limit="1270", label="ガス化炉出口ガス温度", code="TE004", page=9, unit="\u2103", type="high"),
    dict(value="1228", limit="1272", label="フィルター前ガス温度(1次)", code="TE005/TE014", page=10, unit="\u2103", type="high"),
    dict(value="1230", limit="1272", label="フィルター前ガス温度(2次)", code="TE005/TE014", page=10, unit="\u2103", type="high"),
    dict(value="__dp__", limit="1274", label="フィルター差圧", code="PDT06", page=16, unit="Pa", type="high"),
    dict(value="1206", limit="1276", label="フィルター前ガス圧力", code="PT001", page=15, unit="Pa", type="high"),
    dict(value="1208", limit="1278", label="エンジン前ガス圧力", code="PT002", page=16, unit="Pa", type="high"),
    dict(value="1240", limit="1280", label="エンジン内冷却水温度", code="TE012/GE01", page=13, unit="\u2103", type="high"),
    dict(value="1244", limit="1282", label="エンジン油圧", code="GE01", page=28, unit="bar", type="low"),
]
STOP_DIAG_NOLIMIT = [
    dict(value="1204", label="冷却循環圧力(低下)", code="PT003 Low", page=18, unit="bar", min_avg=0.2),
]


def log(msg: str) -> None:
    print(f"[warning-check] {msg}", flush=True)


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


def _set_date_field(page, input_locator, date_str: str) -> None:
    input_locator.click()
    try:
        input_locator.fill("")
    except Exception:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    input_locator.type(date_str, delay=30)
    page.keyboard.press("Escape")


def fetch_export_csv(username: str, password: str, start_date: str, end_date: str, dest_path: Path) -> None:
    """Volter SpaceにログインしてData Exportを実行し、結果をdest_pathに保存する
    (fetch_volter.pyと同一ロジック)"""
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
                raise RuntimeError(f"ユニットページを開けませんでした(ログイン画面にリダイレクト): {page.url}")

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
                            return btn;
                        }
                        await new Promise(r => setTimeout(r, 500));
                    }
                    throw new Error('EXPORTボタンが見つかりません(15秒待機後)');
                }
                """
            )
            export_el = export_handle.as_element()
            if export_el is None:
                if "login" in page.url:
                    raise RuntimeError(
                        f"EXPORT操作中にセッションが切れてログイン画面に戻されました(現在のURL: {page.url})。"
                    )
                raise RuntimeError("EXPORTボタンの要素ハンドルが取得できませんでした")
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
                log(f"captured response from {captured.get('url')} (content-type={captured.get('ctype')})")
                dest_path.write_bytes(captured["data"])
                log(f"saved export (network response) -> {dest_path}")
            else:
                raise RuntimeError("EXPORTクリック後、ダウンロードもCSVレスポンスも検出できませんでした")

        except Exception:
            try:
                page.screenshot(path=str(DEBUG_SCREENSHOT), full_page=True)
                log(f"debug screenshot saved -> {DEBUG_SCREENSHOT}")
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()


def _diag_num(rec, code):
    if code == "__dp__":
        try:
            return float(rec.get(DP_BEFORE_FIELD)) - float(rec.get(DP_AFTER_FIELD))
        except (TypeError, ValueError):
            return None
    v = rec.get(code)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_latest_status(records):
    """最新レコードが運転中であれば、各項目のratio(しきい値への近さ)一覧を返す。
    停止中(Engine<=0)の場合はNoneを返す(その場合は日次の停止診断メールに任せる)。"""
    if not records:
        return None
    last_idx = len(records) - 1
    cur = records[last_idx]
    engine = _diag_num(cur, ENGINE_FIELD) or 0
    if engine <= 0:
        return None

    win_start = max(0, last_idx - 14)
    window = records[win_start:last_idx + 1]
    results = []

    for p in STOP_DIAG_PAIRS:
        v = _diag_num(cur, p["value"])
        lim = _diag_num(cur, p["limit"])
        if v is None or lim is None or lim == 0:
            continue
        ratio = v / lim if p["type"] == "high" else (lim / max(v, 0.01) if lim > 0 else 0)
        results.append({
            "code": p["code"], "label": p["label"], "ratio": ratio, "is_no_limit": False,
            "unit": p["unit"], "value": v, "limit": lim, "page": p["page"],
        })

    for n in STOP_DIAG_NOLIMIT:
        vals = [x for x in (_diag_num(r, n["value"]) for r in window[:-1]) if x is not None]
        v = _diag_num(cur, n["value"])
        if len(vals) < 3 or v is None:
            continue
        avg = sum(vals) / len(vals)
        if avg < n.get("min_avg", 0):
            continue
        drop = 1 - (v / avg) if avg > 0 else 0
        results.append({
            "code": n["code"], "label": n["label"], "ratio": max(0, drop), "is_no_limit": True,
            "unit": n["unit"], "value": v, "limit": avg, "page": n["page"],
        })

    return results


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_warning_email(items, now_jst_str):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    notify_to = os.environ.get("NOTIFY_EMAIL")
    if not gmail_user or not gmail_pass or not notify_to:
        log("GMAIL_USER/GMAIL_APP_PASSWORD/NOTIFY_EMAILが未設定のため、メール送信をスキップします")
        return

    lines = [f"Volterの運転値がしきい値に接近しています({now_jst_str} JST 時点)。", ""]
    for it in items:
        page_str = f" (マニュアルP.{it['page']})" if it.get("page") else ""
        lines.append(
            f"・{it['label']} [{it['code']}]  実測 {it['value']:.1f}{it['unit']} / "
            f"しきい値 {it['limit']:.1f}{it['unit']}  ({it['ratio']*100:.0f}%接近){page_str}"
        )
    lines.append("")
    lines.append("この通知は30分ごとのチェックによる事前警告です。実際に停止した場合は別途「停止イベントを検出しました」メールが届きます。")
    body = "\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = f"【Volter】しきい値接近の事前警告({len(items)}件)"
    msg["From"] = gmail_user
    msg["To"] = notify_to

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, [notify_to], msg.as_string())
        log(f"事前警告メールを送信しました -> {notify_to}")
    except Exception as e:
        log(f"事前警告メール送信に失敗しました: {e}")


def main():
    if os.environ.get("TEST_MODE", "").lower() == "true":
        log("TEST_MODEが有効なため、Volterへの接続をスキップしてテスト用の警告メールを送信します")
        sample_items = [
            {"code": "TEST01", "label": "【テスト】ガス化炉スロート温度", "ratio": 0.90,
             "is_no_limit": False, "unit": "\u2103", "value": 1170.0, "limit": 1300.0, "page": 8},
            {"code": "TEST02", "label": "【テスト】冷却循環圧力(低下)", "ratio": 0.55,
             "is_no_limit": True, "unit": "bar", "value": 0.09, "limit": 0.20, "page": 18},
        ]
        send_warning_email(sample_items, datetime.now(JST).isoformat())
        log("テスト送信処理が完了しました(実際のVolterデータ取得・状態保存は行っていません)")
        return

    username = os.environ.get("VOLTER_USER")
    password = os.environ.get("VOLTER_PASS")
    if not username or not password:
        log("環境変数 VOLTER_USER / VOLTER_PASS が設定されていません")
        sys.exit(1)

    now_jst = datetime.now(JST)
    start_str = now_jst.strftime("%d.%m.%Y")
    end_str = (now_jst + timedelta(days=1)).strftime("%d.%m.%Y")

    log(f"export range {start_str} - {end_str}")
    fetch_export_csv(username, password, start_str, end_str, TMP_EXPORT_PATH)

    raw_bytes = TMP_EXPORT_PATH.read_bytes()
    text_head = raw_bytes[:200].lstrip()
    if not (text_head.startswith(b"{") or text_head.startswith(b"[")):
        log("想定外の形式のデータを取得しました。事前警告チェックをスキップします")
        return

    data = json.loads(raw_bytes)
    if isinstance(data, dict):
        for key in ("data", "items", "results", "records"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    records = [r for r in data if r.get("timestamp")]
    records.sort(key=lambda r: r["timestamp"])

    status = get_latest_status(records)
    state = load_state()
    changed = False
    newly_warned = []

    if status is None:
        log("エンジン停止中、または有効なデータなし。事前警告チェック対象外")
    else:
        for item in status:
            code = item["code"]
            threshold = WARNING_RATIO_NOLIMIT if item["is_no_limit"] else WARNING_RATIO_HIGH
            is_warn = item["ratio"] >= threshold
            was_warn = state.get(code, False)
            if is_warn and not was_warn:
                newly_warned.append(item)
                state[code] = True
                changed = True
            elif not is_warn and was_warn and item["ratio"] < (threshold - CLEAR_MARGIN):
                state[code] = False
                changed = True

    if newly_warned:
        log(f"新規警告: {len(newly_warned)}件 -> {[i['code'] for i in newly_warned]}")
        send_warning_email(newly_warned, now_jst.isoformat())
    else:
        log("新規警告なし")

    if changed:
        save_state(state)
        log(f"warning_state.json を更新しました")


if __name__ == "__main__":
    main()
