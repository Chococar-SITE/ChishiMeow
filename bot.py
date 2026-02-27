import os
import json
import re
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import tasks
import aiosqlite
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise ValueError("環境變數 DISCORD_BOT_TOKEN 未設定")

DB_PATH              = os.getenv("DB_PATH", "./track.db")
THREADS_USERNAME     = os.getenv("THREADS_USERNAME", "")
THREADS_CHANNEL_ID   = os.getenv("THREADS_CHANNEL_ID", "")
THREADS_COOKIES_PATH = os.getenv("THREADS_COOKIES_PATH", "./threads_cookies.json")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ---------- DB ----------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS rules_keywords (
            guild_id TEXT,
            keyword  TEXT,
            PRIMARY KEY (guild_id, keyword)
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id         TEXT,
            channel_id       TEXT,
            message_id       TEXT,
            author_id        TEXT,
            author_tag       TEXT,
            created_at       TEXT,
            content          TEXT,
            matched_keywords TEXT,
            stickers         TEXT,
            emojis           TEXT,
            jump_url         TEXT
        )
        """)
        # Migration：舊版欄位不符時自動重建
        cur = await db.execute("PRAGMA table_info(threads_state)")
        cols = [row[1] for row in await cur.fetchall()]
        if cols and "init_seen_ids" not in cols:
            await db.execute("DROP TABLE threads_state")
            await db.commit()

        await db.execute("""
        CREATE TABLE IF NOT EXISTS threads_state (
            username      TEXT PRIMARY KEY,
            seen_ids      TEXT NOT NULL DEFAULT '[]',
            init_seen_ids TEXT NOT NULL DEFAULT '[]'
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS keyword_counts (
            guild_id    TEXT,
            author_id   TEXT,
            author_tag  TEXT,
            keyword     TEXT,
            count       INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT,
            PRIMARY KEY (guild_id, author_id, keyword)
        )
        """)
        await db.commit()


# ── keyword helpers ──

async def get_keywords(guild_id: str) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT keyword FROM rules_keywords WHERE guild_id=?", (guild_id,)
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def add_keyword(guild_id: str, keyword: str):
    keyword = keyword.strip()
    if not keyword:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO rules_keywords (guild_id, keyword) VALUES (?,?)",
            (guild_id, keyword),
        )
        await db.commit()


async def remove_keyword(guild_id: str, keyword: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM rules_keywords WHERE guild_id=? AND keyword=?",
            (guild_id, keyword),
        )
        await db.commit()


async def insert_log(**kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO logs (
                guild_id, channel_id, message_id, author_id, author_tag,
                created_at, content, matched_keywords, stickers, emojis, jump_url
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                kwargs["guild_id"],
                kwargs["channel_id"],
                kwargs["message_id"],
                kwargs["author_id"],
                kwargs["author_tag"],
                kwargs["created_at"],
                kwargs["content"],
                kwargs["matched_keywords"],
                kwargs["stickers"],
                kwargs["emojis"],
                kwargs["jump_url"],
            ),
        )
        await db.commit()


# ── threads state helpers ──

async def get_threads_state(username: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT seen_ids, init_seen_ids FROM threads_state WHERE username=?", (username,)
        )
        row = await cur.fetchone()
        if row:
            return {
                "seen_ids": json.loads(row[0]),       # list，保留插入順序
                "init_seen_ids": json.loads(row[1]),
            }
        return {"seen_ids": [], "init_seen_ids": []}


async def init_threads_state(username: str, ids: list[str]):
    """第一次執行時呼叫：同時設定 seen_ids 和 init_seen_ids。"""
    encoded = json.dumps(ids, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO threads_state (username, seen_ids, init_seen_ids)
               VALUES (?,?,?)""",
            (username, encoded, encoded),
        )
        await db.commit()


async def add_threads_seen_ids(username: str, new_ids: list[str]):
    state = await get_threads_state(username)
    existing: list[str] = state["seen_ids"]
    existing_set = set(existing)
    # 新 ID 依序追加到尾端（保留插入順序，不重複）
    for nid in new_ids:
        if nid not in existing_set:
            existing.append(nid)
            existing_set.add(nid)
    trimmed = existing[-50:]  # 取最後 50 筆（最新的），順序確定
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE threads_state SET seen_ids=? WHERE username=?",
            (json.dumps(trimmed, ensure_ascii=False), username),
        )
        await db.commit()


