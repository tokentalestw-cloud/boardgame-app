from __future__ import annotations

import os
import sqlite3
import shutil
from pathlib import Path
from collections import defaultdict
from datetime import date
from typing import Optional
from html import escape

import pandas as pd
import plotly.express as px
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
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

APP_TITLE = "桌遊遊玩紀錄系統｜永久保存版"

app = FastAPI(title=APP_TITLE)
app.mount("/game_images", StaticFiles(directory=str(GAME_IMAGE_DIR)), name="game_images")
app.mount("/member_images", StaticFiles(directory=str(MEMBER_IMAGE_DIR)), name="member_images")


def get_conn():
    if USE_POSTGRES:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql: str) -> str:
    return sql if USE_POSTGRES else sql.replace("%s", "?")


def unique_error(exc: Exception) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    if USE_POSTGRES and isinstance(exc, PgIntegrityError):
        return True
    return False


def create_tables() -> None:
    conn = get_conn()
    cur = conn.cursor()

    if USE_POSTGRES:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                image_path TEXT DEFAULT '',
                bgg_score DOUBLE PRECISION,
                user_rating DOUBLE PRECISION,
                intro TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                last_play_date TEXT DEFAULT '',
                play_count INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                favorite_types TEXT DEFAULT '',
                image_path TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS play_records (
                id BIGSERIAL PRIMARY KEY,
                play_date TEXT NOT NULL,
                game_id BIGINT,
                game_name TEXT NOT NULL,
                game_type TEXT NOT NULL,
                players TEXT NOT NULL,
                winners TEXT NOT NULL,
                CONSTRAINT fk_game FOREIGN KEY(game_id) REFERENCES games(id)
            )
            """
        )
    else:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                image_path TEXT DEFAULT '',
                bgg_score REAL,
                user_rating REAL,
                intro TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                last_play_date TEXT DEFAULT '',
                play_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                favorite_types TEXT DEFAULT '',
                image_path TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS play_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                play_date TEXT NOT NULL,
                game_id INTEGER,
                game_name TEXT NOT NULL,
                game_type TEXT NOT NULL,
                players TEXT NOT NULL,
                winners TEXT NOT NULL,
                FOREIGN KEY(game_id) REFERENCES games(id)
            )
            """
        )

    conn.commit()
    conn.close()
    sync_game_stats()


