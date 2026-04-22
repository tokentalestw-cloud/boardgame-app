from __future__ import annotations

import os
import sqlite3
import shutil
from pathlib import Path
from collections import defaultdict
from datetime import date
from typing import Optional
from html import escape
import json
import io
from urllib.parse import quote_plus

import pandas as pd
import plotly.express as px
from fastapi import FastAPI, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgres")

if USE_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg import IntegrityError as PgIntegrityError
else:
    psycopg = None
    dict_row = None
    PgIntegrityError = Exception

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "boardgame_records.db"
GAME_IMAGE_DIR = DATA_DIR / "game_images"
MEMBER_IMAGE_DIR = DATA_DIR / "member_images"
GAME_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
MEMBER_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

APP_TITLE = "桌遊遊玩紀錄系統｜強化版"
app = FastAPI(title=APP_TITLE)
app.mount("/game_images", StaticFiles(directory=str(GAME_IMAGE_DIR)), name="game_images")
app.mount("/member_images", StaticFiles(directory=str(MEMBER_IMAGE_DIR)), name="member_images")


def get_conn():
    if USE_POSTGRES:
        return psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
            connect_timeout=5,
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql: str) -> str:
    return sql if USE_POSTGRES else sql.replace("%s", "?")


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def unique_error(exc: Exception) -> bool:
    return isinstance(exc, (sqlite3.IntegrityError, PgIntegrityError))


def parse_csv_names(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def parse_optional_float(text: str) -> Optional[float]:
    text = (text or "").strip()
    return float(text) if text else None


def load_notes(raw: str | None) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return [line.strip("- •	 ") for line in raw.splitlines() if line.strip()]


def dump_notes(notes: list[str]) -> str:
    return json.dumps([str(x).strip() for x in notes if str(x).strip()], ensure_ascii=False)


def web_image_path(db_path: str) -> str:
    if not db_path:
        return ""
    p = Path(db_path)
    if p.parent.name == "game_images":
        return f"/game_images/{p.name}"
    if p.parent.name == "member_images":
        return f"/member_images/{p.name}"
    return ""


def urlq(value: str) -> str:
    return quote_plus(value or "")


def copy_upload_to_library(upload: Optional[UploadFile], target_dir: Path, base_name: str) -> str:
    if upload is None or not upload.filename:
        return ""
    ext = Path(upload.filename).suffix.lower() or ".png"
    safe_name = "".join(ch for ch in base_name if ch.isalnum() or ch in "-_ ").strip().replace(" ", "_")
    target = target_dir / f"{safe_name}{ext}"
    with target.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return str(target)


def preserve_upload_or_existing(upload: Optional[UploadFile], target_dir: Path, base_name: str, existing_path: str) -> str:
    if upload is None or not upload.filename:
        return existing_path or ""
    return copy_upload_to_library(upload, target_dir, base_name)


def create_tables() -> None:
    conn = get_conn()
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                image_path TEXT DEFAULT '',
                bgg_score DOUBLE PRECISION,
                user_rating DOUBLE PRECISION,
                last_play_date TEXT DEFAULT '',
                play_count INTEGER DEFAULT 0,
                notes TEXT DEFAULT '[]',
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                favorite_types TEXT DEFAULT '',
                image_path TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS play_records (
                id BIGSERIAL PRIMARY KEY,
                play_date TEXT NOT NULL,
                game_id BIGINT,
                game_name TEXT NOT NULL,
                game_type TEXT NOT NULL,
                players TEXT NOT NULL,
                winners TEXT NOT NULL
            )""")
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                image_path TEXT DEFAULT '',
                bgg_score REAL,
                user_rating REAL,
                last_play_date TEXT DEFAULT '',
                play_count INTEGER DEFAULT 0,
                notes TEXT DEFAULT '[]',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                favorite_types TEXT DEFAULT '',
                image_path TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS play_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                play_date TEXT NOT NULL,
                game_id INTEGER,
                game_name TEXT NOT NULL,
                game_type TEXT NOT NULL,
                players TEXT NOT NULL,
                winners TEXT NOT NULL
            )""")
    if USE_POSTGRES:
        cur.execute("ALTER TABLE games ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT '[]'")
    else:
        cur.execute("PRAGMA table_info(games)")
        cols = [row[1] if isinstance(row, tuple) else row["name"] for row in cur.fetchall()]
        if "notes" not in cols:
            cur.execute("ALTER TABLE games ADD COLUMN notes TEXT DEFAULT '[]'")
    conn.commit()
    conn.close()


def sync_game_stats() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE games SET last_play_date = '', play_count = 0")
    cur.execute("""
        SELECT game_name, COUNT(*) AS play_count, MAX(play_date) AS last_play_date
        FROM play_records GROUP BY game_name
    """)
    for row in cur.fetchall():
        name = row["game_name"] if not isinstance(row, tuple) else row[0]
        cnt = row["play_count"] if not isinstance(row, tuple) else row[1]
        last = row["last_play_date"] if not isinstance(row, tuple) else row[2]
        cur.execute(q("UPDATE games SET play_count = %s, last_play_date = %s WHERE name = %s"), (cnt, last, name))
    conn.commit()
    conn.close()


def category_options(conn) -> list[str]:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT category FROM games WHERE category <> '' ORDER BY category")
    return [row[0] if isinstance(row, tuple) else row["category"] for row in cur.fetchall()]