# ── keyword count helpers ──

async def increment_keyword_count(
    guild_id: str, author_id: str, author_tag: str, keyword: str
):
    ts = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO keyword_counts (guild_id, author_id, author_tag, keyword, count, last_seen_at)
            VALUES (?,?,?,?,1,?)
            ON CONFLICT(guild_id, author_id, keyword) DO UPDATE SET
                count        = count + 1,
                author_tag   = excluded.author_tag,
                last_seen_at = excluded.last_seen_at
            """,
            (guild_id, author_id, author_tag, keyword, ts),
        )
        await db.commit()


async def set_keyword_count(
    guild_id: str, author_id: str, author_tag: str, keyword: str, new_count: int
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO keyword_counts (guild_id, author_id, author_tag, keyword, count, last_seen_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(guild_id, author_id, keyword) DO UPDATE SET
                count      = excluded.count,
                author_tag = excluded.author_tag
            """,
            (guild_id, author_id, author_tag, keyword, new_count, now_iso()),
        )
        await db.commit()


async def delete_keyword_counts(
    guild_id: str,
    keyword: str | None = None,
    author_id: str | None = None,
) -> int:
    """刪除（重置）符合條件的計數列，回傳影響筆數。"""
    conditions: list[str] = ["guild_id = ?"]
    params: list = [guild_id]
    if keyword is not None:
        conditions.append("keyword = ?")
        params.append(keyword)
    if author_id is not None:
        conditions.append("author_id = ?")
        params.append(author_id)
    where = " AND ".join(conditions)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"DELETE FROM keyword_counts WHERE {where}", params)
        await db.commit()
        return cur.rowcount


async def get_keyword_counts(
    guild_id: str,
    keyword: str | None = None,
    author_id: str | None = None,
) -> list[dict]:
    """
    查詢計數，支援四種組合：
    - 全部
    - 篩選 keyword
    - 篩選 author_id
    - 同時篩選
    回傳 list[dict]，依 count DESC 排序。
    """
    conditions: list[str] = ["guild_id = ?"]
    params: list = [guild_id]
    if keyword is not None:
        conditions.append("keyword = ?")
        params.append(keyword)
    if author_id is not None:
        conditions.append("author_id = ?")
        params.append(author_id)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT author_tag, author_id, keyword, count, last_seen_at
        FROM keyword_counts
        WHERE {where}
        ORDER BY count DESC
        LIMIT 25
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
    return [
        {
            "author_tag": r[0],
            "author_id": r[1],
            "keyword": r[2],
            "count": r[3],
            "last_seen_at": r[4],
        }
        for r in rows
    ]


# ---------- Helpers ----------

_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")


def extract_custom_emojis(text: str) -> list[str]:
    return _CUSTOM_EMOJI_RE.findall(text)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- Threads Scraping ----------