def parse_csv_names(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def copy_upload_to_library(upload: Optional[UploadFile], target_dir: Path, base_name: str) -> str:
    if upload is None or not upload.filename:
        return ""
    ext = Path(upload.filename).suffix.lower() or ".png"
    safe_name = "".join(ch for ch in base_name if ch.isalnum() or ch in "-_ ").strip().replace(" ", "_")
    target = target_dir / f"{safe_name}{ext}"
    with target.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return str(target)


def web_image_path(db_path: str) -> str:
    if not db_path:
        return ""
    p = Path(db_path)
    if p.parent.name == "game_images":
        return f"/game_images/{p.name}"
    if p.parent.name == "member_images":
        return f"/member_images/{p.name}"
    if "game_images" in db_path:
        return "/game_images/" + p.name
    if "member_images" in db_path:
        return "/member_images/" + p.name
    return ""


def sync_game_stats() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE games SET last_play_date = '', play_count = 0")
    cur.execute(
        """
        SELECT game_name, COUNT(*) AS play_count, MAX(play_date) AS last_play_date
        FROM play_records
        GROUP BY game_name
        """
    )
    for row in cur.fetchall():
        game_name = row["game_name"] if not isinstance(row, tuple) else row[0]
        play_count = row["play_count"] if not isinstance(row, tuple) else row[1]
        last_play_date = row["last_play_date"] if not isinstance(row, tuple) else row[2]
        cur.execute(q("UPDATE games SET play_count = %s, last_play_date = %s WHERE name = %s"), (play_count, last_play_date, game_name))
    conn.commit()
    conn.close()


def get_member_best_type(conn, member_name: str) -> tuple[Optional[str], Optional[str]]:
    cur = conn.cursor()
    cur.execute("SELECT game_type, players, winners FROM play_records")
    stats: dict[str, dict[str, int]] = {}
    for row in cur.fetchall():
        game_type = row["game_type"] or "未分類"
        players = parse_csv_names(row["players"])
        winners = parse_csv_names(row["winners"])
        if member_name not in players:
            continue
        stats.setdefault(game_type, {"games": 0, "wins": 0})
        stats[game_type]["games"] += 1
        if member_name in winners:
            stats[game_type]["wins"] += 1

    best_type = None
    best_rate = 0.0
    for game_type, stat in stats.items():
        if stat["games"] == 0:
            continue
        rate = stat["wins"] / stat["games"]
        if rate > best_rate:
            best_rate = rate
            best_type = game_type
    if best_type:
        return best_type, f"{best_rate * 100:.1f}%"
    return None, None


def get_top_player_by_game_type(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT game_type, players, winners FROM play_records")
    type_player_games: dict[tuple[str, str], int] = defaultdict(int)
    type_player_wins: dict[tuple[str, str], int] = defaultdict(int)

    for row in cur.fetchall():
        game_type = (row["game_type"] or "未分類").strip()
        players = parse_csv_names(row["players"])
        winners = parse_csv_names(row["winners"])
        for player in players:
            type_player_games[(game_type, player)] += 1
        for winner in winners:
            type_player_wins[(game_type, winner)] += 1

    cur.execute("SELECT name, image_path FROM members")
    member_image_map = {row["name"]: web_image_path(row["image_path"]) for row in cur.fetchall()}

    result = []
    game_types = sorted({k[0] for k in type_player_games.keys()})
    for game_type in game_types:
        candidates = []
        for (gt, player), total_games in type_player_games.items():
            if gt != game_type:
                continue
            wins = type_player_wins.get((gt, player), 0)
            win_rate = wins / total_games * 100 if total_games else 0
            candidates.append((player, total_games, wins, win_rate))
        if not candidates:
            continue
        candidates.sort(key=lambda x: (-x[3], -x[2], -x[1], x[0]))
        best_player, total_games, wins, win_rate = candidates[0]
        result.append({
            "game_type": game_type,
            "player": best_player,
            "games": total_games,
            "wins": wins,
            "win_rate": win_rate,
            "image_path": member_image_map.get(best_player, ""),
        })
    return result


def chart_html(df: pd.DataFrame, chart_type: str, x: str, y: Optional[str] = None, title: str = "") -> str:
    if df.empty:
        return '<div class="empty">目前尚無紀錄</div>'
    if chart_type == "bar":
        fig = px.bar(df, x=x, y=y, title=title, text_auto=True)
    elif chart_type == "pie":
        fig = px.pie(df, names=x, values=y, title=title)
    else:
        raise ValueError("Unsupported chart type")
    fig.update_layout(
        margin=dict(l=10, r=10, t=48, b=10),
        height=320,
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FFFFFF",
        font=dict(family="Arial, Noto Sans TC, Microsoft JhengHei", size=12),
    )
    return fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displayModeBar": False, "responsive": True})


def summary_stats(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM games")
    games = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) AS c FROM members")
    members = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) AS c FROM play_records")
    records = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(MAX(play_date), '') AS latest FROM play_records")
    row = cur.fetchone()
    latest = row[0] if isinstance(row, tuple) else row["latest"]
    latest = latest or "尚無紀錄"
    return {"games": games, "members": members, "records": records, "latest": latest}


def page_template(title: str, body: str, active: str = "dashboard", notice: str = "") -> HTMLResponse:
    nav_items = {
        "dashboard": ("儀表板", "/"),
        "games": ("桌遊", "/games"),
        "members": ("成員", "/members"),
        "records": ("紀錄", "/records"),
    }
    nav_html = "".join(
        f'<a class="nav-item {"active" if key == active else ""}" href="{href}">{label}</a>'
        for key, (label, href) in nav_items.items()
    )
    notice_html = f'<div class="notice">{escape(notice)}</div>' if notice else ""
    html = f"""
    <!doctype html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
      <title>{escape(title)}</title>
      <style>
        :root {{ --bg:#f5f7fb; --card:#fff; --text:#243447; --sub:#5b6b7a; --line:#dde6f0; --accent:#5b8ff9; --soft:#eef3fa; --warn:#fff7e6; --warn-line:#f6c667; }}
        * {{ box-sizing:border-box; }}
        body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft JhengHei", sans-serif; background:var(--bg); color:var(--text); }}
        .wrap {{ max-width: 920px; margin: 0 auto; padding: 12px 12px 36px; }}
        .header {{ position: sticky; top: 0; z-index: 20; background: rgba(245,247,251,.92); backdrop-filter: blur(8px); padding-top: env(safe-area-inset-top); }}
        .title {{ font-size: 22px; font-weight: 800; margin: 6px 0 10px; }}
        .nav {{ display:flex; gap:8px; overflow:auto; padding-bottom: 8px; }}
        .nav-item {{ white-space:nowrap; text-decoration:none; color:var(--text); background:var(--soft); padding:10px 14px; border-radius:999px; font-size:14px; }}
        .nav-item.active {{ background:var(--accent); color:#fff; }}
        .card {{ background:var(--card); border:1px solid var(--line); border-radius:18px; padding:14px; box-shadow:0 4px 16px rgba(36,52,71,.05); margin-top:12px; }}
        .card-title {{ font-size:18px; font-weight:800; margin:0 0 10px; }}
        .sub {{ color:var(--sub); font-size:13px; }}
        .notice {{ background:var(--warn); border:1px solid var(--warn-line); padding:12px 14px; border-radius:14px; margin-top:12px; }}
        .grid-2 {{ display:grid; grid-template-columns:1fr; gap:12px; }}
        .stats {{ display:grid; grid-template-columns:repeat(2, 1fr); gap:10px; }}
        .stat {{ background:linear-gradient(180deg,#fff,#f9fbff); border:1px solid var(--line); border-radius:16px; padding:14px; }}
        .stat .num {{ font-size:26px; font-weight:800; margin-top:6px; }}
        .form-grid {{ display:grid; grid-template-columns:1fr; gap:10px; }}
        label {{ display:block; font-size:13px; color:var(--sub); margin-bottom:4px; }}
        input, select, textarea {{ width:100%; border:1px solid var(--line); border-radius:12px; padding:12px; background:#fff; font:inherit; color:var(--text); }}
        textarea {{ min-height:100px; resize:vertical; }}
        button {{ border:0; background:var(--accent); color:#fff; padding:12px 16px; border-radius:12px; font-weight:700; font:inherit; }}
        .btn-row {{ display:flex; gap:8px; flex-wrap:wrap; }}
        .list {{ display:grid; gap:10px; }}
        .item {{ display:flex; gap:12px; align-items:flex-start; background:#fff; border:1px solid var(--line); border-radius:16px; padding:12px; }}
        .thumb {{ width:72px; height:72px; border-radius:16px; object-fit:cover; background:#edf2f8; flex:none; }}
        .thumb.circle {{ border-radius:999px; }}
        .item h3 {{ margin:0 0 4px; font-size:17px; }}
        .meta {{ color:var(--sub); font-size:13px; line-height:1.55; }}
        .spacer {{ flex:1; }}
        .danger {{ background:#fff2f2; color:#c0392b; border:1px solid #f2c5c5; }}
        .pill {{ display:inline-block; padding:4px 10px; border-radius:999px; background:var(--soft); color:var(--text); font-size:12px; margin-top:6px; }}
        .hero-grid {{ display:grid; gap:10px; }}
        .hero-card {{ display:flex; gap:12px; align-items:center; border:1px solid var(--line); border-radius:16px; padding:12px; background:linear-gradient(180deg,#fff,#fbfdff); }}
        .hero-avatar {{ width:56px; height:56px; border-radius:999px; object-fit:cover; background:#edf2f8; }}
        .empty {{ color:var(--sub); padding:10px 0; }}
        .small {{ font-size:12px; color:var(--sub); }}
        .table-wrap {{ overflow:auto; }}
        table {{ width:100%; border-collapse:collapse; font-size:14px; }}
        th, td {{ padding:10px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
        @media (min-width: 760px) {{ .grid-2 {{ grid-template-columns:1fr 1fr; }} .stats {{ grid-template-columns:repeat(4, 1fr); }} .form-grid.two {{ grid-template-columns:1fr 1fr; }} }}
      </style>
    </head>
    <body>
      <div class="header"><div class="wrap"><div class="title">{APP_TITLE}</div><div class="nav">{nav_html}</div></div></div>
      <div class="wrap">{notice_html}{body}</div>
    </body>
    </html>
    """
    return HTMLResponse(html)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, notice: str = ""):
    conn = get_conn()
    stats = summary_stats(conn)

    cur = conn.cursor()
    cur.execute("SELECT game_name, COUNT(*) AS cnt FROM play_records GROUP BY game_name ORDER BY cnt DESC, game_name ASC LIMIT 8")
    top_games = pd.DataFrame([dict(row) for row in cur.fetchall()])

    cur.execute("SELECT players, winners FROM play_records")
    gp = defaultdict(int)
    wins = defaultdict(int)
    for row in cur.fetchall():
        for p in parse_csv_names(row["players"]):
            gp[p] += 1
        for w in parse_csv_names(row["winners"]):
            wins[w] += 1
    member_rows = [{"player": p, "win_rate": round(wins[p] / gp[p] * 100, 1)} for p in gp]
    member_rows.sort(key=lambda x: (-x["win_rate"], x["player"]))
    top_members = pd.DataFrame(member_rows[:8])

    cur.execute("SELECT game_type, COUNT(*) AS cnt FROM play_records GROUP BY game_type ORDER BY cnt DESC, game_type ASC LIMIT 6")
    type_dist = pd.DataFrame([dict(row) for row in cur.fetchall()])

    heroes = get_top_player_by_game_type(conn)
    conn.close()

    hero_cards = "".join(
        f'''<div class="hero-card"><img class="hero-avatar" src="{escape(h['image_path'])}" {'style="display:none"' if not h['image_path'] else ''}><div><div><strong>👑 {escape(h['game_type'])}</strong></div><div>{escape(h['player'])}</div><div class="small">勝率 {h['win_rate']:.1f}%｜勝場 {h['wins']} / {h['games']}</div></div></div>'''
        for h in heroes
    ) or '<div class="empty">目前尚無紀錄</div>'

    body = f"""
    <div class="stats">
      <div class="stat"><div class="sub">桌遊數量</div><div class="num">{stats['games']}</div></div>
      <div class="stat"><div class="sub">成員數量</div><div class="num">{stats['members']}</div></div>
      <div class="stat"><div class="sub">遊玩紀錄</div><div class="num">{stats['records']}</div></div>
      <div class="stat"><div class="sub">最新遊玩日</div><div class="num" style="font-size:18px">{escape(stats['latest'])}</div></div>
    </div>
    <div class="grid-2">
      <div class="card"><div class="card-title">最多遊玩的桌遊</div>{chart_html(top_games, 'bar', 'game_name', 'cnt', '最多遊玩的桌遊')}</div>
      <div class="card"><div class="card-title">玩家勝率 Top 8</div>{chart_html(top_members, 'bar', 'player', 'win_rate', '玩家勝率 Top 8')}</div>
      <div class="card"><div class="card-title">桌遊類型分布</div>{chart_html(type_dist, 'pie', 'game_type', 'cnt', '桌遊類型分布')}</div>
      <div class="card"><div class="card-title">各類型桌遊勝率最高的玩家</div><div class="hero-grid">{hero_cards}</div></div>
    </div>
    """
    return page_template("儀表板", body, active="dashboard", notice=notice)


