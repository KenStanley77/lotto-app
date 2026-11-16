#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用台灣彩券官方資料更新大樂透選號器內的開獎號碼（GitHub Actions 版）。

特色
----
* 只依賴 Python 標準函式庫（urllib 取數，必要時退回 curl），在 Linux runner 與
  Windows 本機都能執行。
* 只改寫 HTML 內 ``const DRAWS = [...];`` 這一行，其餘內容不動。
* ``--check-only`` 只抓資料、比對差異、不寫入任何檔案，適合驗證 API 是否可用。
* 沒有新資料時不輸出多餘訊息、不產生變更，讓 workflow 可以據此跳過 commit。

用法
----
    python update_lotto.py --check-only --verbose
    python update_lotto.py --verbose
    python update_lotto.py --html index.html --data data/lotto_history.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple, Sequence

APP_NAME = "index.html"
DATA_DIR = "data"
HISTORY_NAME = "lotto_history.json"
STATE_NAME = "last_run.json"

DATA_PREFIX = "const DRAWS = "
DATA_SUFFIX = ";"
WINDOW_YEARS = 3

API_URL = (
    "https://api.taiwanlottery.com/TLCAPIWeB/Lottery/Lotto649Result"
    "?month={month}&pageNum=1&pageSize=200"
)
USER_AGENT = "Mozilla/5.0 (compatible; LottoSync/1.0)"
HTTP_TIMEOUT_SECONDS = 30
FETCH_RETRIES = 2

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Draw(NamedTuple):
    """一期開獎紀錄。"""

    date: str
    numbers: tuple[int, ...]
    special: int
    period: int


def log_line(message: str) -> str:
    """加上時間戳的單行訊息。"""
    return "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)


def atomic_write_text(path: Path, text: str) -> None:
    """先寫暫存檔再置換，避免中途失敗留下半個檔案。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def minus_years(day: date, years: int) -> date:
    """往前推 N 年，遇到 2/29 自動退到 2/28。"""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def shift_month(month: str, delta: int) -> str:
    """YYYY-MM 加減月數。"""
    year, mon = (int(part) for part in month.split("-"))
    index = year * 12 + (mon - 1) + delta
    return "%04d-%02d" % (index // 12, index % 12 + 1)


def month_range(first: str, last: str) -> Iterator[str]:
    """含頭含尾逐月產出 YYYY-MM。"""
    current = first
    while current <= last:
        yield current
        current = shift_month(current, 1)


# --------------------------------------------------------------------------
# HTML 資料行
# --------------------------------------------------------------------------
def parse_draws_line(line: str) -> list[Draw]:
    """解析單一行 ``const DRAWS = [...];``。"""
    if not line.startswith(DATA_PREFIX) or not line.endswith(DATA_SUFFIX):
        raise ValueError("資料行格式不符：%s" % line[:60])
    rows = json.loads(line[len(DATA_PREFIX): -len(DATA_SUFFIX)])
    draws: list[Draw] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError("資料列格式不符：%r" % (row,))
        draws.append(
            Draw(
                date=str(row[0]),
                numbers=tuple(int(n) for n in row[1]),
                special=int(row[2]),
                period=int(row[3]),
            )
        )
    return draws


def render_draws_line(draws: Sequence[Draw]) -> str:
    """把資料轉回單一行，格式與原檔一致（緊湊、中文不轉義）。"""
    rows = [[d.date, list(d.numbers), d.special, d.period] for d in draws]
    return DATA_PREFIX + json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + DATA_SUFFIX


def locate_data_line(lines: Sequence[str]) -> int:
    """回傳資料行的索引；找不到或有兩行以上就報錯。"""
    hits = [i for i, line in enumerate(lines) if line.startswith(DATA_PREFIX)]
    if len(hits) != 1:
        raise RuntimeError(
            "HTML 內應有恰好一行 %s，實際找到 %d 行" % (DATA_PREFIX.strip(), len(hits))
        )
    return hits[0]


# --------------------------------------------------------------------------
# 驗證 / 合併
# --------------------------------------------------------------------------
def validate(draw: Draw) -> str | None:
    """回傳錯誤訊息；合法則回 None。"""
    if not DATE_RE.match(draw.date):
        return "日期格式錯誤：%s" % draw.date
    nums = draw.numbers
    if len(nums) != 6 or len(set(nums)) != 6:
        return "主號個數或重複異常：%s" % (nums,)
    if list(nums) != sorted(nums):
        return "主號未升冪：%s" % (nums,)
    if not all(1 <= n <= 49 for n in nums):
        return "主號超出 1-49：%s" % (nums,)
    if not 1 <= draw.special <= 49:
        return "特別號超出 1-49：%s" % draw.special
    if draw.special in nums:
        return "特別號與主號重複：%s" % (nums,)
    if draw.period <= 0:
        return "期別不合法：%s" % draw.period
    return None


def merge_draws(existing: Iterable[Draw], incoming: Iterable[Draw]) -> list[Draw]:
    """以期別為鍵去重（新資料優先），再依日期、期別排序。"""
    table: dict[int, Draw] = {}
    for draw in existing:
        table[draw.period] = draw
    for draw in incoming:
        table[draw.period] = draw
    return sorted(table.values(), key=lambda d: (d.date, d.period))


def window_draws(draws: Sequence[Draw], today: date) -> list[Draw]:
    """取出滾動近三年視窗。"""
    cutoff = minus_years(today, WINDOW_YEARS).isoformat()
    return [d for d in draws if d.date >= cutoff]


def to_draw(row: object) -> Draw | None:
    """把 API 的一列轉成 Draw；無法轉換回 None。"""
    if not isinstance(row, dict):
        return None
    period = row.get("period")
    raw_date = str(row.get("lotteryDate") or "")
    nums = row.get("drawNumberSize") or []
    if period is None or len(nums) != 7 or not DATE_RE.match(raw_date[:10]):
        return None
    try:
        numbers = tuple(sorted(int(n) for n in nums[:6]))
        special = int(nums[6])
    except (TypeError, ValueError):
        return None
    return Draw(date=raw_date[:10], numbers=numbers, special=special, period=int(period))


# --------------------------------------------------------------------------
# 取數：優先 urllib，SSL 或網路異常時退回 curl
# --------------------------------------------------------------------------
def http_get(url: str) -> str:
    """取得網址內容（純文字）。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", "replace")


