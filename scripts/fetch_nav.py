from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

# ---- Config ----
TZ_JST = "Asia/Tokyo"  # 表示用。GitHub ActionsはUTCだが日付は取得元の基準日を使う。
DATA_PATH = Path("data/nav.csv")

MUFG_BASE = "https://developer.am.mufg.jp"
MUFG_CODE_LIST = f"{MUFG_BASE}/code_list"
MUFG_LATEST_BY_FUNDCD = f"{MUFG_BASE}/fund_information_latest/fund_cd/{{fund_cd}}"

PICTET_ITRUST_URL = "https://www.pictet.co.jp/fund/iindia.html"

USER_AGENT = "nav-bot/1.0 (+https://github.com/)"

# 対象銘柄（MUFGは code_list の fund_name で解決）
MUFG_TARGETS = [
    ("eMAXIS Slim 全世界株式（オール・カントリー）", ["emaxis", "slim", "全世界", "オール", "カントリー"]),
    ("eMAXIS Slim 米国株式（S&P500）", ["emaxis", "slim", "米国", "s&p", "500"]),
]


@dataclass(frozen=True)
class NavPoint:
    base_date: str  # YYYY-MM-DD
    nav: int


def normalize(s: str) -> str:
    s = s.lower()
    s = s.replace("＆", "&").replace("　", " ")
    s = re.sub(r"\s+", "", s)
    return s


def http_get(url: str) -> requests.Response:
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)


def pick_value_array(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    MUFG APIのレスポンスは {details:{value:[...]}} などの形が多い。
    揺れがあっても拾えるようにする。
    """
    v = None
    if isinstance(payload.get("details"), dict):
        v = payload["details"].get("value")
    if v is None and isinstance(payload.get("detaets"), dict):  # 実装例で揺れが見られるケース対策
        v = payload["detaets"].get("value")
    if v is None:
        v = payload.get("value")
    return v if isinstance(v, list) else []


def fetch_mufg_code_list() -> List[Dict[str, Any]]:
    r = http_get(MUFG_CODE_LIST)
    if r.status_code != 200:
        raise RuntimeError(f"MUFG code_list HTTP {r.status_code}: {r.text[:500]}")
    data = r.json()
    items = pick_value_array(data)
    if not items:
        raise RuntimeError("MUFG code_list: value配列が空でした（仕様変更の可能性）")
    return items


def score_name(name: str, keywords: List[str]) -> float:
    n = normalize(name)
    score = 0.0
    for kw in keywords:
        if normalize(kw) in n:
            score += 1.0
    # 「emaxis」「slim」を含むなら少し加点（誤爆を減らす）
    if "emaxis" in n:
        score += 0.25
    if "slim" in n:
        score += 0.25
    return score


def resolve_fund_cd(items: List[Dict[str, Any]], keywords: List[str]) -> str:
    best = None
    best_score = -1.0
    for it in items:
        name = it.get("fund_name") or it.get("fund_nm") or ""
        if not isinstance(name, str) or not name:
            continue
        sc = score_name(name, keywords)
        if sc > best_score:
            best_score = sc
            best = it

    # 最低2ヒットくらいないと危険なのでガード
    if best is None or best_score < 2.0:
        raise RuntimeError(f"MUFG: fund_cd解決に失敗（best_score={best_score}, keywords={keywords}）")

    fund_cd = best.get("fund_cd") or best.get("fundcd") or best.get("fundCode")
    if not fund_cd:
        raise RuntimeError("MUFG: code_listアイテムに fund_cd が見つかりませんでした")
    return str(fund_cd)


def fetch_mufg_latest_by_fundcd(fund_cd: str) -> NavPoint:
    url = MUFG_LATEST_BY_FUNDCD.format(fund_cd=fund_cd)
    r = http_get(url)
    if r.status_code != 200:
        raise RuntimeError(f"MUFG latest HTTP {r.status_code}: {r.text[:500]}")
    data = r.json()
    arr = pick_value_array(data)
    if not arr:
        raise RuntimeError(f"MUFG latest: データが空です fund_cd={fund_cd}")
    item = arr[0]
    base_date = item.get("base_date") or item.get("baseDate")
    nav = item.get("nav")
    if not base_date or nav is None:
        raise RuntimeError(f"MUFG latest: base_date/nav が取れません fund_cd={fund_cd}")
    return NavPoint(base_date=str(base_date), nav=int(float(nav)))


def fetch_pictet_itrust_india() -> NavPoint:
    r = http_get(PICTET_ITRUST_URL)
    if r.status_code != 200:
        raise RuntimeError(f"Pictet HTTP {r.status_code}")
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")

    # 例: 基準日: 2026年02月17日
    m_date = re.search(r"基準日[:：]\s*([0-9]{4})年\s*([0-9]{1,2})月\s*([0-9]{1,2})日", text)
    # 例: 基準価額 23,303円
    m_nav = re.search(r"基準価額\s*([0-9]{1,3}(?:,[0-9]{3})*)\s*円", text)

    if not m_date or not m_nav:
        raise RuntimeError("Pictet: 基準日/基準価額を抽出できませんでした（HTML変更の可能性）")

    yyyy, mm, dd = m_date.group(1), int(m_date.group(2)), int(m_date.group(3))
    base_date = f"{yyyy}-{mm:02d}-{dd:02d}"
    nav = int(m_nav.group(1).replace(",", ""))
    return NavPoint(base_date=base_date, nav=nav)


def ensure_csv_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "date",
            "eMAXIS Slim 全世界株式（オール・カントリー）",
            "eMAXIS Slim 米国株式（S&P500）",
            "iTrustインド株式",
        ])


def read_existing_dates(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return {row["date"] for row in r if row.get("date")}


def append_row(path: Path, row: Dict[str, Any]) -> None:
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        w.writerow(row)


def sort_csv_by_date(path: Path) -> None:
    with path.open("r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        rows = list(r)
        fieldnames = r.fieldnames or []

    def parse_d(s: str) -> date:
        return datetime.strptime(s, "%Y-%m-%d").date()

    rows.sort(key=lambda x: parse_d(x["date"]))

    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    tmp.replace(path)


def main() -> None:
    ensure_csv_header(DATA_PATH)

    # 既存日付を読み込み（重複防止）
    existing_dates = read_existing_dates(DATA_PATH)

    # ---- MUFG 2本 ----
    code_list = fetch_mufg_code_list()
    mufg_values: Dict[str, NavPoint] = {}
    for label, keywords in MUFG_TARGETS:
        fund_cd = resolve_fund_cd(code_list, keywords)
        mufg_values[label] = fetch_mufg_latest_by_fundcd(fund_cd)

    # ---- Pictet iTrust ----
    itrust = fetch_pictet_itrust_india()

    # 日付は「基準日」が揃わない可能性があるので、まずは iTrust の基準日を採用
    base_date = itrust.base_date

    if base_date in existing_dates:
        print(f"Already exists: {base_date} (skip)")
        return

    row = {
        "date": base_date,
        "eMAXIS Slim 全世界株式（オール・カントリー）": mufg_values[MUFG_TARGETS[0][0]].nav,
        "eMAXIS Slim 米国株式（S&P500）": mufg_values[MUFG_TARGETS[1][0]].nav,
        "iTrustインド株式": itrust.nav,
    }
    append_row(DATA_PATH, row)
    sort_csv_by_date(DATA_PATH)
    print(f"Appended: {row}")


if __name__ == "__main__":
    main()