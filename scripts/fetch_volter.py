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
RAW_DIR = DATA_DIR / "raw"
LOG_CSV = DATA_DIR / "daily_log.csv"
CLEANING_CSV = DATA_DIR / "cleaning_events.csv"
DEBUG_SCREENSHOT = REPO_ROOT / "debug_screenshot.png"

# CSVヘッダーの中で「瞬時発電電力」を表す列名(部分一致で検索)
POWER_COLUMN_HINT = "IEM3255"
# 参考として合わせて記録する積算値の列(部分一致で検索)
ENERGY_COLUMN_HINT = "Produced energy EM1"

# クリーニングフィルター関連
DP_BEFORE_FIELD = "1208"  # 差圧(クリーニング前側)
DP_AFTER_FIELD = "1206"   # 差圧(クリーニング後側)
DP_DROP_THRESHOLD = 500   # この値以上下降したらクリーニングとみなす
DP_AFTER_MAX = 1000       # 下降後の差圧がこの値以下ならクリーニング完了とみなす
DP_TRIGGER_THRESHOLD = 2500  # 下降前がこの値以上なら「差圧起因」トリガー

# 意味が確認できているJSONフィールドコード -> 分かりやすい列名
# (確認できていないコードは番号のまま出力する)
FIELD_LABELS = {
    "1100": "unit_status1",
    "1200": "serial_number",
    "1204": "circulation_pressure_bar",
    "1206": "gas_pressure_before_filter_Pa",
    "1208": "gas_pressure_before_engine_Pa",
    "1224": "gasifier_top_temp_C",
    "1226": "gas_temp_after_gasifier_C",
    "1228": "gas_temp_before_filter_primary_C",
    "1230": "gas_temp_before_filter_secondary_C",
    "1232": "gasifier_throat_temp_C",
    "1240": "cooling_circ_temp_in_engine_C",
    "1242": "engine",
    "1244": "oil_pressure_bar",
    "1254": "power_kW_IEM3255",
    "1256": "produced_energy_EM1_Wh",
    "1266": "throat_temp_high_limit_C",
    "1268": "gasifier_top_temp_high_limit_C",
    "1270": "gas_temp_after_gasifier_high_limit_C",
    "1272": "gas_temp_before_filter_high_limit_C",
    "1274": "filter_dp_high_limit_Pa",
    "1276": "gas_pressure_before_filter_high_limit_Pa",
    "1278": "gas_pressure_before_engine_high_limit_Pa",
    "1280": "cooling_circ_temp_high_limit_C",
    "1282": "oil_pressure_low_limit_bar",
}


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
    timestamp = best_dt.astimezone(JST).isoformat()

    return str(timestamp), power_kw, energy_wh


def _find_column(header, hint):
    for i, col in enumerate(header):
        if hint.lower() in col.lower():
            return i
    return None