@app.get("/games", response_class=HTMLResponse)
def games_page(request: Request, notice: str = ""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, category, image_path, bgg_score, user_rating, COALESCE(last_play_date,'') AS last_play_date, COALESCE(play_count,0) AS play_count FROM games ORDER BY name")
    rows = cur.fetchall()
    conn.close()

    items = []
    for game in rows:
        image_url = web_image_path(game["image_path"])
        items.append(
            f'''<div class="item"><img class="thumb" src="{escape(image_url)}" {'style="display:none"' if not image_url else ''}><div><h3>{escape(game['name'])}</h3><div class="meta">類型：{escape(game['category'])}<br>BGG：{game['bgg_score'] if game['bgg_score'] is not None else '未填寫'}<br>玩家評分：{game['user_rating'] if game['user_rating'] is not None else '未填寫'}<br>遊玩次數：{game['play_count']}<br>最後遊玩：{escape(game['last_play_date'] or '尚無紀錄')}</div></div><div class="spacer"></div><form method="post" action="/games/{game['id']}/delete" onsubmit="return confirm('確定要刪除這款桌遊嗎？');"><button class="danger" type="submit">刪除</button></form></div>'''
        )
    body = f"""
    <div class="card"><div class="card-title">新增桌遊</div>
      <form class="form-grid two" method="post" action="/games" enctype="multipart/form-data">
        <div><label>桌遊名稱</label><input name="name" required></div>
        <div><label>桌遊類型</label><input name="category" required></div>
        <div><label>BGG 分數</label><input name="bgg_score" inputmode="decimal" placeholder="例如 7.8"></div>
        <div><label>玩家自評分</label><input name="user_rating" inputmode="decimal" placeholder="例如 8.5"></div>
        <div style="grid-column:1/-1"><label>圖片</label><input type="file" name="image"></div>
        <div style="grid-column:1/-1" class="btn-row"><button type="submit">儲存桌遊</button></div>
      </form>
    </div>
    <div class="card"><div class="card-title">桌遊列表</div><div class="list">{''.join(items) or '<div class="empty">目前尚無桌遊</div>'}</div></div>
    """
    return page_template("桌遊", body, active="games", notice=notice)


@app.post("/games")
async def add_game(name: str = Form(...), category: str = Form(...), bgg_score: str = Form(""), user_rating: str = Form(""), image: UploadFile | None = File(None)):
    conn = get_conn()
    cur = conn.cursor()
    try:
        image_path = copy_upload_to_library(image, GAME_IMAGE_DIR, name) if image and image.filename else ""
        bgg_value = float(bgg_score) if bgg_score.strip() else None
        rating_value = float(user_rating) if user_rating.strip() else None
        cur.execute(q("INSERT INTO games (name, category, image_path, bgg_score, user_rating) VALUES (%s, %s, %s, %s, %s)"), (name.strip(), category.strip(), image_path, bgg_value, rating_value))
        conn.commit()
        return redirect("/games?notice=桌遊已新增")
    except Exception as exc:
        conn.rollback()
        if unique_error(exc):
            return redirect("/games?notice=桌遊名稱已存在")
        return redirect(f"/games?notice=新增失敗：{str(exc)[:120]}")
    finally:
        conn.close()


@app.post("/games/{game_id}/delete")
def delete_game(game_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(q("DELETE FROM games WHERE id = %s"), (game_id,))
    conn.commit()
    conn.close()
    return redirect("/games?notice=桌遊已刪除")


@app.get("/members", response_class=HTMLResponse)
def members_page(request: Request, notice: str = ""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, favorite_types, image_path FROM members ORDER BY name")
    rows = cur.fetchall()

    cards = []
    for member in rows:
        best_type, best_rate = get_member_best_type(conn, member["name"])
        stats_cur = conn.cursor()
        stats_cur.execute("SELECT play_date, game_name, players, winners FROM play_records ORDER BY play_date DESC, id DESC")
        games_played = 0
        wins = 0
        recent_games = []
        seen = set()
        for row in stats_cur.fetchall():
            players = parse_csv_names(row["players"])
            winners = parse_csv_names(row["winners"])
            if member["name"] in players:
                games_played += 1
                if member["name"] in winners:
                    wins += 1
                if row["game_name"] not in seen and len(recent_games) < 5:
                    recent_games.append(row["game_name"])
                    seen.add(row["game_name"])
        rate = wins / games_played * 100 if games_played else 0
        image_url = web_image_path(member["image_path"])
        best_line = f"<div class='pill'>🔥 最強類型：{escape(best_type)}（{best_rate}）</div>" if best_type else ""
        cards.append(
            f'''<div class="item"><img class="thumb circle" src="{escape(image_url)}" {'style="display:none"' if not image_url else ''}><div><h3>{escape(member['name'])}</h3><div class="meta">擅長：{escape(member['favorite_types'] or '未填寫')}<br>總場數：{games_played}｜勝場：{wins}｜勝率：{rate:.1f}%<br>近期遊玩：{escape('、'.join(recent_games) if recent_games else '尚無紀錄')}</div>{best_line}</div><div class="spacer"></div><form method="post" action="/members/{member['id']}/delete" onsubmit="return confirm('確定要刪除這位成員嗎？');"><button class="danger" type="submit">刪除</button></form></div>'''
        )
    conn.close()

    body = f"""
    <div class="card"><div class="card-title">新增成員</div>
      <form class="form-grid two" method="post" action="/members" enctype="multipart/form-data">
        <div><label>成員姓名</label><input name="name" required></div>
        <div><label>擅長桌遊類型</label><input name="favorite_types"></div>
        <div style="grid-column:1/-1"><label>照片</label><input type="file" name="image"></div>
        <div style="grid-column:1/-1" class="btn-row"><button type="submit">儲存成員</button></div>
      </form>
    </div>
    <div class="card"><div class="card-title">成員列表</div><div class="list">{''.join(cards) or '<div class="empty">目前尚無成員</div>'}</div></div>
    """
    return page_template("成員", body, active="members", notice=notice)


@app.post("/members")
async def add_member(name: str = Form(...), favorite_types: str = Form(""), image: UploadFile | None = File(None)):
    conn = get_conn()
    cur = conn.cursor()
    try:
        image_path = copy_upload_to_library(image, MEMBER_IMAGE_DIR, name) if image and image.filename else ""
        cur.execute(q("INSERT INTO members (name, favorite_types, image_path) VALUES (%s, %s, %s)"), (name.strip(), favorite_types.strip(), image_path))
        conn.commit()
        return redirect("/members?notice=成員已新增")
    except Exception as exc:
        conn.rollback()
        if unique_error(exc):
            return redirect("/members?notice=成員姓名已存在")
        return redirect(f"/members?notice=新增失敗：{str(exc)[:120]}")
    finally:
        conn.close()


@app.post("/members/{member_id}/delete")
def delete_member(member_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(q("DELETE FROM members WHERE id = %s"), (member_id,))
    conn.commit()
    conn.close()
    return redirect("/members?notice=成員已刪除")


@app.get("/records", response_class=HTMLResponse)
def records_page(request: Request, notice: str = ""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, category FROM games ORDER BY name")
    games = cur.fetchall()
    cur.execute("SELECT name FROM members ORDER BY name")
    members = cur.fetchall()
    cur.execute("SELECT id, play_date, game_name, game_type, players, winners FROM play_records ORDER BY play_date DESC, id DESC")
    records = cur.fetchall()
    conn.close()

    game_options = "".join(f'<option value="{escape(g["name"])}">{escape(g["name"])}｜{escape(g["category"])} </option>' for g in games)
    member_options = "".join(f'<option value="{escape(m["name"])}">' for m in members)
    record_rows = "".join(
        f'''<tr><td>{escape(r['play_date'])}</td><td>{escape(r['game_name'])}</td><td>{escape(r['game_type'])}</td><td>{escape(r['players'])}</td><td>{escape(r['winners'])}</td><td><form method="post" action="/records/{r['id']}/delete" onsubmit="return confirm('確定要刪除這筆紀錄嗎？');"><button class="danger" type="submit">刪除</button></form></td></tr>'''
        for r in records
    )

    body = f"""
    <div class="card"><div class="card-title">新增遊玩紀錄</div>
      <form class="form-grid two" method="post" action="/records">
        <div><label>日期</label><input type="date" name="play_date" value="{date.today().isoformat()}" required></div>
        <div><label>桌遊</label><input name="game_name" list="games_list" required><datalist id="games_list">{game_options}</datalist></div>
        <div><label>桌遊類型（可直接填，或依桌遊帶入）</label><input name="game_type" placeholder="例如 策略 / 派對"></div>
        <div><label>玩家名單（逗號分隔）</label><input name="players" list="members_list" placeholder="福, 芸, 棋" required></div>
        <div style="grid-column:1/-1"><label>勝者（逗號分隔）</label><input name="winners" list="members_list" placeholder="福" required></div>
        <datalist id="members_list">{member_options}</datalist>
        <div style="grid-column:1/-1" class="btn-row"><button type="submit">新增紀錄</button></div>
      </form><div class="small" style="margin-top:8px;">桌遊類型若留空，系統會自動從桌遊資料庫帶入。</div>
    </div>
    <div class="card"><div class="card-title">遊玩紀錄</div><div class="table-wrap"><table><thead><tr><th>日期</th><th>桌遊</th><th>類型</th><th>玩家</th><th>勝者</th><th></th></tr></thead><tbody>{record_rows or '<tr><td colspan="6">目前尚無紀錄</td></tr>'}</tbody></table></div></div>
    """
    return page_template("紀錄", body, active="records", notice=notice)


@app.post("/records")
def add_record(play_date: str = Form(...), game_name: str = Form(...), game_type: str = Form(""), players: str = Form(...), winners: str = Form(...)):
    conn = get_conn()
    cur = conn.cursor()
    game_name = game_name.strip()
    cur.execute(q("SELECT id, category FROM games WHERE name = %s"), (game_name,))
    game_row = cur.fetchone()
    game_id = game_row["id"] if game_row else None
    category = game_type.strip() or (game_row["category"] if game_row else "未分類")

    player_list = parse_csv_names(players)
    winner_list = parse_csv_names(winners)
    invalid = [w for w in winner_list if w not in player_list]
    if invalid:
        conn.close()
        return redirect("/records?notice=勝者必須在玩家名單中")

    cur.execute(q("INSERT INTO play_records (play_date, game_id, game_name, game_type, players, winners) VALUES (%s, %s, %s, %s, %s, %s)"), (play_date, game_id, game_name, category, ", ".join(player_list), ", ".join(winner_list)))
    conn.commit()
    conn.close()
    sync_game_stats()
    return redirect("/records?notice=遊玩紀錄已新增")


@app.post("/records/{record_id}/delete")
def delete_record(record_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(q("DELETE FROM play_records WHERE id = %s"), (record_id,))
    conn.commit()
    conn.close()
    sync_game_stats()
    return redirect("/records?notice=紀錄已刪除")


@app.on_event("startup")
def startup_event():
    create_tables()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("boardgame_web_render_postgres:app", host="0.0.0.0", port=port, reload=False)
