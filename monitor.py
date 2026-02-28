#!/usr/bin/env python3
"""
RELIEF Ticket リセールチケット監視 & LINE通知システム

指定アーティストのリセールチケットが出品されたら即座にLINEで通知する。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv(Path(__file__).parent / ".env")

# ─── 設定 ───────────────────────────────────────────
BASE_URL = "https://relief-ticket.jp"

ARTIST_IDS = {
    "Travis Japan": 38,
    "SixTONES": 40,
    "King & Prince": 41,
    "中島健人": 42,
    "ジュニア": 15,
}

ARTISTS = [a.strip() for a in os.getenv("ARTISTS", "Travis Japan,SixTONES").split(",")]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

STATE_FILE = Path(__file__).parent / "state.json"
DEBUG_DIR = Path(__file__).parent / "debug"


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ─── 状態管理 ────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"notified": {}}


def save_state(state: dict):
    state["last_check"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ─── LINE通知 ────────────────────────────────────────
def send_line(message: str):
    log(f"通知送信: {message[:60]}...")

    if not LINE_TOKEN or not LINE_USER_ID:
        log("[LINE未設定] .envを設定してください")
        return

    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_TOKEN}",
            },
            json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        if resp.status_code == 200:
            log("[LINE送信OK]")
        else:
            log(f"[LINE送信失敗] {resp.status_code}: {resp.text}")
    except Exception as e:
        log(f"[LINE送信エラー] {e}")


# ─── macOS デスクトップ通知（LINE未設定時のフォールバック） ──
def notify_desktop(title: str, message: str):
    try:
        os.system(
            f'''osascript -e 'display notification "{message}" with title "{title}" sound name "Glass"' '''
        )
    except Exception:
        pass


# ─── スクレイピング ───────────────────────────────────

async def get_event_urls(page, artist_name: str) -> list[dict]:
    """アーティストのイベント一覧ページからイベントURLを取得"""
    artist_id = ARTIST_IDS.get(artist_name)
    if not artist_id:
        log(f"  アーティストID不明: {artist_name}")
        return []

    url = f"{BASE_URL}/events/artist/{artist_id}"
    log(f"  イベント一覧: {url}")

    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(1500)

    if DEBUG:
        DEBUG_DIR.mkdir(exist_ok=True)
        await page.screenshot(path=str(DEBUG_DIR / f"events_{artist_id}.png"), full_page=True)

    events = []
    seen = set()
    links = await page.query_selector_all("a")

    for link in links:
        href = await link.get_attribute("href") or ""
        # /events/artist/{id}/{event_id} パターン
        if re.match(rf"/events/artist/{artist_id}/\d+", href) and href not in seen:
            seen.add(href)
            text = (await link.inner_text()).strip().split("\n")[0]
            events.append({
                "name": text,
                "url": f"{BASE_URL}{href}",
                "path": href,
            })
            log(f"    イベント: {text}")

    return events


async def check_event_tickets(page, event: dict) -> list[dict]:
    """
    イベントページで各公演のリセールチケット在庫を確認する。

    在庫あり時のHTML構造:
      div.perform-list (text-mutedなし)
        div.lead -> 日時
        p -> 会場
        select.ticket-select -> 枚数選択
        form.ticket-form -> 購入フォーム
        button/input[type=submit] -> 「購入手続きへ」

    在庫なし時:
      div.perform-list.text-muted
        div.lead -> 日時
        p -> 会場
        (selectやformなし)
    """
    log(f"  チケット確認: {event['name']}")

    await page.goto(event["url"], wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    if DEBUG:
        DEBUG_DIR.mkdir(exist_ok=True)
        safe = re.sub(r"[^\w]", "_", event["name"])[:30]
        await page.screenshot(path=str(DEBUG_DIR / f"tickets_{safe}.png"), full_page=True)
        (DEBUG_DIR / f"tickets_{safe}.html").write_text(await page.content())

    content = await page.content()
    soup = BeautifulSoup(content, "html.parser")

    available = []

    # 方法1: .ticket-select が存在する公演を探す（最も確実）
    selects = soup.select(".ticket-select")
    for sel in selects:
        perform_div = sel.find_parent("div", class_="perform-list")
        if not perform_div:
            continue

        date_el = perform_div.select_one(".lead")
        venue_el = perform_div.select_one("p")
        date_text = date_el.get_text(strip=True) if date_el else "不明"
        venue_text = venue_el.get_text(strip=True) if venue_el else "不明"

        # 選択可能な枚数を取得
        options = sel.select("option[data-ticket-no]")
        ticket_nums = [opt.get_text(strip=True) for opt in options]

        available.append({
            "date": date_text,
            "venue": venue_text,
            "tickets": ticket_nums,
            "method": "select",
        })

    # 方法2: .perform-list で text-muted でないものを探す
    if not available:
        for div in soup.select(".perform-list"):
            classes = div.get("class", [])
            if "text-muted" not in classes:
                date_el = div.select_one(".lead")
                venue_el = div.select_one("p")
                date_text = date_el.get_text(strip=True) if date_el else "不明"
                venue_text = venue_el.get_text(strip=True) if venue_el else "不明"

                available.append({
                    "date": date_text,
                    "venue": venue_text,
                    "tickets": [],
                    "method": "active",
                })

    # 方法3: 「購入手続きへ」ボタンの存在を確認
    if not available:
        for btn in soup.select("button, input[type=submit], a.btn"):
            text = btn.get_text(strip=True)
            if "購入手続き" in text:
                available.append({
                    "date": "不明",
                    "venue": "不明",
                    "tickets": [],
                    "method": "button",
                })

    return available


# ─── メインチェック ────────────────────────────────────
async def run_check() -> list[dict]:
    state = load_state()
    new_findings = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()

        try:
            for artist in ARTISTS:
                log(f"--- {artist} ---")

                events = await get_event_urls(page, artist)
                if not events:
                    log(f"  イベントなし")
                    continue

                for event in events:
                    try:
                        tickets = await check_event_tickets(page, event)
                    except Exception as e:
                        log(f"  エラー ({event['name']}): {e}")
                        continue

                    if not tickets:
                        log(f"    在庫なし")
                        continue

                    # 新規チケットかチェック
                    for t in tickets:
                        key = f"{event['path']}|{t['date']}|{t['venue']}"

                        if key not in state["notified"]:
                            state["notified"][key] = datetime.now().isoformat()
                            new_findings.append({
                                "artist": artist,
                                "event_name": event["name"],
                                "event_url": event["url"],
                                **t,
                            })
        finally:
            await browser.close()

    # 通知
    for f in new_findings:
        ticket_info = f"({', '.join(f['tickets'])})" if f["tickets"] else ""
        msg = (
            f"🎫 リセールチケット出品!\n\n"
            f"【{f['artist']}】\n"
            f"{f['event_name']}\n"
            f"📅 {f['date']}\n"
            f"📍 {f['venue']}\n"
            f"🎟 {ticket_info}\n\n"
            f"▶ 購入ページ:\n{f['event_url']}"
        )
        send_line(msg)
        notify_desktop("RELIEF Ticket", f"{f['artist']} {f['date']} {f['venue']}")

    if new_findings:
        log(f"=== {len(new_findings)}件の新規リセール検出! ===")
    else:
        log("変更なし")

    # 古い通知履歴を掃除（30日以上前）
    cutoff = datetime.now().timestamp() - 30 * 86400
    state["notified"] = {
        k: v for k, v in state["notified"].items()
        if datetime.fromisoformat(v).timestamp() > cutoff
    }

    save_state(state)
    return new_findings


async def main():
    once = "--once" in sys.argv

    log("=" * 50)
    log("RELIEF Ticket リセールチケット監視")
    log(f"対象: {', '.join(ARTISTS)}")
    log(f"モード: {'単発' if once else f'ループ({CHECK_INTERVAL}秒間隔)'}")
    log(f"LINE: {'設定済み' if LINE_TOKEN and LINE_USER_ID else '未設定'}")
    log("=" * 50)

    if once:
        # GitHub Actions 用: 1回だけ実行
        findings = await run_check()
        # 検出があった場合は exit code 0、なくても 0
        return

    while True:
        try:
            await run_check()
        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"エラー: {e}")
            traceback.print_exc()

        log(f"次回チェック: {CHECK_INTERVAL}秒後")
        try:
            await asyncio.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            break

    log("監視終了")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n監視を終了しました")
