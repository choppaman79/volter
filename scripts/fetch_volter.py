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

# CSVヘッダーの中で「瞬時発電電力」を表す列名(部分一致で検索)
POWER_COLUMN_HINT = "IEM3255"
# 参考として合わせて記録する積算値の列(部分一致で検索)
ENERGY_COLUMN_HINT = "Produced energy EM1"


def log(msg: str) -> None:
    print(f"[volter] {msg}", flush=True)


def fetch_export_csv(username: str, password: str, start_date: str, end_date: str, dest_path: Path) -> None:
    """Volter SpaceにログインしてData Exportを実行し、CSVをdest_pathに保存する"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            log(f"open {LOGIN_URL}")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            # SPAなのでDOM構築後もJSでフォームが描画されるまで少し時間がかかる
            page.get_by_text("LOGIN", exact=False).first.wait_for(timeout=30000)

            # --- ログイン ---
            # ラベル文字列 "Username" / "Password" に近い入力欄を探す(複数戦略でフォールバック)
            user_input = _find_input(page, ["Username", "username", "email"])
            pass_input = _find_input(page, ["Password", "password"])
            user_input.click()
            user_input.fill(username)
            pass_input.click()
            pass_input.fill(password)

            login_btn = page.get_by_text("LOGIN", exact=False).first
            login_btn.click()

            # ログインが本当に成功したか(URLが/loginから離れたか)を明示的に確認する。
            # これを確認せずに進むと、ログイン失敗時にログイン画面のままの入力欄へ
            # 後続の日付入力が誤って書き込まれてしまう(過去に発生した不具合)。
            try:
                page.wait_for_url(lambda url: "login" not in url, timeout=20000)
            except PWTimeout:
                raise RuntimeError(
                    "ログインに失敗しました。VOLTER_USER/VOLTER_PASSが正しいか確認してください。"
                    f" (現在のURL: {page.url})"
                )
            log(f"login ok, url={page.url}")

            # --- ユニットページへ ---
            page.goto(UNIT_URL, wait_until="domcontentloaded", timeout=60000)
            log(f"opened {UNIT_URL}")
            if "login" in page.url:
                raise RuntimeError(f"ユニットページを開けませんでした(ログイン画面にリダイレクト): {page.url}")

            # このページは常時ポーリングしているため networkidle 待ちはタイムアウトする。
            # 実際に必要な DATA EXPORT の見出しが出るまで待つ。
            page.get_by_text("DATA EXPORT", exact=False).first.wait_for(timeout=30000)

            # --- 日付入力 ---
            start_input = _find_input(page, ["StartDate", "Start Date", "start"])
            end_input = _find_input(page, ["EndDate", "End Date", "end"])

            _set_date_field(page, start_input, start_date)
            _set_date_field(page, end_input, end_date)

            # --- エクスポート実行 & ダウンロード捕捉 ---
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
                    throw new Error('EXPORTボタンが見つかりません(15秒待機後)');
                }
                """
            )
            export_el = export_handle.as_element()
            if export_el is None:
                if "login" in page.url:
                    raise RuntimeError(
                        f"EXPORT操作中にセッションが切れてログイン画面に戻されました(現在のURL: {page.url})。"
                        f" もう一度実行してみてください。"
                    )
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
                log(f"captured response from {captured.get('url')} (content-type={captured.get('ctype')})")
                dest_path.write_bytes(captured["data"])
                log(f"saved export (network response) -> {dest_path}")
                try:
                    preview = captured["data"][:1500].decode("utf-8", errors="replace")
                    log(f"captured data preview (先頭1500文字):\n{preview}")
                except Exception:
                    pass
            else:
                log(f"open pages count = {len(context.pages)}")
                for i, p in enumerate(context.pages):
                    log(f"  page[{i}].url = {p.url}")
                log(f"直近のレスポンス一覧(最大20件):")
                for url, ctype in all_responses[-20:]:
                    log(f"  [{ctype}] {url}")
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


def _find_input(page, label_candidates):
    """ラベル文字列候補から近傍の入力欄を推測して返す(複数戦略)"""
    for label in label_candidates:
        # 1) placeholder一致
        loc = page.get_by_placeholder(label, exact=False)
        if loc.count() > 0:
            return loc.first
        # 2) label要素の次のinput
        loc = page.locator(f"xpath=//*[contains(text(), '{label}')]/following::input[1]")
        if loc.count() > 0:
            return loc.first
        # 3) aria-label一致
        loc = page.get_by_label(label, exact=False)
        if loc.count() > 0:
            return loc.first
    raise RuntimeError(f"入力欄が見つかりません: {label_candidates}")


def _set_date_field(page, input_locator, date_str: str) -> None:
    """日付入力欄に日付を設定する(DD.MM.YYYY形式を想定、カレンダーPopupは押し戻す)"""
    input_locator.click()
    try:
        input_locator.fill("")
    except Exception:
        # fill不可(readonly等)な場合はキーボード全選択→削除
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    input_locator.type(date_str, delay=30)
    page.keyboard.press("Escape")