def curl_get(url: str) -> str:
    """以 curl 取得內容，作為 urllib 失敗時的備援。"""
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        raise RuntimeError("找不到 curl，且 urllib 取數失敗")
    proc = subprocess.run(
        [curl, "-sS", "-m", str(HTTP_TIMEOUT_SECONDS), url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=HTTP_TIMEOUT_SECONDS + 15,
    )
    if proc.returncode != 0:
        raise RuntimeError("curl 回傳 %d：%s" % (proc.returncode, (proc.stderr or "").strip()[:200]))
    return proc.stdout


def fetch_month(month: str, log: list[str]) -> list[Draw]:
    """抓取並解析單一月份；失敗回傳空清單並記錄訊息（不中斷整體流程）。"""
    url = API_URL.format(month=month)
    last_error = ""

    for attempt in range(1, FETCH_RETRIES + 1):
        body = ""
        try:
            body = http_get(url)
        except (urllib.error.URLError, ssl.SSLError, OSError, ValueError) as exc:
            last_error = "urllib 取數失敗：%s" % exc
            try:
                body = curl_get(url)
            except (RuntimeError, OSError, subprocess.SubprocessError) as fallback_exc:
                last_error = "%s；%s" % (last_error, fallback_exc)

        if body.strip():
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                last_error = "回應不是合法 JSON：%s" % exc
            else:
                rows = (payload.get("content") or {}).get("lotto649Res") or []
                if not isinstance(rows, list):
                    last_error = "回應結構不符（content.lotto649Res 不是陣列）"
                else:
                    parsed: list[Draw] = []
                    for row in rows:
                        draw = to_draw(row)
                        if draw is None:
                            continue
                        problem = validate(draw)
                        if problem:
                            log.append(log_line("略過無效資料 %s：%s" % (month, problem)))
                            continue
                        parsed.append(draw)
                    log.append(log_line("取得 %s：%d 期" % (month, len(parsed))))
                    return parsed

        if attempt < FETCH_RETRIES:
            time.sleep(1.5)

    log.append(log_line("月份 %s 取數失敗（%s）" % (month, last_error)))
    return []


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="更新選號器內的開獎號碼（GitHub Actions 版）")
    parser.add_argument("--html", default=APP_NAME, help="HTML 路徑（預設 index.html）")
    parser.add_argument("--data", default=os.path.join(DATA_DIR, HISTORY_NAME),
                        help="歷史封存 JSON 路徑（預設 data/lotto_history.json）")
    parser.add_argument("--today", help="假裝今天是這個日期 YYYY-MM-DD（測試用）")
    parser.add_argument("--check-only", action="store_true", help="只抓資料與比對，不寫入任何檔案")
    parser.add_argument("--verbose", action="store_true", help="不論有無變更都輸出摘要")
    return parser.parse_args(argv)