def find_max_power_of_day(json_path: Path):
    """1日分のエクスポートJSONから、その日の最大瞬時発電電力(kW)とその時刻を返す"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("data", "items", "results", "records"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return None, None

    POWER_FIELD = "1254"
    best_val = None
    best_ts = None
    for rec in data:
        ts_str = rec.get("timestamp")
        if not ts_str or POWER_FIELD not in rec:
            continue
        try:
            v = float(rec[POWER_FIELD])
        except (TypeError, ValueError):
            continue
        if best_val is None or v > best_val:
            best_val = v
            try:
                rec_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                best_ts = rec_dt.astimezone(JST).isoformat()
            except Exception:
                best_ts = ts_str
    return best_val, best_ts


def append_log_row(target_date: str, timestamp: str, power_kw: str, energy_wh: str,
                    max_power_kw=None, max_power_time: str = "") -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not LOG_CSV.exists()
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["date", "record_timestamp_jst", "power_kW", "produced_energy_em1_Wh",
                              "max_power_kW", "max_power_time_jst"])
        writer.writerow([target_date, timestamp, power_kw, energy_wh,
                          max_power_kw if max_power_kw is not None else "", max_power_time])
    log(f"appended: {target_date}, {power_kw} kW (瞬時) / max {max_power_kw} kW")


def detect_cleaning_events_from_json(json_path: Path):
    """1日分のエクスポートJSONから、差圧の急降下(=クリーニング完了)イベントを検出する"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("data", "items", "results", "records"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []

    rows = []
    for rec in data:
        ts_str = rec.get("timestamp")
        if not ts_str or DP_BEFORE_FIELD not in rec or DP_AFTER_FIELD not in rec:
            continue
        try:
            rec_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            dp = float(rec[DP_BEFORE_FIELD]) - float(rec[DP_AFTER_FIELD])
        except Exception:
            continue
        rows.append((rec_dt, dp))
    rows.sort(key=lambda pair: pair[0])

    events = []
    for i in range(1, len(rows)):
        prev_dt, prev_dp = rows[i - 1]
        curr_dt, curr_dp = rows[i]
        drop = prev_dp - curr_dp
        if drop >= DP_DROP_THRESHOLD and 0 <= curr_dp <= DP_AFTER_MAX:
            events.append({
                "timestamp_jst": curr_dt.astimezone(JST).isoformat(),
                "before_pa": round(prev_dp, 1),
                "after_pa": round(curr_dp, 1),
                "drop_pa": round(drop, 1),
                "trigger": "dp" if prev_dp >= DP_TRIGGER_THRESHOLD else "time",
            })
    return events