def parse_power_at_midnight(export_path: Path, target_utc_dt):
    """エクスポートデータ(JSON or CSV)から瞬時発電電力(kW)と積算値を取り出す"""
    raw_bytes = export_path.read_bytes()
    text_head = raw_bytes[:200].lstrip()

    if text_head.startswith(b"{") or text_head.startswith(b"["):
        return _parse_power_from_json(export_path, target_utc_dt)
    else:
        return _parse_power_from_csv(export_path)


def _parse_power_from_csv(csv_path: Path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        first_row = next(reader)

    power_idx = _find_column(header, POWER_COLUMN_HINT)
    energy_idx = _find_column(header, ENERGY_COLUMN_HINT)

    timestamp = first_row[0].strip('"')
    power_kw = first_row[power_idx]
    energy_wh = first_row[energy_idx] if energy_idx is not None else ""

    return timestamp, power_kw, energy_wh


def _find_metric_series(obj, hint):
    """JSON構造内を再帰的に探索し、名前がhintに一致する時系列データ(配列)を探す"""
    if isinstance(obj, dict):
        name = str(obj.get("name") or obj.get("label") or obj.get("title") or obj.get("description") or "")
        for key in ("values", "data", "samples", "points", "series"):
            candidate = obj.get(key)
            if isinstance(candidate, list) and hint.lower() in name.lower():
                return candidate
        for v in obj.values():
            result = _find_metric_series(v, hint)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _find_metric_series(item, hint)
            if result:
                return result
    return None


def _extract_time_value(sample):
    if isinstance(sample, dict):
        ts = sample.get("t") or sample.get("time") or sample.get("timestamp") or sample.get("ts")
        val = sample.get("v") if "v" in sample else sample.get("value")
        return ts, val
    if isinstance(sample, (list, tuple)) and len(sample) >= 2:
        return sample[0], sample[1]
    return None, sample


def _parse_power_from_json(json_path: Path, target_utc_dt):
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        for key in ("data", "items", "results", "records"):
            if isinstance(data.get(key), list):
                data = data[key]
                break

    if not isinstance(data, list) or not data:
        raise RuntimeError(f"想定外のJSON形式です(list以外/空)。 data/raw/{json_path.name} を確認してください。")

    log(f"json record count = {len(data)}")

    POWER_FIELD = "1254"
    ENERGY_FIELD = "1256"

    # 各レコードの本物のtimestampフィールドを使って、目標時刻(JST 0時)に最も近いものを選ぶ
    dated = []
    for rec in data:
        ts_str = rec.get("timestamp")
        if not ts_str or POWER_FIELD not in rec:
            continue
        try:
            rec_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        dated.append((rec_dt, rec))

    if not dated:
        raise RuntimeError(f"JSON内に有効なレコード(timestamp/{POWER_FIELD})が見つかりませんでした。 data/raw/{json_path.name} を確認してください。")

    dated.sort(key=lambda pair: abs((pair[0] - target_utc_dt).total_seconds()))
    best_dt, best_rec = dated[0]
    diff_sec = abs((best_dt - target_utc_dt).total_seconds())
    log(f"target(JST0時,UTC換算)={target_utc_dt.isoformat()}  選択レコード時刻={best_dt.isoformat()}  差={diff_sec:.0f}秒")

    power_kw = best_rec.get(POWER_FIELD)
    energy_wh = best_rec.get(ENERGY_FIELD, "")
    timestamp = best_dt.isoformat()

    return str(timestamp), power_kw, energy_wh


def _find_column(header, hint):
    for i, col in enumerate(header):
        if hint.lower() in col.lower():
            return i
    return None


def append_log_row(target_date: str, timestamp: str, power_kw: str, energy_wh: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not LOG_CSV.exists()
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["date", "record_timestamp_utc", "power_kW", "produced_energy_em1_Wh"])
        writer.writerow([target_date, timestamp, power_kw, energy_wh])
    log(f"appended: {target_date}, {power_kw} kW")


def main():
    username = os.environ.get("VOLTER_USER")
    password = os.environ.get("VOLTER_PASS")
    if not username or not password:
        log("環境変数 VOLTER_USER / VOLTER_PASS が設定されていません")
        sys.exit(1)

    now_jst = datetime.now(JST)
    run_date = now_jst.date()
    target_date = run_date - timedelta(days=1)  # 記録したい「24:00」はこの日の終わり

    # 「run_dateのJST0時」= 「target_dateの24:00」に相当するUTC時刻
    target_utc_dt = datetime(run_date.year, run_date.month, run_date.day, 0, 0, 0, tzinfo=JST).astimezone(ZoneInfo("UTC"))

    start_str = target_date.strftime("%d.%m.%Y")
    end_str = (run_date + timedelta(days=1)).strftime("%d.%m.%Y")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"{run_date.isoformat()}.json"

    log(f"target_date(24:00)={target_date}, export range {start_str} - {end_str}")
    fetch_export_csv(username, password, start_str, end_str, raw_path)

    timestamp, power_kw, energy_wh = parse_power_at_midnight(raw_path, target_utc_dt)
    append_log_row(target_date.isoformat(), timestamp, power_kw, energy_wh)


if __name__ == "__main__":
    main()