def summary_stats(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM games")
    row = cur.fetchone()
    games = row["c"] if not isinstance(row, tuple) else row[0]
    cur.execute("SELECT COUNT(*) AS c FROM members")
    row = cur.fetchone()
    members = row["c"] if not isinstance(row, tuple) else row[0]
    cur.execute("SELECT COUNT(*) AS c FROM play_records")
    row = cur.fetchone()
    records = row["c"] if not isinstance(row, tuple) else row[0]
    cur.execute("SELECT COALESCE(MAX(play_date), '') AS latest FROM play_records")
    latest_row = cur.fetchone()
    latest = latest_row["latest"] if not isinstance(latest_row, tuple) else latest_row[0]
    return {"games": games, "members": members, "records": records, "latest": latest or "尚無紀錄"}

def top_game_rankings(conn, limit: int = 8) -> list[dict]:
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute("SELECT game_name, COUNT(*) AS plays FROM play_records GROUP BY game_name ORDER BY plays DESC, game_name ASC LIMIT %s", (limit,))
    else:
        cur.execute(f"SELECT game_name, COUNT(*) AS plays FROM play_records GROUP BY game_name ORDER BY plays DESC, game_name ASC LIMIT {limit}")
    return [dict(row) for row in cur.fetchall()]


def top_member_rankings(conn, limit: int = 8) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT players, winners FROM play_records")
    games = defaultdict(int)
    wins = defaultdict(int)
    for row in cur.fetchall():
        for p in parse_csv_names(row["players"]):
            games[p] += 1
        for w in parse_csv_names(row["winners"]):
            wins[w] += 1
    items = [{"player": p, "wins": wins[p], "games": games[p], "win_rate": round(wins[p] / games[p] * 100, 1)} for p in games]
    items.sort(key=lambda x: (-x["win_rate"], -x["wins"], -x["games"], x["player"]))
    return items[:limit]


def get_member_best_type(conn, member_name: str) -> tuple[Optional[str], Optional[str]]:
    cur = conn.cursor()
    cur.execute("SELECT game_type, players, winners FROM play_records")
    stats = {}
    for row in cur.fetchall():
        gtype = row["game_type"] or "未分類"
        players = parse_csv_names(row["players"])
        winners = parse_csv_names(row["winners"])
        if member_name not in players:
            continue
        stats.setdefault(gtype, {"games": 0, "wins": 0})
        stats[gtype]["games"] += 1
        if member_name in winners:
            stats[gtype]["wins"] += 1
    best_type, best_rate, best_games = None, 0.0, 0
    for gtype, stat in stats.items():
        if stat["games"] == 0:
            continue
        rate = stat["wins"] / stat["games"]
        if rate > best_rate or (rate == best_rate and stat["games"] > best_games):
            best_type, best_rate, best_games = gtype, rate, stat["games"]
    return (best_type, f"{best_rate * 100:.1f}%") if best_type else (None, None)


def get_member_summary(conn, member_name: str) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT play_date, game_name, players, winners FROM play_records ORDER BY play_date DESC, id DESC")
    games_played = wins = 0
    recent_games, seen = [], set()
    for row in cur.fetchall():
        players = parse_csv_names(row["players"])
        winners = parse_csv_names(row["winners"])
        if member_name in players:
            games_played += 1
            if member_name in winners:
                wins += 1
            if row["game_name"] not in seen and len(recent_games) < 5:
                recent_games.append(row["game_name"])
                seen.add(row["game_name"])
    best_type, best_rate = get_member_best_type(conn, member_name)
    return {
        "games_played": games_played,
        "wins": wins,
        "win_rate": wins / games_played * 100 if games_played else 0,
        "recent_games": recent_games,
        "best_type": best_type,
        "best_rate": best_rate,
    }


def get_top_player_by_game_type(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT game_type, players, winners FROM play_records")
    type_player_games = defaultdict(int)
    type_player_wins = defaultdict(int)
    for row in cur.fetchall():
        gtype = (row["game_type"] or "未分類").strip()
        for p in parse_csv_names(row["players"]):
            type_player_games[(gtype, p)] += 1
        for w in parse_csv_names(row["winners"]):
            type_player_wins[(gtype, w)] += 1
    cur.execute("SELECT name, image_path FROM members")
    image_map = {row["name"]: web_image_path(row["image_path"]) for row in cur.fetchall()}
    result = []
    for gtype in sorted({k[0] for k in type_player_games.keys()}):
        candidates = []
        for (gt, player), total in type_player_games.items():
            if gt != gtype:
                continue
            wins = type_player_wins.get((gt, player), 0)
            rate = wins / total * 100 if total else 0
            candidates.append((player, total, wins, rate))
        if not candidates:
            continue
        candidates.sort(key=lambda x: (-x[3], -x[2], -x[1], x[0]))
        player, total, wins, rate = candidates[0]
        result.append({"game_type": gtype, "player": player, "games": total, "wins": wins, "win_rate": rate, "image_path": image_map.get(player, "")})
    return result


def recent_records(conn, limit: int = 6):
    cur = conn.cursor()
    cur.execute(q("SELECT play_date, game_name, players FROM play_records ORDER BY play_date DESC, id DESC LIMIT %s"), (limit,))
    return [dict(r) for r in cur.fetchall()]



def chart_html(df: pd.DataFrame, chart_type: str, x: str, y: Optional[str] = None, title: str = "", click_template: str | None = None) -> str:
    if df.empty:
        return '<div class="empty">目前尚無紀錄</div>'
    if chart_type == "bar":
        fig = px.bar(df, x=x, y=y, title=title, text_auto=True, custom_data=[x])
    else:
        fig = px.pie(df, names=x, values=y, title=title, custom_data=[x])
    fig.update_layout(margin=dict(l=10, r=10, t=44, b=10), height=320, paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF")
    div_id = f"chart-{abs(hash((title, x, y, chart_type))) % 10_000_000}"
    post_script = None
    if click_template:
        safe_template = click_template.replace("'", "\'")
        post_script = f"""
        const gd = document.getElementById('{div_id}');
        if (gd) {{
          gd.on('plotly_click', function(data) {{
            const raw = data?.points?.[0]?.customdata?.[0] ?? data?.points?.[0]?.label ?? data?.points?.[0]?.x;
            if (raw !== undefined && raw !== null) {{
              const url = '{safe_template}'.replace('__VALUE__', encodeURIComponent(String(raw)));
              window.location.href = url;
            }}
          }});
          gd.style.cursor = 'pointer';
        }}
        """
    return fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False, "responsive": True}, div_id=div_id, post_script=post_script)


def game_type_badges(conn, game_name: str) -> str:
    cur = conn.cursor()
    cur.execute(q("SELECT COUNT(*) AS plays FROM play_records WHERE game_name = %s"), (game_name,))
    row = cur.fetchone()
    plays = row[0] if isinstance(row, tuple) else row["plays"]
    cur.execute(q("SELECT winners FROM play_records WHERE game_name = %s"), (game_name,))
    winner_refs = 0
    for r in cur.fetchall():
        winner_refs += len(parse_csv_names(r[0] if isinstance(r, tuple) else r["winners"]))
    badges = [f'<span class="pill">🎯 {plays} 場</span>']
    if winner_refs:
        badges.append(f'<span class="pill">🏆 {winner_refs} 勝者紀錄</span>')
    return "".join(badges)


def player_badges(summary: dict) -> str:
    return "".join([
        f'<span class="pill">🎮 {summary["games_played"]} 場</span>',
        f'<span class="pill">🏆 {summary["wins"]} 勝</span>',
        f'<span class="pill">📊 {summary["win_rate"]:.1f}%</span>',
    ])


def winrate_detail_rows(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT game_type, players, winners FROM play_records")
    totals = defaultdict(int)
    wins = defaultdict(int)
    for row in cur.fetchall():
        gtype = row["game_type"] or "未分類"
        for p in parse_csv_names(row["players"]):
            totals[(p, gtype)] += 1
        for w in parse_csv_names(row["winners"]):
            wins[(w, gtype)] += 1
    rows = []
    for (player, gtype), total in totals.items():
        w = wins.get((player, gtype), 0)
        rows.append({"player": player, "game_type": gtype, "games": total, "wins": w, "win_rate": round(w / total * 100, 1) if total else 0.0})
    rows.sort(key=lambda x: (-x["win_rate"], -x["wins"], -x["games"], x["player"], x["game_type"]))
    return rows



def get_game_summary(conn, game_name: str) -> dict:
    cur = conn.cursor()
    cur.execute(q("SELECT COUNT(*) AS plays FROM play_records WHERE game_name = %s"), (game_name,))
    row = cur.fetchone()
    plays = row[0] if isinstance(row, tuple) else row["plays"]
    cur.execute(q("SELECT MAX(play_date) AS latest FROM play_records WHERE game_name = %s"), (game_name,))
    row = cur.fetchone()
    latest = row[0] if isinstance(row, tuple) else row["latest"]
    cur.execute(q("SELECT play_date, players, winners FROM play_records WHERE game_name = %s ORDER BY play_date DESC, id DESC LIMIT %s"), (game_name, 12))
    history = [dict(r) for r in cur.fetchall()]
    return {"plays": plays or 0, "latest": latest or "尚無紀錄", "history": history}


def get_member_breakdown(conn, member_name: str) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT game_name, game_type, players, winners, play_date FROM play_records ORDER BY play_date DESC, id DESC")
    stats = {}
    for row in cur.fetchall():
        players = parse_csv_names(row["players"])
        winners = parse_csv_names(row["winners"])
        if member_name not in players:
            continue
        key = (row["game_name"], row["game_type"] or "未分類")
        stats.setdefault(key, {"games": 0, "wins": 0, "latest": row["play_date"]})
        stats[key]["games"] += 1
        if member_name in winners:
            stats[key]["wins"] += 1
        if row["play_date"] > stats[key]["latest"]:
            stats[key]["latest"] = row["play_date"]
    result = []
    for (game_name, game_type), s in stats.items():
        result.append({
            "game_name": game_name,
            "game_type": game_type,
            "games": s["games"],
            "wins": s["wins"],
            "win_rate": round(s["wins"] / s["games"] * 100, 1) if s["games"] else 0.0,
            "latest": s["latest"],
        })
    result.sort(key=lambda x: (-x["win_rate"], -x["wins"], -x["games"], x["game_name"]))
    return result


def game_detail_block(conn, game: dict) -> str:
    summary = get_game_summary(conn, game["name"])
    image_url = web_image_path(game["image_path"])
    hide_attr = 'style="display:none"' if not image_url else ''
    history_html = ''.join(
        f"<tr><td>{escape(r['play_date'])}</td><td>{escape(r['players'])}</td><td>{escape(r['winners'])}</td></tr>"
        for r in summary["history"]
    ) or '<tr><td colspan="3">目前尚無紀錄</td></tr>'
    notes = load_notes(game.get("notes"))
    notes_html = ''.join(
        f'<li style="margin:8px 0"><div style="display:flex;gap:8px;align-items:flex-start;justify-content:space-between"><span>{escape(note)}</span><form method="post" action="/games/{game['id']}/notes/{idx}/delete" onsubmit="return confirm(\'刪除這則心得？\');"><button class="danger" type="submit">刪除</button></form></div></li>'
        for idx, note in enumerate(notes)
    ) or '<div class="empty">目前尚無心得，新增一則吧。</div>'
    return f"""
<div class="card">
  <div class="item" style="padding:0;border:0;box-shadow:none;background:transparent">
    <img class="thumb" src="{escape(image_url)}" {hide_attr}>
    <div>
      <h2 style="margin:0 0 6px">{escape(game['name'])}</h2>
      <div class="meta">類型：{escape(game['category'])}<br>BGG：{game['bgg_score'] if game['bgg_score'] is not None else '未填寫'}<br>玩家評分：{game['user_rating'] if game['user_rating'] is not None else '未填寫'}<br>遊玩次數：{summary['plays']}<br>最後遊玩：{escape(summary['latest'])}</div>
      <div class="badge-row">{game_type_badges(conn, game['name'])}</div>
    </div>
  </div>
</div>
<div class="card"><div class="section-title"><div class="card-title">條列式心得</div><div class="small">每次想到都可以追加一則</div></div>
  <form method="post" action="/games/{game['id']}/notes" class="form-grid">
    <div><label>新增心得</label><textarea name="note_text" rows="3" placeholder="例如：前期資源很重要、兩人玩節奏比較緊、適合帶新手入門"></textarea></div>
    <div class="btn-row"><button type="submit">➕ 新增心得</button></div>
  </form>
  <div style="margin-top:10px"><ul style="padding-left:18px;margin:0">{notes_html}</ul></div>
</div>
<div class="card"><div class="section-title"><div class="card-title">最近遊玩紀錄</div><a class="btn btn-soft" href="/records?game_filter={urlq(game['name'])}">查看全部</a></div><div class="table-wrap"><table><thead><tr><th>日期</th><th>玩家</th><th>勝者</th></tr></thead><tbody>{history_html}</tbody></table></div></div>"""

def member_detail_block(conn, member: dict) -> str:
    summary = get_member_summary(conn, member['name'])
    image_url = web_image_path(member['image_path'])
    hide_attr = 'style="display:none"' if not image_url else ''
    best_line = f"<div class='pill'>🔥 最強類型：{escape(summary['best_type'])}（{summary['best_rate']}）</div>" if summary['best_type'] else ''
    breakdown = get_member_breakdown(conn, member['name'])
    rows_html = ''.join(
        f"<tr><td>{escape(r['game_name'])}</td><td>{escape(r['game_type'])}</td><td>{r['games']}</td><td>{r['wins']}</td><td>{r['win_rate']:.1f}%</td><td>{escape(r['latest'])}</td></tr>"
        for r in breakdown[:20]
    ) or '<tr><td colspan="6">目前尚無紀錄</td></tr>'
    return f"""
<div class="card">
  <div class="item" style="padding:0;border:0;box-shadow:none;background:transparent">
    <img class="thumb circle" src="{escape(image_url)}" {hide_attr}>
    <div>
      <h2 style="margin:0 0 6px">{escape(member['name'])}</h2>
      <div class="meta">擅長：{escape(member['favorite_types'] or '未填寫')}<br>總場數：{summary['games_played']}｜勝場：{summary['wins']}｜勝率：{summary['win_rate']:.1f}%<br>近期遊玩：{escape('、'.join(summary['recent_games']) if summary['recent_games'] else '尚無紀錄')}</div>
      <div class="badge-row">{player_badges(summary)}</div>{best_line}
    </div>
  </div>
</div>
<div class="card"><div class="section-title"><div class="card-title">桌遊表現明細</div><a class="btn btn-soft" href="/stats?player_filter={urlq(member['name'])}">勝率分析</a></div><div class="table-wrap"><table><thead><tr><th>桌遊</th><th>類型</th><th>場次</th><th>勝場</th><th>勝率</th><th>最近遊玩</th></tr></thead><tbody>{rows_html}</tbody></table></div></div>"""

def page_template(title: str, body: str, active: str, notice: str = "") -> HTMLResponse:
    nav = {
        "dashboard": ("🏠 儀表板", "/"),
        "games": ("🎲 桌遊", "/games"),
        "members": ("👥 成員", "/members"),
        "records": ("📝 紀錄", "/records"),
        "stats": ("📈 勝率", "/stats"),
    }
    nav_html = "".join(f'<a class="nav-item {"active" if k == active else ""}" href="{href}">{label}</a>' for k, (label, href) in nav.items())
    bottom_nav = "".join(f'<a class="bottom-item {"active" if k == active else ""}" href="{href}">{label}</a>' for k, (label, href) in nav.items())
    notice_html = f'<div class="notice">{escape(notice)}</div>' if notice else ""
    html = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{escape(title)}</title>
<style>
:root{{--bg:#f5f7fb;--bg-grad-1:#f7f9fd;--bg-grad-2:#f2f6fb;--card:#fff;--text:#243447;--sub:#5b6b7a;--line:#dde6f0;--accent:#5b8ff9;--accent-deep:#3b6fdc;--soft:#eef3fa;--warn:#fff7e6;--warn-line:#f6c667;--danger:#fff1f1;--danger-line:#f1c2c2;--shadow:0 8px 24px rgba(25,38,61,.06);--header:rgba(245,247,251,.92);--hero:#edf4ff;}}
body.theme-dark{{--bg:#0f1723;--bg-grad-1:#0f1723;--bg-grad-2:#121c2b;--card:#162233;--text:#e9eef7;--sub:#a4b3c8;--line:#26364a;--accent:#78a6ff;--accent-deep:#5a87f0;--soft:#1b2a3d;--warn:#3a2d15;--warn-line:#8c6c25;--danger:#3d2022;--danger-line:#94484d;--shadow:0 12px 28px rgba(0,0,0,.28);--header:rgba(15,23,35,.92);--hero:#1a2b42;}}
*{{box-sizing:border-box}} html,body{{min-height:100%}} body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft JhengHei",sans-serif;background:linear-gradient(180deg,var(--bg-grad-1) 0%,var(--bg-grad-2) 100%);color:var(--text);transition:background .2s ease,color .2s ease}}
a{{color:inherit}} .wrap{{max-width:980px;margin:0 auto;padding:12px 12px 90px}} .header{{position:sticky;top:0;z-index:20;background:var(--header);backdrop-filter:blur(12px);border-bottom:1px solid color-mix(in srgb, var(--line) 80%, transparent);padding-top:env(safe-area-inset-top)}}
.title{{font-size:22px;font-weight:800;margin:6px 0 10px}} .top-row{{display:flex;align-items:center;justify-content:space-between;gap:10px}} .theme-toggle{{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:999px;padding:10px 12px;cursor:pointer;box-shadow:var(--shadow)}} .nav{{display:flex;gap:8px;overflow:auto;padding-bottom:8px}} .nav-item{{white-space:nowrap;text-decoration:none;color:var(--text);background:var(--soft);padding:10px 14px;border-radius:999px;font-size:14px}}
.nav-item.active{{background:linear-gradient(180deg,var(--accent),var(--accent-deep));color:#fff;box-shadow:0 6px 16px rgba(91,143,249,.28)}} .card{{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:14px;box-shadow:var(--shadow);margin-top:12px}}
.card-title{{font-size:18px;font-weight:800;margin:0 0 10px}} .section-title{{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}} .sub,.small{{color:var(--sub);font-size:13px}} .notice{{background:var(--warn);border:1px solid var(--warn-line);padding:12px 14px;border-radius:16px;margin-top:12px}}
.grid-2{{display:grid;grid-template-columns:1fr;gap:12px}} .stats{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}} .stat{{background:linear-gradient(180deg,color-mix(in srgb, var(--card) 96%, #fff), color-mix(in srgb, var(--card) 90%, var(--hero)));border:1px solid var(--line);border-radius:18px;padding:14px}} .num{{font-size:26px;font-weight:800;margin-top:6px}}
.form-grid{{display:grid;grid-template-columns:1fr;gap:10px}} label{{display:block;font-size:13px;color:var(--sub);margin-bottom:4px}} input,select,textarea{{width:100%;border:1px solid var(--line);border-radius:14px;padding:12px;background:var(--card);font:inherit;color:var(--text);outline:none}}
input:focus,select:focus,textarea:focus{{border-color:#8cb0ff;box-shadow:0 0 0 4px rgba(91,143,249,.12)}} button,.btn{{border:0;background:linear-gradient(180deg,var(--accent),var(--accent-deep));color:#fff;padding:12px 16px;border-radius:14px;font-weight:700;font:inherit;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:6px}}
.btn-row{{display:flex;gap:8px;flex-wrap:wrap}} .btn-soft{{background:var(--soft);color:var(--accent-deep);border:1px solid color-mix(in srgb, var(--accent) 18%, var(--line))}} .danger{{background:var(--danger);color:#c0392b;border:1px solid var(--danger-line)}} .list{{display:grid;gap:12px}}
.item{{display:flex;gap:12px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:12px;box-shadow:0 8px 20px rgba(25,38,61,.05)}} .thumb{{width:76px;height:76px;border-radius:18px;object-fit:cover;background:#edf2f8;flex:none}} .thumb.circle{{border-radius:999px}}
.item h3{{margin:0 0 4px;font-size:17px}} .meta{{color:var(--sub);font-size:13px;line-height:1.58}} .spacer{{flex:1}} .tag,.pill{{display:inline-block;padding:5px 10px;border-radius:999px;font-size:12px;border:1px solid #ffe0a8;background:#fff8ea;color:#cb7a00}}
.hero-grid{{display:grid;gap:10px}} .hero-card{{display:flex;gap:12px;align-items:center;border:1px solid var(--line);border-radius:18px;padding:12px;background:linear-gradient(180deg,color-mix(in srgb, var(--card) 100%, transparent), color-mix(in srgb, var(--card) 92%, var(--hero)))}} .hero-avatar{{width:56px;height:56px;border-radius:999px;object-fit:cover;background:#edf2f8}}
.empty{{color:var(--sub);padding:10px 0}} .table-wrap{{overflow:auto}} table{{width:100%;border-collapse:collapse;font-size:14px}} th,td{{padding:10px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
.rank-grid{{display:grid;gap:12px}} .rank-card{{border:1px solid var(--line);border-radius:18px;padding:12px;background:var(--card)}} .rank-line{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px dashed color-mix(in srgb, var(--line) 70%, transparent)}} .rank-line:last-child{{border-bottom:0}}
.modal-backdrop{{position:fixed;inset:0;background:rgba(12,23,40,.48);display:none;align-items:flex-end;justify-content:center;z-index:30}} .modal-backdrop.show{{display:flex}} .modal-sheet{{width:100%;max-width:720px;background:var(--card);border:1px solid var(--line);border-radius:24px 24px 0 0;padding:14px;max-height:90vh;overflow:auto}}
.modal-head{{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}} .close-btn{{background:var(--soft);color:var(--text);border:1px solid var(--line);padding:10px 14px;border-radius:12px}}
.preview-box{{display:flex;align-items:center;gap:10px;min-height:52px}} .preview-box img{{width:52px;height:52px;border-radius:14px;object-fit:cover;border:1px solid var(--line);background:#edf2f8}} .preview-box img.circle{{border-radius:999px}} .preview-hint{{font-size:12px;color:var(--sub)}}
.choice-wrap{{display:flex;flex-wrap:wrap;gap:8px}} .choice-chip{{border:1px solid var(--line);background:var(--card);border-radius:999px;padding:9px 12px;font-size:14px;cursor:pointer;color:var(--text)}} .choice-chip.active{{background:linear-gradient(180deg,var(--accent),var(--accent-deep));color:#fff;border-color:transparent;box-shadow:0 6px 14px rgba(91,143,249,.22)}} .choice-chip.win{{border-color:#ffe0a8;background:#fff8ea}} .choice-chip.win.active{{background:linear-gradient(180deg,#ffb84d,#f39c12);color:#fff;border-color:transparent}}
.quick-links{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}} .quick-links .btn{{width:100%}} .fab-row{{display:flex;gap:10px;overflow:auto;scrollbar-width:none}} .fab-card{{min-width:180px;padding:14px;border-radius:18px;border:1px solid var(--line);background:linear-gradient(180deg,color-mix(in srgb, var(--card) 98%, transparent), color-mix(in srgb, var(--card) 88%, var(--hero)));box-shadow:var(--shadow)}} .fab-card strong{{display:block;margin-bottom:4px}}
.bottom-nav{{position:fixed;left:0;right:0;bottom:0;z-index:25;padding:8px 12px calc(8px + env(safe-area-inset-bottom));background:color-mix(in srgb, var(--header) 96%, transparent);backdrop-filter:blur(12px);border-top:1px solid color-mix(in srgb, var(--line) 75%, transparent)}} .bottom-grid{{max-width:980px;margin:0 auto;display:grid;grid-template-columns:repeat(5,1fr);gap:8px}} .bottom-item{{text-decoration:none;text-align:center;padding:10px 6px;border-radius:16px;background:var(--soft);font-size:12px;color:var(--text)}} .bottom-item.active{{background:linear-gradient(180deg,var(--accent),var(--accent-deep));color:#fff;box-shadow:0 6px 16px rgba(91,143,249,.28)}}}}
@media (max-width:760px){{
.item{{display:grid;grid-template-columns:92px minmax(0,1fr);gap:14px;align-items:start}}
.item .thumb{{width:92px;height:92px;border-radius:18px}}
.item .thumb.circle{{width:92px;height:92px;border-radius:999px}}
.item h3{{font-size:22px;line-height:1.2;margin:0 0 8px;word-break:keep-all;overflow-wrap:anywhere}}
.item .meta{{font-size:15px;line-height:1.65;word-break:keep-all;overflow-wrap:anywhere}}
.item .spacer{{display:none}}
.item .btn-row{{grid-column:1 / -1;display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}}
.item .btn-row form{{margin:0}}
.item .btn-row .btn,.item .btn-row .btn-soft,.item .btn-row .danger,.item .btn-row button{{width:100%;min-height:48px;font-size:15px;border-radius:14px;padding:12px 10px}}
.item .badge-row{{margin-top:10px;display:flex;flex-wrap:wrap;gap:8px}}
.item .pill{{margin:0}}
}}
@media (min-width:760px){{.grid-2{{grid-template-columns:1fr 1fr}}.stats{{grid-template-columns:repeat(4,1fr)}}.form-grid.two{{grid-template-columns:1fr 1fr}}.rank-grid{{grid-template-columns:1fr 1fr}}.modal-backdrop{{align-items:center;padding:24px}}.modal-sheet{{border-radius:24px;max-height:92vh}}.wrap{{padding-bottom:40px}}.bottom-nav{{display:none}}}}
</style>
<script>
function applyTheme(theme){{document.body.classList.toggle('theme-dark', theme==='dark');const btn=document.getElementById('themeToggle');if(btn) btn.textContent = theme==='dark' ? '☀️ 淺色' : '🌙 深色'; localStorage.setItem('bg_theme', theme);}}
function toggleTheme(){{const dark=document.body.classList.contains('theme-dark');applyTheme(dark?'light':'dark');}}
function openModal(id){{const m=document.getElementById(id);if(m)m.classList.add('show');}}
function closeModal(id){{const m=document.getElementById(id);if(m)m.classList.remove('show');}}
document.addEventListener('click',function(e){{const b=e.target.closest('.modal-backdrop.show');if(b&&e.target===b)b.classList.remove('show');}});
function fillGameType(gameInputId,typeInputId,mapId){{const gameInput=document.getElementById(gameInputId);const typeInput=document.getElementById(typeInputId);const dataEl=document.getElementById(mapId);if(!gameInput||!typeInput||!dataEl)return;try{{const data=JSON.parse(dataEl.textContent);typeInput.value=data[gameInput.value]||typeInput.value;}}catch(e){{}}}}
function bindImagePreview(inputId,imgId){{const input=document.getElementById(inputId);const img=document.getElementById(imgId);if(!input||!img)return;input.addEventListener('change',()=>{{const file=input.files&&input.files[0];if(!file){{img.style.display='none';img.removeAttribute('src');return;}}const reader=new FileReader();reader.onload=e=>{{img.src=e.target.result;img.style.display='block';}};reader.readAsDataURL(file);}});}}
function toggleChoice(setName, value, hiddenId, syncWinners){{const hidden=document.getElementById(hiddenId);if(!hidden)return;let items=(hidden.value||'').split(',').map(s=>s.trim()).filter(Boolean);if(items.includes(value)){{items=items.filter(v=>v!==value);}}else{{items.push(value);}}hidden.value=items.join(', ');document.querySelectorAll(`[data-set="${{setName}}"]`).forEach(el=>{{const val=el.getAttribute('data-value');el.classList.toggle('active', items.includes(val));}});if(syncWinners) syncWinnerChoices();}}
function syncWinnerChoices(){{const playersEl=document.getElementById('record_players');const winnersEl=document.getElementById('record_winners');if(!playersEl||!winnersEl)return;const players=(playersEl.value||'').split(',').map(s=>s.trim()).filter(Boolean);let winners=(winnersEl.value||'').split(',').map(s=>s.trim()).filter(Boolean);winners=winners.filter(w=>players.includes(w));winnersEl.value=winners.join(', ');document.querySelectorAll('[data-set="winner"]').forEach(el=>{{const val=el.getAttribute('data-value');const allowed=players.includes(val);el.disabled=!allowed;el.style.opacity=allowed?'1':'0.45';el.style.cursor=allowed?'pointer':'not-allowed';el.classList.toggle('active', winners.includes(val));}});}}
document.addEventListener('DOMContentLoaded',()=>{{document.querySelectorAll('[data-preview-bind]').forEach(el=>bindImagePreview(el.dataset.previewBind, el.dataset.previewTarget));syncWinnerChoices();applyTheme(localStorage.getItem('bg_theme')||'light');}});
</script></head>
<body><div class="header"><div class="wrap"><div class="top-row"><div class="title">{APP_TITLE}</div><button id="themeToggle" class="theme-toggle" type="button" onclick="toggleTheme()">🌙 深色</button></div><div class="nav">{nav_html}</div></div></div><div class="wrap">{notice_html}{body}</div><div class="bottom-nav"><div class="bottom-grid">{bottom_nav}</div></div></body></html>"""
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
def dashboard(notice: str = ""):
    conn = get_conn()
    stats = summary_stats(conn)
    top_games_data = top_game_rankings(conn, 8)
    top_members_data = top_member_rankings(conn, 8)
    cur = conn.cursor()
    cur.execute("SELECT game_type, COUNT(*) AS cnt FROM play_records GROUP BY game_type ORDER BY cnt DESC, game_type ASC LIMIT 6")
    type_dist = pd.DataFrame([dict(row) for row in cur.fetchall()])
    heroes = get_top_player_by_game_type(conn)
    recent = recent_records(conn, 6)
    conn.close()
    top_games = pd.DataFrame(top_games_data)
    top_members = pd.DataFrame(top_members_data)
    hero_cards = "".join(
        f'''<a class="hero-card" style="text-decoration:none" href="/stats?player_filter={urlq(h['player'])}&type_filter={urlq(h['game_type'])}"><img class="hero-avatar" src="{escape(h['image_path'])}" {'style="display:none"' if not h['image_path'] else ''}><div><div><strong>👑 {escape(h['game_type'])}</strong></div><div>{escape(h['player'])}</div><div class="small">勝率 {h['win_rate']:.1f}%｜勝場 {h['wins']} / {h['games']}</div></a>'''
        for h in heroes
    ) or '<div class="empty">目前尚無紀錄</div>'
    game_ranks_html = "".join(f"<div class='rank-line'><div><strong>#{i+1}</strong> {escape(r['game_name'])}</div><div class='small'>{r['plays']} 場</div></div>" for i, r in enumerate(top_games_data)) or '<div class="empty">目前尚無紀錄</div>'
    member_ranks_html = "".join(f"<div class='rank-line'><div><strong>#{i+1}</strong> {escape(r['player'])}</div><div class='small'>勝率 {r['win_rate']:.1f}%｜{r['wins']} 勝</div></div>" for i, r in enumerate(top_members_data)) or '<div class="empty">目前尚無紀錄</div>'
    recent_html = "".join(f"<div class='rank-line'><div><strong>{escape(r['play_date'])}</strong>｜{escape(r['game_name'])}</div><div class='small'>{escape(r['players'])}</div></div>" for r in recent) or '<div class="empty">目前尚無紀錄</div>'
    body = f"""
<div class="stats">
  <div class="stat"><div class="sub">桌遊數量</div><div class="num">{stats['games']}</div></div>
  <div class="stat"><div class="sub">成員數量</div><div class="num">{stats['members']}</div></div>
  <div class="stat"><div class="sub">遊玩紀錄</div><div class="num">{stats['records']}</div></div>
  <div class="stat"><div class="sub">最新遊玩日</div><div class="num" style="font-size:18px">{escape(stats['latest'])}</div></div>
</div>
<div class="card"><div class="section-title"><div class="card-title">快速操作</div><div class="small">常用入口</div></div><div class="btn-row"><a class="btn" href="/records">➕ 新增紀錄</a><a class="btn btn-soft" href="/games">🎲 管理桌遊</a><a class="btn btn-soft" href="/members">👥 管理成員</a></div></div>
<div class="grid-2">
  <div class="card"><div class="section-title"><div class="card-title">最多遊玩的桌遊</div><div class="small">熱門遊戲</div></div>{chart_html(top_games, 'bar', 'game_name', 'plays', '最多遊玩的桌遊', '/records?game_filter=__VALUE__')}</div>
  <div class="card"><div class="section-title"><div class="card-title">玩家勝率 Top 8</div><div class="small">整體表現</div></div>{chart_html(top_members, 'bar', 'player', 'win_rate', '玩家勝率 Top 8', '/stats?player_filter=__VALUE__')}</div>
  <div class="card"><div class="section-title"><div class="card-title">桌遊類型分布</div><div class="small">目前遊玩結構</div></div>{chart_html(type_dist, 'pie', 'game_type', 'cnt', '桌遊類型分布', '/stats?type_filter=__VALUE__')}</div>
  <div class="card"><div class="section-title"><div class="card-title">各類型桌遊勝率最高的玩家</div><div class="small">類型王者</div></div><div class="hero-grid">{hero_cards}</div></div>
</div>
<div class="rank-grid">
  <div class="card"><div class="card-title">桌遊排行榜</div><div class="rank-card">{game_ranks_html}</div></div>
  <div class="card"><div class="card-title">玩家排行榜</div><div class="rank-card">{member_ranks_html}</div></div>
</div>
<div class="card"><div class="card-title">最近紀錄</div><div class="rank-card">{recent_html}</div></div>"""
    return page_template("儀表板", body, "dashboard", notice)


@app.get("/games", response_class=HTMLResponse)
def games_page(notice: str = "", q_text: str = "", category_filter: str = "全部"):
    conn = get_conn(); cur = conn.cursor()
    sql = "SELECT id, name, category, image_path, bgg_score, user_rating, COALESCE(last_play_date,'') AS last_play_date, COALESCE(play_count,0) AS play_count, COALESCE(notes,'[]') AS notes FROM games WHERE 1=1"
    params = []
    if q_text.strip():
        sql += " AND (name LIKE %s OR category LIKE %s)" if USE_POSTGRES else " AND (name LIKE ? OR category LIKE ?)"
        like = f"%{q_text.strip()}%"
        params.extend([like, like])
    if category_filter and category_filter != "全部":
        sql += " AND category = %s" if USE_POSTGRES else " AND category = ?"
        params.append(category_filter)
    sql += " ORDER BY name"
    cur.execute(q(sql), tuple(params) if USE_POSTGRES else params)
    rows = cur.fetchall()
    categories = category_options(conn)
    category_list = "".join(f'<option value="{escape(c)}">' for c in categories)
    filter_options = '<option value="全部">全部類型</option>' + "".join(f'<option value="{escape(c)}" {"selected" if c == category_filter else ""}>{escape(c)}</option>' for c in categories)
    items, modals = [], []
    for game in rows:
        image_url = web_image_path(game["image_path"])
        hide_attr = 'style="display:none"' if not image_url else ''
        badge_html = game_type_badges(conn, game["name"])
        items.append(f'''<div class="item"><img class="thumb" src="{escape(image_url)}" {hide_attr}><div><h3>{escape(game['name'])}</h3><div class="meta">類型：{escape(game['category'])}<br>BGG：{game['bgg_score'] if game['bgg_score'] is not None else '未填寫'}<br>玩家評分：{game['user_rating'] if game['user_rating'] is not None else '未填寫'}<br>遊玩次數：{game['play_count']}<br>最後遊玩：{escape(game['last_play_date'] or '尚無紀錄')}</div><div class="badge-row">{badge_html}</div></div><div class="spacer"></div><div class="btn-row"><a class="btn btn-soft" href="/games/{game['id']}">🔎 詳細</a><button class="btn-soft" type="button" onclick="openModal('game-modal-{game['id']}')">✏️ 編輯</button><form method="post" action="/games/{game['id']}/delete" onsubmit="return confirm('確定要刪除這款桌遊嗎？');"><button class="danger" type="submit">🗑️ 刪除</button></form></div></div>''')
        modals.append(f'''<div class="modal-backdrop" id="game-modal-{game['id']}"><div class="modal-sheet"><div class="modal-head"><div><div class="card-title" style="margin:0">編輯桌遊</div><div class="small">修改桌遊資料，若不選圖片會保留原圖</div></div><button class="close-btn" type="button" onclick="closeModal('game-modal-{game['id']}')">關閉</button></div><form class="form-grid two" method="post" action="/games/{game['id']}/edit" enctype="multipart/form-data"><div><label>桌遊名稱</label><input name="name" value="{escape(game['name'])}" required></div><div><label>桌遊類型</label><input name="category" value="{escape(game['category'])}" list="category_list" required></div><div><label>BGG 分數</label><input name="bgg_score" inputmode="decimal" value="{game['bgg_score'] if game['bgg_score'] is not None else ''}"></div><div><label>玩家自評分</label><input name="user_rating" inputmode="decimal" value="{game['user_rating'] if game['user_rating'] is not None else ''}"></div><div style="grid-column:1/-1"><label>更換圖片</label><input id="edit_game_image_{game['id']}" type="file" name="image" data-preview-bind="edit_game_image_{game['id']}" data-preview-target="edit_game_preview_{game['id']}"></div><div style="grid-column:1/-1" class="preview-box"><img id="edit_game_preview_{game['id']}" src="{escape(image_url)}" {hide_attr}><div class="preview-hint">不選新圖會保留原圖</div></div><div style="grid-column:1/-1"><label>心得（每行一則）</label><textarea name="notes_text" rows="4">{escape(chr(10).join(load_notes(game.get("notes"))))}</textarea></div><div style="grid-column:1/-1" class="btn-row"><button type="submit">儲存修改</button><button class="close-btn" type="button" onclick="closeModal('game-modal-{game['id']}')">取消</button></div></form></div></div>''')
    body = f"""
<div class="card"><div class="card-title">新增桌遊</div>
<form class="form-grid two" method="post" action="/games" enctype="multipart/form-data">
  <div><label>桌遊名稱</label><input name="name" placeholder="例如 阿納克遺跡" required></div>
  <div><label>桌遊類型</label><input name="category" list="category_list" placeholder="例如 策略 / 派對" required></div>
  <div><label>BGG 分數</label><input name="bgg_score" inputmode="decimal" placeholder="例如 7.8"></div>
  <div><label>玩家自評分</label><input name="user_rating" inputmode="decimal" placeholder="例如 8.5"></div>
  <div style="grid-column:1/-1"><label>圖片</label><input id="add_game_image" type="file" name="image" data-preview-bind="add_game_image" data-preview-target="add_game_preview"></div><div style="grid-column:1/-1" class="preview-box"><img id="add_game_preview" style="display:none"><div class="preview-hint">選圖後會即時預覽</div></div><div style="grid-column:1/-1"><label>心得（每行一則，可空白）</label><textarea name="notes_text" rows="3" placeholder="例如：互動很強、兩人局節奏快、適合新手入門"></textarea></div>
  <div style="grid-column:1/-1" class="btn-row"><button type="submit">➕ 儲存桌遊</button></div>
</form><datalist id="category_list">{category_list}</datalist></div>
<div class="card"><div class="section-title"><div class="card-title">桌遊列表</div><div class="small">共 {len(rows)} 款</div></div>
<form class="form-grid two" method="get" action="/games" style="margin-bottom:10px">
  <div><label>搜尋桌遊</label><input name="q_text" value="{escape(q_text)}" placeholder="搜尋名稱或類型"></div>
  <div><label>類型篩選</label><select name="category_filter">{filter_options}</select></div>
  <div class="btn-row" style="grid-column:1/-1"><button type="submit">搜尋 / 篩選</button><a class="btn btn-soft" href="/games">清除條件</a></div>
</form>
<div class="list">{''.join(items) or '<div class="empty">目前尚無桌遊</div>'}</div></div>
{''.join(modals)}"""
    conn.close()
    return page_template("桌遊", body, "games", notice)


@app.post("/games")
async def add_game(name: str = Form(...), category: str = Form(...), bgg_score: str = Form(""), user_rating: str = Form(""), notes_text: str = Form(""), image: UploadFile | None = File(None)):
    conn = get_conn(); cur = conn.cursor()
    try:
        image_path = copy_upload_to_library(image, GAME_IMAGE_DIR, name) if image and image.filename else ""
        cur.execute(q("INSERT INTO games (name, category, image_path, bgg_score, user_rating, notes) VALUES (%s, %s, %s, %s, %s, %s)"), (name.strip(), category.strip(), image_path, parse_optional_float(bgg_score), parse_optional_float(user_rating), dump_notes(load_notes(notes_text))))
        conn.commit()
        return redirect("/games?notice=桌遊已新增")
    except Exception as exc:
        conn.rollback()
        return redirect("/games?notice=" + ("桌遊名稱已存在" if unique_error(exc) else f"新增失敗：{str(exc)[:120]}"))
    finally:
        conn.close()


@app.post("/games/{game_id}/edit")
async def edit_game(game_id: int, name: str = Form(...), category: str = Form(...), bgg_score: str = Form(""), user_rating: str = Form(""), notes_text: str = Form(""), image: UploadFile | None = File(None)):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute(q("SELECT name, image_path, COALESCE(notes,'[]') AS notes FROM games WHERE id = %s"), (game_id,))
        old = cur.fetchone()
        if not old:
            return redirect("/games?notice=找不到這款桌遊")
        old_name = old["name"]; existing_image = old["image_path"] or ""
        image_path = preserve_upload_or_existing(image, GAME_IMAGE_DIR, name.strip(), existing_image)
        cur.execute(q("UPDATE games SET name = %s, category = %s, image_path = %s, bgg_score = %s, user_rating = %s, notes = %s WHERE id = %s"), (name.strip(), category.strip(), image_path, parse_optional_float(bgg_score), parse_optional_float(user_rating), dump_notes(load_notes(notes_text)), game_id))
        if old_name != name.strip():
            cur.execute(q("UPDATE play_records SET game_name = %s WHERE game_name = %s"), (name.strip(), old_name))
        cur.execute(q("UPDATE play_records SET game_type = %s WHERE game_id = %s OR game_name = %s"), (category.strip(), game_id, name.strip()))
        conn.commit(); sync_game_stats()
        return redirect("/games?notice=桌遊已更新")
    except Exception as exc:
        conn.rollback()
        return redirect("/games?notice=" + ("桌遊名稱已存在" if unique_error(exc) else f"更新失敗：{str(exc)[:120]}"))
    finally:
        conn.close()


@app.post("/games/{game_id}/delete")
def delete_game(game_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(q("DELETE FROM games WHERE id = %s"), (game_id,))
    conn.commit(); conn.close()
    return redirect("/games?notice=桌遊已刪除")


@app.get("/members", response_class=HTMLResponse)
def members_page(notice: str = "", q_text: str = ""):
    conn = get_conn(); cur = conn.cursor()
    sql = "SELECT id, name, favorite_types, image_path FROM members WHERE 1=1"
    params = []
    if q_text.strip():
        sql += " AND (name LIKE %s OR favorite_types LIKE %s)" if USE_POSTGRES else " AND (name LIKE ? OR favorite_types LIKE ?)"
        like = f"%{q_text.strip()}%"
        params.extend([like, like])
    sql += " ORDER BY name"
    cur.execute(q(sql), tuple(params) if USE_POSTGRES else params)
    rows = cur.fetchall()
    cards, modals = [], []
    for member in rows:
        image_url = web_image_path(member["image_path"])
        hide_attr = 'style="display:none"' if not image_url else ''
        summary = get_member_summary(conn, member["name"])
        best_line = f"<div class='pill'>🔥 最強類型：{escape(summary['best_type'])}（{summary['best_rate']}）</div>" if summary["best_type"] else ""
        badges = player_badges(summary)
        cards.append(f'''<div class="item"><img class="thumb circle" src="{escape(image_url)}" {hide_attr}><div><h3>{escape(member['name'])}</h3><div class="meta">擅長：{escape(member['favorite_types'] or '未填寫')}<br>總場數：{summary['games_played']}｜勝場：{summary['wins']}｜勝率：{summary['win_rate']:.1f}%<br>近期遊玩：{escape('、'.join(summary['recent_games']) if summary['recent_games'] else '尚無紀錄')}</div><div class="badge-row">{badges}</div>{best_line}</div><div class="spacer"></div><div class="btn-row"><a class="btn btn-soft" href="/members/{member['id']}">🔎 詳細</a><button class="btn-soft" type="button" onclick="openModal('member-modal-{member['id']}')">✏️ 編輯</button><form method="post" action="/members/{member['id']}/delete" onsubmit="return confirm('確定要刪除這位成員嗎？');"><button class="danger" type="submit">🗑️ 刪除</button></form></div></div>''')
        modals.append(f'''<div class="modal-backdrop" id="member-modal-{member['id']}"><div class="modal-sheet"><div class="modal-head"><div><div class="card-title" style="margin:0">編輯成員</div><div class="small">修改姓名、擅長類型或照片</div></div><button class="close-btn" type="button" onclick="closeModal('member-modal-{member['id']}')">關閉</button></div><form class="form-grid two" method="post" action="/members/{member['id']}/edit" enctype="multipart/form-data"><div><label>成員姓名</label><input name="name" value="{escape(member['name'])}" required></div><div><label>擅長桌遊類型</label><input name="favorite_types" value="{escape(member['favorite_types'] or '')}" placeholder="例如 策略 / 派對"></div><div style="grid-column:1/-1"><label>更換照片</label><input id="edit_member_image_{member['id']}" type="file" name="image" data-preview-bind="edit_member_image_{member['id']}" data-preview-target="edit_member_preview_{member['id']}"></div><div style="grid-column:1/-1" class="preview-box"><img id="edit_member_preview_{member['id']}" class="circle" src="{escape(image_url)}" {hide_attr}><div class="preview-hint">不選新圖會保留原圖</div></div><div style="grid-column:1/-1" class="btn-row"><button type="submit">儲存修改</button><button class="close-btn" type="button" onclick="closeModal('member-modal-{member['id']}')">取消</button></div></form></div></div>''')
    conn.close()
    body = f"""
<div class="card"><div class="card-title">新增成員</div>
<form class="form-grid two" method="post" action="/members" enctype="multipart/form-data">
  <div><label>成員姓名</label><input name="name" placeholder="例如 福" required></div>
  <div><label>擅長桌遊類型</label><input name="favorite_types" placeholder="例如 策略 / 派對"></div>
  <div style="grid-column:1/-1"><label>照片</label><input id="add_member_image" type="file" name="image" data-preview-bind="add_member_image" data-preview-target="add_member_preview"></div><div style="grid-column:1/-1" class="preview-box"><img id="add_member_preview" class="circle" style="display:none"><div class="preview-hint">選圖後會即時預覽</div></div>
  <div style="grid-column:1/-1" class="btn-row"><button type="submit">➕ 儲存成員</button></div>
</form></div>
<div class="card"><div class="section-title"><div class="card-title">成員列表</div><div class="small">共 {len(rows)} 位</div></div>
<form class="form-grid" method="get" action="/members" style="margin-bottom:10px">
  <div><label>搜尋成員</label><input name="q_text" value="{escape(q_text)}" placeholder="搜尋姓名或擅長類型"></div>
  <div class="btn-row"><button type="submit">搜尋</button><a class="btn btn-soft" href="/members">清除條件</a></div>
</form>
<div class="list">{''.join(cards) or '<div class="empty">目前尚無成員</div>'}</div></div>
{''.join(modals)}"""
    return page_template("成員", body, "members", notice)


@app.post("/members")
async def add_member(name: str = Form(...), favorite_types: str = Form(""), image: UploadFile | None = File(None)):
    conn = get_conn(); cur = conn.cursor()
    try:
        image_path = copy_upload_to_library(image, MEMBER_IMAGE_DIR, name) if image and image.filename else ""
        cur.execute(q("INSERT INTO members (name, favorite_types, image_path) VALUES (%s, %s, %s)"), (name.strip(), favorite_types.strip(), image_path))
        conn.commit()
        return redirect("/members?notice=成員已新增")
    except Exception as exc:
        conn.rollback()
        return redirect("/members?notice=" + ("成員姓名已存在" if unique_error(exc) else f"新增失敗：{str(exc)[:120]}"))
    finally:
        conn.close()


@app.post("/members/{member_id}/edit")
async def edit_member(member_id: int, name: str = Form(...), favorite_types: str = Form(""), image: UploadFile | None = File(None)):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute(q("SELECT name, image_path FROM members WHERE id = %s"), (member_id,))
        old = cur.fetchone()
        if not old:
            return redirect("/members?notice=找不到這位成員")
        old_name = old["name"]; existing_image = old["image_path"] or ""
        image_path = preserve_upload_or_existing(image, MEMBER_IMAGE_DIR, name.strip(), existing_image)
        cur.execute(q("UPDATE members SET name = %s, favorite_types = %s, image_path = %s WHERE id = %s"), (name.strip(), favorite_types.strip(), image_path, member_id))
        if old_name != name.strip():
            cur.execute("SELECT id, players, winners FROM play_records")
            for row in cur.fetchall():
                old_players = parse_csv_names(row["players"])
                old_winners = parse_csv_names(row["winners"])
                new_players = [name.strip() if p == old_name else p for p in old_players]
                new_winners = [name.strip() if w == old_name else w for w in old_winners]
                if old_players != new_players or old_winners != new_winners:
                    cur.execute(q("UPDATE play_records SET players = %s, winners = %s WHERE id = %s"), (", ".join(new_players), ", ".join(new_winners), row["id"]))
        conn.commit()
        return redirect("/members?notice=成員已更新")
    except Exception as exc:
        conn.rollback()
        return redirect("/members?notice=" + ("成員姓名已存在" if unique_error(exc) else f"更新失敗：{str(exc)[:120]}"))
    finally:
        conn.close()


@app.post("/members/{member_id}/delete")
def delete_member(member_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(q("DELETE FROM members WHERE id = %s"), (member_id,))
    conn.commit(); conn.close()
    return redirect("/members?notice=成員已刪除")




@app.get("/games/{game_id}", response_class=HTMLResponse)
def game_detail_page(game_id: int, notice: str = ""):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(q("SELECT id, name, category, image_path, bgg_score, user_rating, COALESCE(last_play_date,'') AS last_play_date, COALESCE(play_count,0) AS play_count, COALESCE(notes,'[]') AS notes FROM games WHERE id = %s"), (game_id,))
    game = cur.fetchone()
    if not game:
        conn.close()
        return redirect('/games?notice=找不到這款桌遊')
    body = game_detail_block(conn, dict(game))
    conn.close()
    return page_template(f"桌遊詳細｜{game['name']}", body, "games", notice)


@app.get("/members/{member_id}", response_class=HTMLResponse)
def member_detail_page(member_id: int, notice: str = ""):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(q("SELECT id, name, favorite_types, image_path FROM members WHERE id = %s"), (member_id,))
    member = cur.fetchone()
    if not member:
        conn.close()
        return redirect('/members?notice=找不到這位成員')
    body = member_detail_block(conn, dict(member))
    conn.close()
    return page_template(f"成員詳細｜{member['name']}", body, "members", notice)

@app.get("/records", response_class=HTMLResponse)
def records_page(notice: str = "", q_text: str = "", game_filter: str = "", member_filter: str = "", date_from: str = "", date_to: str = ""):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT name, category FROM games ORDER BY name")
    games = cur.fetchall()
    cur.execute("SELECT name FROM members ORDER BY name")
    members = cur.fetchall()

    sql = "SELECT id, play_date, game_name, game_type, players, winners FROM play_records WHERE 1=1"
    params = []
    if q_text.strip():
        sql += " AND (game_name LIKE %s OR game_type LIKE %s OR players LIKE %s OR winners LIKE %s)" if USE_POSTGRES else " AND (game_name LIKE ? OR game_type LIKE ? OR players LIKE ? OR winners LIKE ?)"
        like = f"%{q_text.strip()}%"
        params.extend([like, like, like, like])
    if game_filter.strip():
        sql += " AND game_name = %s" if USE_POSTGRES else " AND game_name = ?"
        params.append(game_filter.strip())
    if member_filter.strip():
        sql += " AND (players LIKE %s OR winners LIKE %s)" if USE_POSTGRES else " AND (players LIKE ? OR winners LIKE ?)"
        lm = f"%{member_filter.strip()}%"
        params.extend([lm, lm])
    if date_from.strip():
        sql += " AND play_date >= %s" if USE_POSTGRES else " AND play_date >= ?"
        params.append(date_from.strip())
    if date_to.strip():
        sql += " AND play_date <= %s" if USE_POSTGRES else " AND play_date <= ?"
        params.append(date_to.strip())
    sql += " ORDER BY play_date DESC, id DESC"
    cur.execute(q(sql), tuple(params) if USE_POSTGRES else params)
    records = cur.fetchall()
    categories = category_options(conn)
    conn.close()

    game_options = "".join(f'<option value="{escape(g["name"])}">{escape(g["name"])}｜{escape(g["category"])} </option>' for g in games)
    game_filter_options = '<option value="">全部桌遊</option>' + "".join(f'<option value="{escape(g["name"])}" {"selected" if g["name"] == game_filter else ""}>{escape(g["name"])} </option>' for g in games)
    member_filter_options = '<option value="">全部成員</option>' + "".join(f'<option value="{escape(m["name"])}" {"selected" if m["name"] == member_filter else ""}>{escape(m["name"])} </option>' for m in members)
    member_options = "".join(f'<option value="{escape(m["name"])}">' for m in members)
    category_list = "".join(f'<option value="{escape(c)}">' for c in categories)
    member_chip_html = "".join(
        "<button type=\"button\" class=\"choice-chip\" data-set=\"player\" data-value=\"" + escape(m["name"]) + "\" onclick=\"toggleChoice('player', '" + escape(m["name"]) + "', 'record_players', true)\">" + escape(m["name"]) + "</button>"
        for m in members
    )
    winner_chip_html = "".join(
        "<button type=\"button\" class=\"choice-chip win\" data-set=\"winner\" data-value=\"" + escape(m["name"]) + "\" onclick=\"toggleChoice('winner', '" + escape(m["name"]) + "', 'record_winners', false)\">" + escape(m["name"]) + "</button>"
        for m in members
    )
    gmap = {g["name"]: g["category"] for g in games}
    record_rows = "".join(f'''<tr><td>{escape(r['play_date'])}</td><td>{escape(r['game_name'])}</td><td>{escape(r['game_type'])}</td><td>{escape(r['players'])}</td><td>{escape(r['winners'])}</td><td><form method="post" action="/records/{r['id']}/delete" onsubmit="return confirm('確定要刪除這筆紀錄嗎？');"><button class="danger" type="submit">刪除</button></form></td></tr>''' for r in records)

    body = f"""
<div class="card"><div class="card-title">新增遊玩紀錄</div>
<form class="form-grid two" method="post" action="/records">
  <div><label>日期</label><input type="date" name="play_date" value="{date.today().isoformat()}" required></div>
  <div><label>桌遊</label><input id="record_game_name" name="game_name" list="games_list" onchange="fillGameType('record_game_name','record_game_type','game_type_map')" required><datalist id="games_list">{game_options}</datalist></div>
  <div><label>桌遊類型</label><input id="record_game_type" name="game_type" list="category_list" placeholder="可自動帶入或自行修改"></div>
  <div style="grid-column:1/-1"><label>點選玩家</label><div class="choice-wrap">{member_chip_html}</div><input id="record_players" name="players" type="hidden" required><div class="preview-hint">可點選加入或取消，新增勝者區會跟著更新</div></div>
  <div style="grid-column:1/-1"><label>點選勝者</label><div class="choice-wrap">{winner_chip_html}</div><input id="record_winners" name="winners" type="hidden" required><div class="preview-hint">只有已選玩家會顯示在這裡</div></div>
  <datalist id="category_list">{category_list}</datalist>
  <script id="game_type_map" type="application/json">{escape(json.dumps(gmap, ensure_ascii=False))}</script>
  <div style="grid-column:1/-1" class="btn-row"><button type="submit">➕ 新增紀錄</button></div>
</form><div class="small" style="margin-top:8px;">選桌遊後會自動帶入類型。勝者必須包含在玩家名單中。</div></div>
<div class="card"><div class="section-title"><div class="card-title">遊玩紀錄</div><div class="small">共 {len(records)} 筆</div></div>
<form class="form-grid two" method="get" action="/records" style="margin-bottom:10px">
  <div><label>關鍵字</label><input name="q_text" value="{escape(q_text)}" placeholder="桌遊、類型、玩家、勝者"></div>
  <div><label>桌遊篩選</label><select name="game_filter">{game_filter_options}</select></div>
  <div><label>成員篩選</label><select name="member_filter">{member_filter_options}</select></div>
  <div><label>開始日期</label><input type="date" name="date_from" value="{escape(date_from)}"></div>
  <div><label>結束日期</label><input type="date" name="date_to" value="{escape(date_to)}"></div>
  <div class="btn-row" style="grid-column:1/-1"><button type="submit">搜尋 / 篩選</button><a class="btn btn-soft" href="/records">清除條件</a></div>
</form>
<div class="table-wrap"><table><thead><tr><th>日期</th><th>桌遊</th><th>類型</th><th>玩家</th><th>勝者</th><th></th></tr></thead><tbody>{record_rows or '<tr><td colspan="6">目前尚無紀錄</td></tr>'}</tbody></table></div></div>"""
    return page_template("紀錄", body, "records", notice)


@app.post("/records")
def add_record(play_date: str = Form(...), game_name: str = Form(...), game_type: str = Form(""), players: str = Form(...), winners: str = Form("")):
    conn = get_conn(); cur = conn.cursor()
    game_name = game_name.strip()
    cur.execute(q("SELECT id, category FROM games WHERE name = %s"), (game_name,))
    game_row = cur.fetchone()
    game_id = game_row["id"] if game_row else None
    category = game_type.strip() or (game_row["category"] if game_row else "未分類")
    player_list = parse_csv_names(players)
    winner_list = parse_csv_names(winners)
    if not winner_list:
        conn.close()
        return redirect("/records?notice=請至少選擇一位勝者")
    invalid = [w for w in winner_list if w not in player_list]
    if invalid:
        conn.close()
        return redirect("/records?notice=勝者必須在玩家名單中")
    cur.execute(q("INSERT INTO play_records (play_date, game_id, game_name, game_type, players, winners) VALUES (%s, %s, %s, %s, %s, %s)"), (play_date, game_id, game_name, category, ", ".join(player_list), ", ".join(winner_list)))
    conn.commit(); conn.close(); sync_game_stats()
    return redirect("/records?notice=遊玩紀錄已新增")


@app.post("/records/{record_id}/delete")
def delete_record(record_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(q("DELETE FROM play_records WHERE id = %s"), (record_id,))
    conn.commit(); conn.close(); sync_game_stats()
    return redirect("/records?notice=紀錄已刪除")



@app.post("/games/{game_id}/notes")
def add_game_note(game_id: int, note_text: str = Form("")):
    note_text = (note_text or "").strip()
    if not note_text:
        return redirect(f"/games/{game_id}?notice=請先輸入心得內容")
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute(q("SELECT COALESCE(notes,'[]') AS notes FROM games WHERE id = %s"), (game_id,))
        row = cur.fetchone()
        if not row:
            return redirect("/games?notice=找不到這款桌遊")
        notes = load_notes(row["notes"] if not isinstance(row, tuple) else row[0])
        notes.append(note_text)
        cur.execute(q("UPDATE games SET notes = %s WHERE id = %s"), (dump_notes(notes), game_id))
        conn.commit()
        return redirect(f"/games/{game_id}?notice=心得已新增")
    finally:
        conn.close()


@app.post("/games/{game_id}/notes/{note_index}/delete")
def delete_game_note(game_id: int, note_index: int):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute(q("SELECT COALESCE(notes,'[]') AS notes FROM games WHERE id = %s"), (game_id,))
        row = cur.fetchone()
        if not row:
            return redirect("/games?notice=找不到這款桌遊")
        notes = load_notes(row["notes"] if not isinstance(row, tuple) else row[0])
        if 0 <= note_index < len(notes):
            notes.pop(note_index)
            cur.execute(q("UPDATE games SET notes = %s WHERE id = %s"), (dump_notes(notes), game_id))
            conn.commit()
        return redirect(f"/games/{game_id}?notice=心得已刪除")
    finally:
        conn.close()


@app.get("/stats", response_class=HTMLResponse)
def stats_page(notice: str = "", player_filter: str = "", type_filter: str = ""):
    conn = get_conn()
    rows = winrate_detail_rows(conn)
    cur = conn.cursor()
    cur.execute("SELECT name FROM members ORDER BY name")
    players = [r[0] if isinstance(r, tuple) else r["name"] for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT category FROM games WHERE category <> '' ORDER BY category")
    game_types = [r[0] if isinstance(r, tuple) else r["category"] for r in cur.fetchall()]
    conn.close()

    filtered = []
    for r in rows:
        if player_filter and r["player"] != player_filter:
            continue
        if type_filter and r["game_type"] != type_filter:
            continue
        filtered.append(r)

    player_options = '<option value="">全部玩家</option>' + ''.join(f'<option value="{escape(p)}" {"selected" if p==player_filter else ""}>{escape(p)}</option>' for p in players)
    type_options = '<option value="">全部類型</option>' + ''.join(f'<option value="{escape(t)}" {"selected" if t==type_filter else ""}>{escape(t)}</option>' for t in game_types)
    rows_html = ''.join(
        f"<tr><td><a href='/stats?player_filter={urlq(r['player'])}'>{escape(r['player'])}</a></td><td><a href='/stats?type_filter={urlq(r['game_type'])}'>{escape(r['game_type'])}</a></td><td>{r['games']}</td><td>{r['wins']}</td><td>{r['win_rate']:.1f}%</td></tr>"
        for r in filtered
    ) or '<tr><td colspan="5">目前尚無資料</td></tr>'

    body = f"""
<div class="card"><div class="section-title"><div class="card-title">勝率分析</div><div class="small">依玩家與桌遊類型查看</div></div>
<form class="form-grid two" method="get" action="/stats" style="margin-bottom:10px">
  <div><label>玩家</label><select name="player_filter">{player_options}</select></div>
  <div><label>桌遊類型</label><select name="type_filter">{type_options}</select></div>
  <div class="btn-row" style="grid-column:1/-1"><button type="submit">套用篩選</button><a class="btn btn-soft" href="/stats">清除條件</a></div>
</form>
<div class="table-wrap"><table><thead><tr><th>玩家</th><th>類型</th><th>遊玩次數</th><th>勝場</th><th>勝率</th></tr></thead><tbody>{rows_html}</tbody></table></div></div>"""
    return page_template("勝率分析", body, "stats", notice)

@app.on_event("startup")
def startup_event():
    try:
        create_tables()
    except Exception as e:
        print("Startup create_tables failed:", e)




def df_stream_response(df: pd.DataFrame, filename: str) -> StreamingResponse:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue().encode("utf-8-sig")]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/export/games")
def export_games():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT name, category, bgg_score, user_rating, last_play_date, play_count FROM games ORDER BY name")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return df_stream_response(pd.DataFrame(rows), "games_export.csv")


@app.get("/export/members")
def export_members():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT name, favorite_types FROM members ORDER BY name")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return df_stream_response(pd.DataFrame(rows), "members_export.csv")


@app.get("/export/records")
def export_records():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT play_date, game_name, game_type, players, winners FROM play_records ORDER BY play_date DESC, id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return df_stream_response(pd.DataFrame(rows), "records_export.csv")


@app.post("/import/csv")
async def import_csv(kind: str = Form(...), file: UploadFile | None = File(None)):
    if file is None or not file.filename:
        return redirect('/?notice=請選擇 CSV 檔案')
    try:
        raw = await file.read()
        df = pd.read_csv(io.BytesIO(raw))
        df.columns = [str(c).strip() for c in df.columns]
    except Exception as exc:
        return redirect('/?notice=' + f'CSV 讀取失敗：{str(exc)[:120]}')

    conn = get_conn(); cur = conn.cursor()
    imported = 0
    try:
        if kind == 'games':
            required = {'name', 'category'}
            if not required.issubset(set(df.columns)):
                return redirect('/?notice=桌遊 CSV 缺少必要欄位：name, category')
            for _, row in df.fillna('').iterrows():
                name = str(row.get('name','')).strip()
                category = str(row.get('category','')).strip()
                if not name or not category:
                    continue
                bgg_score = parse_optional_float(str(row.get('bgg_score','')).strip()) if 'bgg_score' in df.columns else None
                user_rating = parse_optional_float(str(row.get('user_rating','')).strip()) if 'user_rating' in df.columns else None
                cur.execute(q('SELECT id FROM games WHERE name = %s'), (name,))
                existing = cur.fetchone()
                if existing:
                    cur.execute(q('UPDATE games SET category = %s, bgg_score = %s, user_rating = %s WHERE id = %s'), (category, bgg_score, user_rating, existing['id']))
                else:
                    cur.execute(q('INSERT INTO games (name, category, bgg_score, user_rating) VALUES (%s, %s, %s, %s)'), (name, category, bgg_score, user_rating))
                imported += 1
        elif kind == 'members':
            required = {'name'}
            if not required.issubset(set(df.columns)):
                return redirect('/?notice=成員 CSV 缺少必要欄位：name')
            for _, row in df.fillna('').iterrows():
                name = str(row.get('name','')).strip()
                favorite_types = str(row.get('favorite_types','')).strip()
                if not name:
                    continue
                cur.execute(q('SELECT id FROM members WHERE name = %s'), (name,))
                existing = cur.fetchone()
                if existing:
                    cur.execute(q('UPDATE members SET favorite_types = %s WHERE id = %s'), (favorite_types, existing['id']))
                else:
                    cur.execute(q('INSERT INTO members (name, favorite_types) VALUES (%s, %s)'), (name, favorite_types))
                imported += 1
        elif kind == 'records':
            required = {'play_date','game_name','game_type','players','winners'}
            if not required.issubset(set(df.columns)):
                return redirect('/?notice=紀錄 CSV 缺少必要欄位：play_date, game_name, game_type, players, winners')
            for _, row in df.fillna('').iterrows():
                play_date = str(row.get('play_date','')).strip()
                game_name = str(row.get('game_name','')).strip()
                game_type = str(row.get('game_type','')).strip()
                players = str(row.get('players','')).strip()
                winners = str(row.get('winners','')).strip()
                if not all([play_date, game_name, game_type, players, winners]):
                    continue
                cur.execute(q('SELECT id FROM games WHERE name = %s'), (game_name,))
                g = cur.fetchone()
                game_id = g['id'] if g else None
                cur.execute(q('INSERT INTO play_records (play_date, game_id, game_name, game_type, players, winners) VALUES (%s, %s, %s, %s, %s, %s)'), (play_date, game_id, game_name, game_type, players, winners))
                imported += 1
        else:
            return redirect('/?notice=不支援的匯入類型')

        conn.commit()
        sync_game_stats()
        return redirect('/?notice=' + f'已成功匯入 {imported} 筆 {kind} 資料')
    except Exception as exc:
        conn.rollback()
        return redirect('/?notice=' + f'匯入失敗：{str(exc)[:120]}')
    finally:
        conn.close()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)