def run(args: argparse.Namespace, log: list[str]) -> dict:
    root = Path(__file__).resolve().parent
    html_path = Path(args.html)
    if not html_path.is_absolute():
        html_path = root / html_path
    if not html_path.is_file():
        raise RuntimeError("找不到 HTML：%s" % html_path)

    data_path = Path(args.data)
    if not data_path.is_absolute():
        data_path = root / data_path
    state_path = data_path.with_name(STATE_NAME)

    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()

    text = html_path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    index = locate_data_line(lines)
    html_draws = parse_draws_line(lines[index])
    log.append(log_line("HTML 現有 %d 期（%s ~ %s）" % (len(html_draws), html_draws[0].date, html_draws[-1].date)))

    archived: list[Draw] = []
    if data_path.is_file():
        try:
            payload = json.loads(data_path.read_text(encoding="utf-8"))
            archived = [d for d in (to_stored(row) for row in payload.get("draws", [])) if d]
            log.append(log_line("封存檔現有 %d 期" % len(archived)))
        except (json.JSONDecodeError, OSError, AttributeError) as exc:
            log.append(log_line("封存檔讀取失敗，改以 HTML 為基礎重建：%s" % exc))

    known = merge_draws(archived, html_draws)
    if not known:
        raise RuntimeError("沒有任何既有資料可推算補齊起點")

    first_month = shift_month(known[-1].date[:7], -1)
    last_month = today.strftime("%Y-%m")
    months = list(month_range(first_month, last_month))
    log.append(log_line("預計抓取月份：%s ~ %s（共 %d 個月）" % (first_month, last_month, len(months))))

    fetched: list[Draw] = []
    failed: list[str] = []
    for month in months:
        got = fetch_month(month, log)
        if got:
            fetched.extend(got)
        else:
            failed.append(month)

    merged = merge_draws(known, fetched)
    windowed = window_draws(merged, today)
    if not windowed:
        raise RuntimeError("近三年視窗內沒有任何資料，請確認取數是否成功")

    new_line = render_draws_line(windowed)
    changed = new_line != lines[index]
    added = len(merged) - len(known)
    log.append(log_line("新增 %d 期；已知總計 %d 期；視窗 %d 期（%s ~ %s）" % (
        added, len(merged), len(windowed), windowed[0].date, windowed[-1].date)))

    if failed:
        log.append(log_line("警告：以下月份取數失敗，本次未納入：%s" % ", ".join(failed)))

    if not args.check_only:
        if changed:
            lines[index] = new_line
            atomic_write_text(html_path, newline.join(lines))
            log.append(log_line("已寫回 HTML：%s" % html_path))
        atomic_write_text(data_path, json.dumps(
            {"schema": 1, "count": len(merged),
             "draws": [[d.date, list(d.numbers), d.special, d.period] for d in merged]},
            ensure_ascii=False, indent=0))
        atomic_write_text(state_path, json.dumps({
            "changed": changed, "added": added, "total": len(merged),
            "windowCount": len(windowed), "windowFrom": windowed[0].date,
            "windowTo": windowed[-1].date, "failedMonths": failed,
            "at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=1))

    return {
        "changed": changed, "added": added, "total": len(merged),
        "windowCount": len(windowed), "windowFrom": windowed[0].date,
        "windowTo": windowed[-1].date, "failedMonths": failed,
        "monthsTotal": len(months),
    }


def to_stored(row: object) -> Draw | None:
    """把封存檔的一列轉成 Draw。"""
    if not isinstance(row, list) or len(row) != 4:
        return None
    try:
        return Draw(date=str(row[0]), numbers=tuple(int(n) for n in row[1]),
                    special=int(row[2]), period=int(row[3]))
    except (TypeError, ValueError):
        return None


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    log: list[str] = []
    try:
        result = run(args, log)
    except Exception as exc:  # noqa: BLE001 - 明確回報給 Actions log
        print(log_line("更新失敗：%s: %s" % (type(exc).__name__, exc)), file=sys.stderr)
        for line in log:
            print(line, file=sys.stderr)
        return 1

    if args.verbose or args.check_only or result["changed"] or result["failedMonths"]:
        for line in log:
            print(line)
        print(log_line("完成：%s，新增 %d 期，視窗 %d 期（%s ~ %s）" % (
            "已更新" if result["changed"] else "無變更",
            result["added"], result["windowCount"],
            result["windowFrom"], result["windowTo"])))

    if result["failedMonths"] and len(result["failedMonths"]) == result["monthsTotal"]:
        # 所有月份都取數失敗 → 讓 CI 明確失敗，避免靜默通過
        print(log_line("所有月份取數皆失敗，結束碼 1"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())