async def fetch_latest_threads_posts(username: str) -> list[dict] | None:
    """
    使用 Playwright 抓取 Threads 公開個人頁面的最新貼文（前 5 則）。
    回傳 [{"post_id": str, "url": str, "text": str}, ...] 或 None（失敗時）。
    回傳多則是為了跳過置頂貼文。
    """
    profile_url = f"https://www.threads.net/@{username}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="zh-TW",
            )
            # 載入已儲存的登入 Cookie
            if os.path.exists(THREADS_COOKIES_PATH):
                with open(THREADS_COOKIES_PATH, encoding="utf-8") as f:
                    await context.add_cookies(json.load(f))
                print(f"[Threads] 已載入 Cookie：{THREADS_COOKIES_PATH}")
            else:
                print("[Threads] 未找到 Cookie 檔案，以未登入狀態嘗試")

            page = await context.new_page()
            await page.goto(profile_url, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(3_000)

            final_url = page.url
            print(f"[Threads] 最終頁面 URL：{final_url}")

            # 登入牆偵測
            if any(k in final_url for k in ("login", "accounts", "signup")):
                print("[Threads] 偵測到登入牆，無法在未登入狀態下查看此頁面")
                return None

            # 從 DOM 取得貼文清單，同時偵測置頂標記
            raw: list[dict] = await page.evaluate(r"""
                () => {
                    function isPinned(linkEl) {
                        let el = linkEl;
                        for (let i = 0; i < 8; i++) {
                            if (!el.parentElement) break;
                            el = el.parentElement;
                            // 超過單篇容器就停
                            if (el.querySelectorAll('a[href*="/post/"]').length > 3) break;
                            // 偵測 "Pinned" 文字節點
                            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                            let node;
                            while ((node = walker.nextNode())) {
                                const t = node.textContent?.trim();
                                if (t === 'Pinned' || t === '置頂') return true;
                            }
                            // 偵測 aria-label
                            for (const a of el.querySelectorAll('[aria-label]')) {
                                if (a.getAttribute('aria-label').toLowerCase().includes('pin')) return true;
                            }
                        }
                        return false;
                    }

                    const seen = new Set();
                    const results = [];
                    for (const link of document.querySelectorAll('a[href*="/post/"]')) {
                        const m = link.href.match(/\/post\/([^/?#]+)/);
                        if (!m) continue;
                        const pid = m[1];
                        if (seen.has(pid)) continue;
                        seen.add(pid);
                        results.push({ pid, pinned: isPinned(link) });
                        if (results.length >= 10) break;
                    }
                    return results;
                }
            """)

            if not raw:
                page_title = await page.title()
                print(f"[Threads] 找不到貼文連結，頁面標題：{page_title!r}")
                return None

            results: list[dict] = []
            for item in raw:
                pid: str = item["pid"]
                pinned: bool = item["pinned"]
                # 過濾掉非此用戶的貼文（若有的話）
                clean_url = f"https://www.threads.com/@{username}/post/{pid}"
                results.append({"post_id": pid, "url": clean_url, "pinned": pinned})

            pinned_ids = [r["post_id"] for r in results if r["pinned"]]
            print(f"[Threads] 共 {len(results)} 則，置頂：{pinned_ids}")
            return results

        except Exception as e:
            print(f"[Threads] 抓取 @{username} 失敗：{e}")
            return None
        finally:
            await browser.close()


# ---------- Background Task ----------

@tasks.loop(minutes=10)
async def check_threads_task():
    if not THREADS_USERNAME or not THREADS_CHANNEL_ID:
        return

    try:
        posts = await fetch_latest_threads_posts(THREADS_USERNAME)
        if posts is None:
            print(f"[Threads] 無法取得 @{THREADS_USERNAME} 的貼文")
            return

        state = await get_threads_state(THREADS_USERNAME)
        seen_list: list[str] = state["seen_ids"]
        seen_set = set(seen_list)
        fetched_ids = [p["post_id"] for p in posts]

        # 第一次執行：同時初始化 seen_ids 和 init_seen_ids，不發通知
        if not seen_list:
            await init_threads_state(THREADS_USERNAME, fetched_ids)
            print(f"[Threads] 初始化 @{THREADS_USERNAME}，記錄 {len(fetched_ids)} 則貼文 ID")
            return

        # 找出所有未見過的貼文
        new_posts = [p for p in posts if p["post_id"] not in seen_set]
        if not new_posts:
            return  # 沒有新貼文

        # 優先通知非置頂；若新貼文全是置頂（罕見），仍全數通知以免漏報
        notify_posts = [p for p in new_posts if not p["pinned"]] or new_posts

        # 先更新 seen（含置頂），再發通知（避免重複通知）
        await add_threads_seen_ids(THREADS_USERNAME, fetched_ids)

        channel = client.get_channel(int(THREADS_CHANNEL_ID))
        if not isinstance(channel, discord.TextChannel):
            print(f"[Threads] 找不到頻道 {THREADS_CHANNEL_ID}")
            return

        for post in notify_posts:
            embed = discord.Embed(
                title=f"@{THREADS_USERNAME} 發布了新貼文",
                url=post["url"],
                color=0x000000,
            )
            embed.set_footer(text="Threads · 自動偵測")
            embed.timestamp = datetime.now(timezone.utc)
            await channel.send(embed=embed)

        print(f"[Threads] @{THREADS_USERNAME} 發送了 {len(notify_posts)} 則新貼文通知（略過置頂 {len(new_posts) - len(notify_posts)} 則）")

    except Exception as e:
        print(f"[Threads] 背景檢查失敗：{e}")


@check_threads_task.before_loop
async def before_check_threads():
    await client.wait_until_ready()


# ---------- Slash Commands — keyword tracking ----------

@tree.command(name="track_add", description="新增追蹤關鍵字")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(keyword="要追蹤的文字")
async def track_add(interaction: discord.Interaction, keyword: str):
    if not interaction.guild:
        await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
        return
    await add_keyword(str(interaction.guild_id), keyword)
    await interaction.response.send_message(f"已加入關鍵字：`{keyword}`", ephemeral=True)


@tree.command(name="track_remove", description="移除追蹤關鍵字")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(keyword="要移除的文字")
async def track_remove(interaction: discord.Interaction, keyword: str):
    if not interaction.guild:
        await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
        return
    await remove_keyword(str(interaction.guild_id), keyword)
    await interaction.response.send_message(f"已移除關鍵字：`{keyword}`", ephemeral=True)


@tree.command(name="track_list", description="列出目前追蹤關鍵字")
@app_commands.default_permissions(administrator=True)
async def track_list(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
        return
    kws = await get_keywords(str(interaction.guild_id))
    if not kws:
        await interaction.response.send_message("目前沒有追蹤關鍵字。", ephemeral=True)
        return
    lines = "\n".join(f"- `{k}`" for k in kws)
    await interaction.response.send_message(f"追蹤關鍵字：\n{lines}", ephemeral=True)


# ---------- Slash Commands — keyword stats ----------

@tree.command(name="track_stats", description="查看關鍵字被特定人說過的次數")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    keyword="篩選特定關鍵字（留空 = 全部）",
    user="篩選特定成員（留空 = 全部）",
)
async def track_stats(
    interaction: discord.Interaction,
    keyword: str | None = None,
    user: discord.Member | None = None,
):
    if not interaction.guild:
        await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    author_id = str(user.id) if user else None
    kw_filter = keyword.strip() if keyword else None

    rows = await get_keyword_counts(guild_id, keyword=kw_filter, author_id=author_id)

    if not rows:
        await interaction.response.send_message("目前沒有符合條件的統計資料。", ephemeral=True)
        return

    # 組成 embed
    title_parts: list[str] = []
    if kw_filter:
        title_parts.append(f"關鍵字「{kw_filter}」")
    if user:
        title_parts.append(f"@{user.display_name}")
    title = "、".join(title_parts) + " 的統計" if title_parts else "關鍵字統計"

    embed = discord.Embed(title=title, color=0x5865F2)

    lines: list[str] = []
    for r in rows:
        kw_str = f"`{r['keyword']}`"
        who = f"**{r['author_tag']}**"
        lines.append(f"{who} — {kw_str}：**{r['count']}** 次")

    # Discord embed value 上限 1024 字元
    chunk = "\n".join(lines)
    if len(chunk) > 1020:
        chunk = chunk[:1020] + "\n…"
    embed.add_field(name="排行（前 25）", value=chunk, inline=False)
    embed.set_footer(text=f"共 {len(rows)} 筆")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="track_stats_set", description="手動設定某人某關鍵字的次數")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    user="要修改的成員",
    keyword="關鍵字",
    count="新的次數",
)
async def track_stats_set(
    interaction: discord.Interaction,
    user: discord.Member,
    keyword: str,
    count: int,
):
    if not interaction.guild:
        await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
        return
    if count < 0:
        await interaction.response.send_message("次數不能為負數。", ephemeral=True)
        return
    await set_keyword_count(
        str(interaction.guild_id),
        str(user.id),
        str(user),
        keyword.strip(),
        count,
    )
    await interaction.response.send_message(
        f"已將 **{user.display_name}** 的關鍵字 `{keyword.strip()}` 次數設為 **{count}**。",
        ephemeral=True,
    )


