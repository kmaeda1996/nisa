from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

# ---- Config ----
DATA_PATH = Path("data/nav.csv")

MUFG_BASE = "https://developer.am.mufg.jp"
MUFG_CODE_LIST = f"{MUFG_BASE}/code_list"
MUFG_LATEST_BY_FUNDCD = f"{MUFG_BASE}/fund_information_latest/fund_cd/{{fund_cd}}"

PICTET_ITRUST_URL = "https://www.pictet.co.jp/fund/iindia.html"

USER_AGENT = "nav-bot/1.1 (+https://github.com/)"

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
    return requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        },
        timeout=30,
    )


# -----------------------------
# MUFG response parsing (robust)
# -----------------------------

def looks_like_code_list_item(x: Any) -> bool:
    """code_list の1行っぽい dict か判定"""
    if not isinstance(x, dict):
        return False
    keys = set(x.keys())
    # fund_cd / fund_name があるのが一番ありがたいが、揺れも考慮
    has_cd = any(k in keys for k in ("fund_cd", "fundcd", "fundCode"))
    has_nm = any(k in keys for k in ("fund_name", "fund_nm", "fundName"))
    return has_cd and has_nm


def find_list_of_dicts(payload: Any) -> List[Dict[str, Any]]:
    """
    JSON内を再帰探索して、code_list本体っぽい「dictの配列」を拾う。
    - まず「dictの配列」で、その要素が fund_cd & fund_name を持つものを優先。
    - 見つからなければ空。
    """
    candidates: List[List[Dict[str, Any]]] = []

    def rec(node: Any) -> None:
        if isinstance(node, list):
            if node and all(isinstance(e, dict) for e in node):
                # code_listっぽいか？
                if any(looks_like_code_list_item(e) for e in node[: min(50, len(node))]):
                    candidates.append(node)  # 優先候補
            for e in node:
                rec(e)
        elif isinstance(node, dict):
            for v in node.values():
                rec(v)

    rec(payload)

    # 候補が複数ある場合、先頭50件中の「それっぽさ」が高い配列を選ぶ
    def score(lst: List[Dict[str, Any]]) -> int:
        sample = lst[: min(50, len(lst))]
        return sum(1 for e in sample if looks_like_code_list_item(e))

    if not candidates:
        return []

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def extract_value_array(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    古い実装で想定していた場所も一応チェックしつつ、
    ダメなら再帰探索にフォールバック。
    """
    # よくあるパターン
    for path in (
        ("details", "value"),
        ("detaets", "value"),
        ("data", "value"),
        ("value",),
    ):
        cur: Any = payload
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and isinstance(cur, list) and cur and all(isinstance(e, dict) for e in cur):
            return cur

    # フォールバック：中身を探索して見つける
    return find_list_of_dicts(payload)


def fetch_mufg_code_list() -> List[Dict[str, Any]]:
    r = http_get(MUFG_CODE_LIST)
    if r.status_code != 200:
        raise RuntimeError(f"MUFG code_list HTTP {r.status_code}: {r.text[:800]}")

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"MUFG code_list: JSONとしてパースできませんでした: {r.text[:800]}")

    items = extract_value_array(data)
    if not items:
        # ここで情報を出して落ちる（次に直しやすくする）
        top_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
        snippet = json.dumps(data, ensure_ascii=False)[:1200]
        raise RuntimeError(
            "MUFG code_list: code_list本体の配列を見つけられませんでした（仕様変更の可能性）\n"
            f"top_keys={top_keys}\n"
            f"json_snippet={snippet}"
        )
    return items


def score_name(name: str, keywords: List[str]) -> float:
    n = normalize(name)
    score = 0.0
    for kw in keywords:
        if normalize(kw) in n:
            score += 1.0
    # 誤爆を減らす軽い加点
    if "emaxis" in n:
        score += 0.25
    if "slim" in n:
        score += 0.25
    return score


def resolve_fund_cd(items: List[Dict[str, Any]], keywords: List[str]) -> str:
    best = None
    best_score = -1.0
    for it in items:
        name = it.get("fund_name") or it.get("fund_nm") or it.get("fundName") or ""
        if not isinstance(name, str) or not name:
            continue
        sc = score_name(name, keywords)
        if sc > best_score:
            best_score = sc
            best = it

    if best is None or best_score < 2.0:
        # 失敗時に候補を少し見える化
        preview = []
        for it in items[:50]:
            nm = it.get("fund_name") or it.get("fund_nm") or it.get("fundName")
            cd = it.get("fund_cd") or it.get("fundcd") or it.get("fundCode")
            if nm and cd:
                preview.append(f"{cd}:{nm}")
        raise RuntimeError(
            f"MUFG: fund_cd解決に失敗（best_score={best_score}, keywords={keywords}）\n"
            f"preview(先頭候補50)={preview[:20]}"
        )

    fund_cd = best.get("fund_cd") or best.get("fundcd") or best.get("fundCode")
    if not fund_cd:
        raise RuntimeError("MUFG: code_listアイテムに fund_cd が見つかりませんでした")
    return str(fund_cd)


def fetch_mufg_latest_by_fundcd(fund_cd: str) -> NavPoint:
    url = MUFG_LATEST_BY_FUNDCD.format(fund_cd=fund_cd)
    r = http_get(url)
    if r.status_code != 200:
        raise RuntimeError(f"MUFG latest HTTP {r.status_code}: {r.text[:800]}")

    data = r.json()
    arr = extract_value_array(data)
    if not arr:
        raise RuntimeError(f"MUFG latest: データが空です fund_cd={fund_cd}")

    item = arr[0]
    base_date = item.get("base_date") or item.get("baseDate")
    nav = item.get("nav")
    if not base_date or nav is None:
        raise RuntimeError(f"MUFG latest: base_date/nav が取れません fund_cd={fund_cd}")
    return NavPoint(base_date=str(base_date), nav=int(float(nav)))


# -----------------------------
# Pictet iTrust India
# -----------------------------

def fetch_pictet_itrust_india() -> NavPoint:
    r = http_get(PICTET_ITRUST_URL)
    if r.status_code != 200:
        raise RuntimeError(f"Pictet HTTP {r.status_code}")
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")

    m_date = re.search(r"基準日[:：]\s*([0-9]{4})年\s*([0-9]{1,2})月\s*([0-9]{1,2})日", text)
    m_nav = re.search(r"基準価額\s*([0-9]{1,3}(?:,[0-9]{3})*)\s*円", text)

    if not m_date or not m_nav:
        raise RuntimeError("Pictet: 基準日/基準価額を抽出できませんでした（HTML変更の可能性）")

    yyyy, mm, dd = m_date.group(1), int(m_date.group(2)), int(m_date.group(3))
    base_date = f"{yyyy}-{mm:02d}-{dd:02d}"
    nav = int(m_nav.group(1).replace(",", ""))
    return NavPoint(base_date=base_date, nav=nav)


# -----------------------------
# CSV helpers
# -----------------------------

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
    existing_dates = read_existing_dates(DATA_PATH)

    # ---- MUFG 2本 ----
    code_list = fetch_mufg_code_list()
    mufg_values: Dict[str, NavPoint] = {}
    for label, keywords in MUFG_TARGETS:
        fund_cd = resolve_fund_cd(code_list, keywords)
        mufg_values[label] = fetch_mufg_latest_by_fundcd(fund_cd)

    # ---- Pictet iTrust ----
    itrust = fetch_pictet_itrust_india()

    # 日付はまず iTrust の基準日（揃わない場合があるため）
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