def merge_cleaning_events(new_events):
    """既存のcleaning_events.csvと新規検出イベントをマージし、間隔を再計算する。
    戻り値: (全イベントのリスト(時刻順、interval_min付き), 新規に追加されたイベントのインデックス付きリスト)
    """
    existing = {}
    if CLEANING_CSV.exists():
        with open(CLEANING_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[row["timestamp_jst"]] = row

    is_new_flags = {}
    for ev in new_events:
        key = ev["timestamp_jst"]
        if key not in existing:
            existing[key] = {
                "timestamp_jst": ev["timestamp_jst"],
                "before_pa": ev["before_pa"],
                "after_pa": ev["after_pa"],
                "drop_pa": ev["drop_pa"],
                "trigger": ev["trigger"],
            }
            is_new_flags[key] = True

    all_events = sorted(existing.values(), key=lambda r: r["timestamp_jst"])

    new_list = []
    for i, ev in enumerate(all_events):
        if i == 0:
            ev["interval_min"] = ""
        else:
            prev_dt = datetime.fromisoformat(all_events[i - 1]["timestamp_jst"])
            curr_dt = datetime.fromisoformat(ev["timestamp_jst"])
            interval_min = (curr_dt - prev_dt).total_seconds() / 60
            ev["interval_min"] = round(interval_min, 1)
        if is_new_flags.get(ev["timestamp_jst"]):
            new_list.append((i, ev))

    return all_events, new_list


def save_cleaning_events(all_events):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = ["timestamp_jst", "before_pa", "after_pa", "drop_pa", "interval_min", "trigger"]
    with open(CLEANING_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for ev in all_events:
            writer.writerow({k: ev.get(k, "") for k in fieldnames})


def find_short_interval_alerts(all_events, new_list):
    """新規イベントのうち、直前の間隔が「さらに前の間隔の半分以下」に急減したものを抽出"""
    alerts = []
    for idx, ev in new_list:
        if idx < 2:
            continue  # 比較対象の「前回間隔」がまだ無い
        curr_interval = ev.get("interval_min")
        prev_interval = all_events[idx - 1].get("interval_min")
        if curr_interval == "" or prev_interval in ("", None):
            continue
        try:
            curr_interval = float(curr_interval)
            prev_interval = float(prev_interval)
        except (TypeError, ValueError):
            continue
        if prev_interval > 0 and curr_interval <= prev_interval / 2:
            alerts.append((ev, prev_interval, curr_interval))
    return alerts


def send_alert_email(alerts):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    notify_to = os.environ.get("NOTIFY_EMAIL")
    if not gmail_user or not gmail_pass or not notify_to:
        log("GMAIL_USER/GMAIL_APP_PASSWORD/NOTIFY_EMAILが未設定のため、メール送信をスキップします")
        return

    lines = ["クリーニングフィルターの実施間隔が、直前の間隔より急に短くなりました。", ""]
    for ev, prev_interval, curr_interval in alerts:
        lines.append(
            f"・{ev['timestamp_jst']}  差圧 {ev['before_pa']}→{ev['after_pa']}Pa"
            f"（今回間隔: {curr_interval:.0f}分 / 前回間隔: {prev_interval:.0f}分）"
            f" トリガー: {'差圧2500超' if ev['trigger'] == 'dp' else '時間'}"
        )
    body = "\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = "【Volter】クリーニング間隔が急に短くなりました"
    msg["From"] = gmail_user
    msg["To"] = notify_to

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, [notify_to], msg.as_string())
        log(f"アラートメールを送信しました -> {notify_to}")
    except Exception as e:
        log(f"メール送信に失敗しました: {e}")


# ==== 停止原因診断 ====
# Volter停止原因診断ツール(dataexport.csv形式のマニュアル診断)の判定ロジックを
# 生JSON(数値フィールドコード)ベースに移植したもの。
# コードとdataexport.csvの列名の対応は、実際のdataexport.csvサンプルと
# 生JSONを突き合わせて確認済み。
ENGINE_FIELD = "1242"  # Engine (0=停止 / 1=運転)

# (value, limit, label, code, page, unit, type) type: "high"=上限接近 / "low"=下限接近
# value="__dp__" はフィルター差圧(1208-1206)を表す特殊値
STOP_DIAG_PAIRS = [
    dict(value="1232", limit="1266", label="ガス化炉スロート温度", code="TE003", page=8, unit="\u2103", type="high",
         fix=["エアノズル位置が低すぎる→ノズルを高く調整", "木質チップの品質不良(チップサイズ過大・形状不良)を確認",
              "スロートのクリンカーを除去し、チップの不純物を確認",
              "急激な上昇はセンサー不良・接続不良・クリンカー付着の可能性もあり(P36「ガス化炉の目詰まり」も参照)"]),
    dict(value="1224", limit="1268", label="ガス化炉トップ温度", code="TE002", page=7, unit="\u2103", type="high",
         fix=["燃料レベルが低すぎないか(レベルセンサー設定・含水量・サイトグラスの清潔さ)を確認",
              "上部温度センサー付近のスロートで火が上がっていないか確認",
              "上端の空気漏れ(ロータリーバルブベアリング・ガスケット・燃料/温度センサー取付・フィーディングチューブ・センターチューブ)→バキュームテストで確認",
              "設置空間の負圧が強すぎないか確認"]),
    dict(value="1226", limit="1270", label="ガス化炉出口ガス温度", code="TE004", page=9, unit="\u2103", type="high",
         fix=["スロート温度が高くないか確認(TE003)",
              "炭層レベルが低すぎないか→下部エアバルブを閉め気味に、アッシュオーガーのアイドル時間を短縮",
              "空気漏れ(一次冷却器上部フランジ・フレキシブルジョイント・センターチューブのひび)をバキュームテストで確認"]),
    dict(value="1228", limit="1272", label="フィルター前ガス温度(1次)", code="TE005/TE014", page=10, unit="\u2103", type="high",
         fix=["一次ガス冷却器の冷却効率を確認(クリーニングスクリューの回転方向、フロー制御、冷却回路のエア抜き)",
              "ガス化後のガス温度が高すぎないか確認(TE004)",
              "一次冷却器上部フレキシブルジョイント・下部軸受の空気漏れを確認"]),
    dict(value="1230", limit="1272", label="フィルター前ガス温度(2次)", code="TE005/TE014", page=10, unit="\u2103", type="high",
         fix=["一次ガス冷却器の冷却効率を確認(クリーニングスクリューの回転方向、フロー制御、冷却回路のエア抜き)",
              "ガス化後のガス温度が高すぎないか確認(TE004)",
              "一次冷却器上部フレキシブルジョイント・下部軸受の空気漏れを確認"]),
    dict(value="__dp__", limit="1274", label="フィルター差圧", code="PDT06", page=16, unit="Pa", type="high",
         fix=["メインガスフィルターの詰まり(灰オーガーの運転/アイドル時間、フィルター清掃・カートリッジ交換、洗浄圧力3bar設定を確認)",
              "セーフティフィルターの閉塞(交換時期・灰やタールによる閉塞)を確認",
              "二次ガス冷却器の詰まり(凝縮トラップの清掃)を確認",
              "エンジンの運転状態(ガス消費量増加)を確認"]),
    dict(value="1206", limit="1276", label="フィルター前ガス圧力", code="PT001", page=15, unit="Pa", type="high",
         fix=["ガス化炉内のガス流れ不足→下部エアバルブを開き気味に、アッシュグレートのアイドル時間を延長",
              "スロートのクリンカーを除去、エアノズル(メイン/ラジアル)の詰まりを確認",
              "一次ガス冷却器のクリーニングスクリュー動作を確認"]),
    dict(value="1208", limit="1278", label="エンジン前ガス圧力", code="PT002", page=16, unit="Pa", type="high",
         fix=["フィルター前圧力(PT001)が高くないか確認", "フィルター差圧(PDT06)が高くないか確認",
              "木質ガス流量過多(燃料品質、点火・バルブ調整・ラムダセンサー・圧縮などエンジン状態、空気漏れのバキュームテスト)"]),
    dict(value="1240", limit="1280", label="エンジン内冷却水温度", code="TE012/GE01", page=13, unit="\u2103", type="high",
         fix=["冷却回路の圧力・漏れを確認し、必要に応じて冷媒を追加", "冷却回路内のエア抜き(流量制御バルブを手動で開閉)",
              "流量制御弁の動作を確認", "エンジン損傷の兆候(オイルへの冷却水混入、漏れ、圧縮/リークダウンテスト)を確認"]),
    dict(value="1244", limit="1282", label="エンジン油圧", code="GE01", page=28, unit="bar", type="low",
         fix=["オイルレベルを確認し、必要に応じて補充。オイル漏れも点検",
              "油圧センサーの配線・状態を確認(旧灰色VDOセンサーの場合は新型真鍮センサーへの置換状況も確認)",
              "正しいオイルが規定通り交換されているか確認",
              "油圧レギュレーターの取付・スプリングを確認、異音(ベアリング摩耗の兆候)がないか確認"]),
]

STOP_DIAG_NOLIMIT = []  # PT003(冷却循環圧力)は100%になっても停止に至らないため対象外


def _diag_num(rec, code):
    """レコードからフィールド値をfloatで取得する(__dp__はフィルター差圧の特殊計算)"""
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


def _measure_all(records, last_run_idx):
    """停止直前の実測値をしきい値と比較し、原因候補の一覧を近さ順に返す"""
    win_start = max(0, last_run_idx - 14)
    window = records[win_start:last_run_idx + 1]
    cur = records[last_run_idx]
    prior = records[max(0, last_run_idx - 8)]
    all_c = []

    for p in STOP_DIAG_PAIRS:
        v = _diag_num(cur, p["value"])
        lim = _diag_num(cur, p["limit"])
        if v is None or lim is None or lim == 0:
            continue
        ratio = v / lim if p["type"] == "high" else (lim / max(v, 0.01) if lim > 0 else 0)
        pv = _diag_num(prior, p["value"])
        trend = ""
        if pv is not None:
            if p["type"] == "high":
                trend = "上昇傾向" if v > pv else ("低下傾向" if v < pv else "")
            else:
                trend = "低下傾向" if v < pv else ("上昇傾向" if v > pv else "")
        all_c.append({
            "label": p["label"], "fix": p["fix"], "ratio": ratio, "trend": trend,
            "is_no_limit": False, "code": p["code"], "page": p["page"],
            "detail": f"実測 {v:.1f}{p['unit']} / 上限{'' if p['type']=='high' else '(低)'} {lim:.1f}{p['unit']} ({ratio*100:.0f}%)",
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
        all_c.append({
            "label": n["label"], "fix": n["fix"], "ratio": max(0, drop),
            "trend": "低下傾向" if drop > 0.15 else "", "is_no_limit": True,
            "code": n["code"], "page": n["page"],
            "detail": f"直前平均 {avg:.2f}{n['unit']} → 実測 {v:.2f}{n['unit']} ({drop*100:.0f}%低下)",
        })

    all_c.sort(key=lambda c: -c["ratio"])
    return all_c


def _diagnose_causes(all_c):
    """しきい値に近い順(highは75%以上、no-limit系は60%以上)で上位2件を返す"""
    candidates = [c for c in all_c if (c["ratio"] >= 0.6 if c["is_no_limit"] else c["ratio"] >= 0.75)]
    candidates.sort(key=lambda c: -c["ratio"])
    return candidates[:2]


def _find_recovery(records, from_idx):
    """停止イベント以降で最初にEngineが再稼働した記録を探す(同日エクスポート内のみ)"""
    from_ts = records[from_idx].get("timestamp")
    for i in range(from_idx + 1, len(records)):
        eng = _diag_num(records[i], ENGINE_FIELD) or 0
        if eng > 0:
            try:
                t0 = datetime.fromisoformat(from_ts.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(records[i]["timestamp"].replace("Z", "+00:00"))
                mins = round((t1 - t0).total_seconds() / 60)
            except Exception:
                mins = None
            return {"timestamp_jst": t1.astimezone(JST).isoformat() if mins is not None else records[i]["timestamp"], "minutes": mins}
    return None


def detect_and_diagnose_stops(json_path: Path):
    """1日分のエクスポートJSONから、Engine停止イベントを検出し原因候補を診断する"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("data", "items", "results", "records"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []

    records = [r for r in data if r.get("timestamp")]
    records.sort(key=lambda r: r["timestamp"])
    if len(records) < 3:
        return []

    events = []
    for i in range(1, len(records)):
        prev_eng = _diag_num(records[i - 1], ENGINE_FIELD) or 0
        cur_eng = _diag_num(records[i], ENGINE_FIELD) or 0
        if prev_eng > 0 and cur_eng == 0:
            last_run_idx = i - 1
            all_c = _measure_all(records, last_run_idx)
            causes = _diagnose_causes(all_c)
            recovery = _find_recovery(records, i)
            try:
                stop_dt = datetime.fromisoformat(records[last_run_idx]["timestamp"].replace("Z", "+00:00")).astimezone(JST)
                stop_ts_jst = stop_dt.isoformat()
            except Exception:
                stop_ts_jst = records[last_run_idx]["timestamp"]
            events.append({
                "timestamp_jst": stop_ts_jst,
                "causes": causes,
                "recovery": recovery,
            })
    return events


def send_stop_diagnosis_email(events):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    notify_to = os.environ.get("NOTIFY_EMAIL")
    if not gmail_user or not gmail_pass or not notify_to:
        log("GMAIL_USER/GMAIL_APP_PASSWORD/NOTIFY_EMAILが未設定のため、メール送信をスキップします")
        return

    lines = ["Volterの停止イベントを検出しました(自動診断・簡易判定です)。", ""]
    for ev in events:
        lines.append(f"■ {ev['timestamp_jst']} JST")
        if ev["recovery"]:
            mins = ev["recovery"]["minutes"]
            lines.append(f"  {mins}分後に自動復帰" if mins is not None else "  自動復帰しました")
        else:
            lines.append("  この日のデータ内では未復帰(要確認)")
        if ev["causes"]:
            for c in ev["causes"]:
                page_str = f" (マニュアルP.{c['page']})" if c.get("page") else ""
                lines.append(f"  ・原因候補: {c['label']} [{c['code']}] {c['detail']}{page_str}")
        else:
            lines.append("  ・明確なしきい値近接は検出されませんでした。タッチスクリーンのFault code/alarmビットを確認してください")
        lines.append("")
    lines.append("※ しきい値への近さから推定した簡易診断です。断定的な故障診断ではないため、最終判断は現地確認・メーカー(Foresty Energy社)サポートと合わせて行ってください。")

    body = "\n".join(lines)
    msg = MIMEText(body)
    msg["Subject"] = f"【Volter】停止イベントを検出しました({len(events)}件)"
    msg["From"] = gmail_user
    msg["To"] = notify_to

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, [notify_to], msg.as_string())
        log(f"停止診断メールを送信しました -> {notify_to}")
    except Exception as e:
        log(f"停止診断メール送信に失敗しました: {e}")


def json_to_csv(json_path: Path, csv_path: Path):
    """1日分の生JSONを、Excelで開きやすいCSVに変換する。
    ブール値の内訳フィールド(例: 1112_0)は除外し、主要な数値フィールドのみ出力する。
    意味が確認できているフィールドはFIELD_LABELSの分かりやすい名前を使い、
    未確認のものは番号のまま列名にする。
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("data", "items", "results", "records"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list) or not data:
        log(f"json_to_csv: 変換対象データが空です ({json_path.name})")
        return

    # 主要な数値フィールド(純粋な数字のキーのみ、"1112_0"のような内訳は除外)
    numeric_keys = set()
    for rec in data:
        for k in rec.keys():
            if k.isdigit():
                numeric_keys.add(k)
    numeric_keys = sorted(numeric_keys, key=int)

    fieldnames = ["timestamp_jst", "timestamp_utc", "deviceId", "diff_pressure_Pa"] + [
        FIELD_LABELS.get(k, k) for k in numeric_keys
    ]

    rows = []
    for rec in data:
        ts_str = rec.get("timestamp")
        try:
            ts_utc = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            ts_jst = ts_utc.astimezone(JST)
        except Exception:
            ts_utc = None
            ts_jst = None

        row = {
            "timestamp_jst": ts_jst.isoformat() if ts_jst else "",
            "timestamp_utc": ts_utc.isoformat() if ts_utc else "",
            "deviceId": rec.get("deviceId", ""),
        }
        try:
            row["diff_pressure_Pa"] = round(float(rec[DP_BEFORE_FIELD]) - float(rec[DP_AFTER_FIELD]), 1)
        except Exception:
            row["diff_pressure_Pa"] = ""
        for k in numeric_keys:
            row[FIELD_LABELS.get(k, k)] = rec.get(k, "")
        rows.append((ts_utc, row))

    rows.sort(key=lambda pair: (pair[0] is None, pair[0]))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for _, row in rows:
            writer.writerow(row)
    log(f"CSV変換完了 -> {csv_path} ({len(rows)}行)")


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

    csv_export_path = raw_path.with_suffix(".csv")
    json_to_csv(raw_path, csv_export_path)

    timestamp, power_kw, energy_wh = parse_power_at_midnight(raw_path, target_utc_dt)
    max_power_kw, max_power_time = find_max_power_of_day(raw_path)
    append_log_row(target_date.isoformat(), timestamp, power_kw, energy_wh, max_power_kw, max_power_time or "")

    # クリーニングフィルターのイベント検出とメール通知
    new_events = detect_cleaning_events_from_json(raw_path)
    log(f"detected {len(new_events)} cleaning events in this export")
    all_events, new_list = merge_cleaning_events(new_events)
    save_cleaning_events(all_events)
    log(f"cleaning_events.csv: total={len(all_events)}, new={len(new_list)}")
    alerts = find_short_interval_alerts(all_events, new_list)
    if alerts:
        log(f"間隔急減アラート対象: {len(alerts)}件")
        send_alert_email(alerts)
    else:
        log("間隔急減アラートなし")

    # 停止イベントの自動原因診断とメール通知
    stop_events = detect_and_diagnose_stops(raw_path)
    log(f"detected {len(stop_events)} stop events in this export")
    if stop_events:
        send_stop_diagnosis_email(stop_events)


if __name__ == "__main__":
    main()