@tree.command(name="track_stats_reset", description="清除關鍵字次數統計（可篩選關鍵字或成員）")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    keyword="只清除此關鍵字的記錄（留空 = 不限）",
    user="只清除此成員的記錄（留空 = 不限）",
)
async def track_stats_reset(
    interaction: discord.Interaction,
    keyword: str | None = None,
    user: discord.Member | None = None,
):
    if not interaction.guild:
        await interaction.response.send_message("此指令只能在伺服器內使用。", ephemeral=True)
        return
    kw_filter = keyword.strip() if keyword else None
    author_id = str(user.id) if user else None
    deleted = await delete_keyword_counts(
        str(interaction.guild_id), keyword=kw_filter, author_id=author_id
    )
    parts: list[str] = []
    if kw_filter:
        parts.append(f"關鍵字 `{kw_filter}`")
    if user:
        parts.append(f"成員 **{user.display_name}**")
    scope = "、".join(parts) if parts else "所有統計"
    await interaction.response.send_message(
        f"已清除 {scope} 的次數記錄，共 {deleted} 筆。", ephemeral=True
    )


# ---------- Slash Commands — Help ----------

@tree.command(name="help", description="顯示所有指令說明")
@app_commands.default_permissions(administrator=True)
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="指令說明",
        color=0x5865F2,
    )
    embed.add_field(
        name="🔍 關鍵字追蹤",
        value=(
            "`/track_add <keyword>` — 新增追蹤關鍵字\n"
            "`/track_remove <keyword>` — 移除追蹤關鍵字\n"
            "`/track_list` — 列出目前所有追蹤關鍵字\n"
            "`/track_stats [keyword] [user]` — 查看關鍵字被說次數統計\n"
            "`/track_stats_set <user> <keyword> <count>` — 手動設定次數\n"
            "`/track_stats_reset [keyword] [user]` — 清除次數記錄\n"
            "訊息含有關鍵字、貼圖或自訂 emoji 時，自動記錄到資料庫"
        ),
        inline=False,
    )
    embed.add_field(
        name="🧵 Threads 監控",
        value=(
            f"`/threads_check` — 立即查詢 @{THREADS_USERNAME or '（未設定）'} 的最新貼文\n"
            "每 10 分鐘自動偵測一次，有新貼文時發送通知\n"
            "監控對象與通知頻道設定於 `.env`"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- Slash Commands — Threads ----------

@tree.command(name="threads_check", description="立即手動檢查 Threads 最新貼文")
@app_commands.default_permissions(administrator=True)
async def threads_check(interaction: discord.Interaction):
    if not THREADS_USERNAME:
        await interaction.response.send_message(
            "尚未在 `.env` 設定 `THREADS_USERNAME`。", ephemeral=True
        )
        return

    await interaction.response.defer()
    posts = await fetch_latest_threads_posts(THREADS_USERNAME)

    if posts is None:
        await interaction.followup.send(
            f"無法取得 **@{THREADS_USERNAME}** 的貼文。\n"
            "可能原因：用戶不存在、帳號為私密、或 Threads 頁面需要登入。"
        )
        return

    # 優先回傳第一則非置頂貼文（DOM 直接偵測）
    non_pinned = [p for p in posts if not p["pinned"]]
    if non_pinned:
        post = non_pinned[0]
    else:
        post = posts[0]  # 全是置頂時 fallback

    embed = discord.Embed(
        title=f"@{THREADS_USERNAME} 的最新貼文",
        url=post["url"],
        color=0x000000,
    )
    embed.set_footer(text="Threads · 手動查詢")
    embed.timestamp = datetime.now(timezone.utc)
    await interaction.followup.send(embed=embed)


# ---------- Events ----------

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        return

    guild_id = str(message.guild.id)
    kws = await get_keywords(guild_id)

    content = message.content or ""
    content_lower = content.lower()

    matched = [k for k in kws if k.lower() in content_lower]

    stickers = [
        {
            "id": str(st.id),
            "name": st.name,
            "format": str(st.format) if getattr(st, "format", None) else None,
        }
        for st in (getattr(message, "stickers", None) or [])
    ]

    custom_emojis = extract_custom_emojis(content)

    if not matched and not stickers and not custom_emojis:
        return

    # 累加每個符合的關鍵字次數
    for kw in matched:
        await increment_keyword_count(
            guild_id,
            str(message.author.id),
            str(message.author),
            kw,
        )

    await insert_log(
        guild_id=guild_id,
        channel_id=str(message.channel.id),
        message_id=str(message.id),
        author_id=str(message.author.id),
        author_tag=str(message.author),
        created_at=now_iso(),
        content=content,
        matched_keywords=json.dumps(matched, ensure_ascii=False),
        stickers=json.dumps(stickers, ensure_ascii=False),
        emojis=json.dumps(custom_emojis, ensure_ascii=False),
        jump_url=message.jump_url,
    )


@client.event
async def on_ready():
    await init_db()
    await tree.sync()
    if not check_threads_task.is_running():
        check_threads_task.start()
    print(f"Logged in as {client.user}  (ID: {client.user.id})")  # type: ignore[union-attr]
    print("------")


client.run(TOKEN)
