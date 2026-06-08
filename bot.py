import os
import json
import re
import subprocess
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
STARTUP_CHANNEL_ID   = os.getenv("STARTUP_CHANNEL_ID", "")  # 開機訊息頻道（settings 可覆寫）


def _detect_version() -> str:
    """取得目前 git commit short SHA 作為版本號（除錯用）；非 git 環境回傳 'unknown'。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        sha = result.stdout.strip()
        return sha if result.returncode == 0 and sha else "unknown"
    except Exception:
        return "unknown"


VERSION = _detect_version()

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
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
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
    # 本輪抓到的 id 一律移到尾端刷新最近度：避免仍在頁面上的貼文（含置頂）
    # 被 50 筆視窗淘汰後、又因 pin 偵測 miss 而重新誤判為新貼文
    new_set = set(new_ids)
    kept = [i for i in existing if i not in new_set]
    refreshed = kept + list(new_ids)
    trimmed = refreshed[-50:]  # 取最後 50 筆（最新的），順序確定
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE threads_state SET seen_ids=? WHERE username=?",
            (json.dumps(trimmed, ensure_ascii=False), username),
        )
        await db.commit()


# ── settings helpers ──

THREADS_NOTIFY_ROLE_KEY = "threads_notify_role"
STARTUP_CHANNEL_KEY     = "startup_channel"


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        if value is None:
            await db.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                (key, value),
            )
        await db.commit()


async def get_threads_notify_content() -> tuple[str | None, discord.AllowedMentions]:
    """回傳 (要標記的身分組文字, AllowedMentions)；未設定時為 (None, 不標記)。"""
    role_id = await get_setting(THREADS_NOTIFY_ROLE_KEY)
    if role_id:
        return f"<@&{role_id}>", discord.AllowedMentions(roles=True)
    return None, discord.AllowedMentions.none()


async def get_startup_channel_id() -> str | None:
    """開機訊息頻道：settings 設定優先，未設定時退回 .env 的 STARTUP_CHANNEL_ID。"""
    cid = await get_setting(STARTUP_CHANNEL_KEY)
    if cid:
        return cid
    return STARTUP_CHANNEL_ID or None


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
    top_n: int = 5,
) -> list[dict]:
    """
    各關鍵字前 top_n 名；指定 author_id 時改為列出該人所有關鍵字。
    回傳 list[dict]，依 keyword → count DESC 排序。
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

    if author_id is not None:
        # 指定成員：直接列出該人所有關鍵字，不限名次
        sql = f"""
            SELECT author_tag, author_id, keyword, count, last_seen_at
            FROM keyword_counts
            WHERE {where}
            ORDER BY count DESC
            LIMIT 25
        """
    else:
        # 每個關鍵字各取前 top_n 名（使用視窗函式）
        params.append(top_n)
        sql = f"""
            SELECT author_tag, author_id, keyword, count, last_seen_at
            FROM (
                SELECT author_tag, author_id, keyword, count, last_seen_at,
                       ROW_NUMBER() OVER (PARTITION BY keyword ORDER BY count DESC) AS rn
                FROM keyword_counts
                WHERE {where}
            )
            WHERE rn <= ?
            ORDER BY keyword, count DESC
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
    使用 Playwright 抓取 Threads 公開個人頁面的最新貼文（前 10 則）。
    回傳 [{"post_id", "url", "pinned", "text", "images"}, ...] 或 None（失敗時）。
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
            # 將 username 傳入 JS，只抓屬於該用戶的貼文連結
            raw: list[dict] = await page.evaluate("""
                (username) => {
                    const userPattern = '/@' + username + '/post/';

                    // 置頂標籤可能的字串：英文 Pinned、zh-TW 實際用「釘選 / 已釘選」（非「置頂」）
                    const PIN_LABELS = ['pinned', '置頂', '釘選', '已釘選'];
                    // 大頭貼 alt 文字（各語系），用來排除頭像圖片
                    const AVATAR_HINTS = ['profile picture', '大頭貼', '頭像', '個人檔案'];

                    // 從貼文連結往上找出「單篇貼文容器」：往上爬，直到祖先包含過多 /post/ 連結為止
                    function postContainer(linkEl) {
                        let el = linkEl;
                        let best = linkEl;
                        for (let i = 0; i < 8; i++) {
                            if (!el.parentElement) break;
                            el = el.parentElement;
                            if (el.querySelectorAll('a[href*="/post/"]').length > 3) break;
                            best = el;
                        }
                        return best;
                    }

                    function isPinned(container) {
                        // 偵測置頂文字節點（短標籤精確比對，避免誤判內文）
                        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
                        let node;
                        while ((node = walker.nextNode())) {
                            const t = (node.textContent || '').trim().toLowerCase();
                            if (t && PIN_LABELS.includes(t)) return true;
                        }
                        // 偵測 aria-label / title（圖示型置頂標記）
                        for (const a of container.querySelectorAll('[aria-label], [title]')) {
                            const label = ((a.getAttribute('aria-label') || '') + ' ' +
                                           (a.getAttribute('title') || '')).toLowerCase();
                            if (PIN_LABELS.some(k => label.includes(k))) return true;
                        }
                        return false;
                    }

                    // 翻譯鈕標籤（各語系）：內文擷取時需排除，避免把按鈕「翻譯」抓進內文
                    const TRANSLATE_LABELS = ['翻譯', '翻译', 'translate', '查看翻譯', '顯示翻譯', 'see translation'];

                    // 容器內是否有「翻譯」按鈕（葉節點且文字剛好等於翻譯標籤）
                    function hasTranslateButton(container) {
                        for (const b of container.querySelectorAll('*')) {
                            if (b.children.length) continue;            // 只看葉節點
                            const bt = (b.textContent || '').trim().toLowerCase();
                            if (TRANSLATE_LABELS.includes(bt)) return true;
                        }
                        return false;
                    }

                    // 取貼文內文：Threads 內文多以 [dir="auto"] 呈現，取最長的葉節點區塊
                    function extractText(container) {
                        let leaf = '', any = '';
                        for (const el of container.querySelectorAll('[dir="auto"]')) {
                            const t = (el.innerText || '').trim();
                            if (t.length > any.length) any = t;
                            if (el.querySelector('[dir="auto"]')) continue;  // 跳過外層包裝，偏好葉節點
                            if (t.length > leaf.length) leaf = t;
                        }
                        let out = leaf || any;
                        // 內文尾端若被一併抓進「翻譯」按鈕字樣，且容器確有翻譯鈕，則移除
                        if (out) {
                            const low = out.toLowerCase();
                            for (const lbl of TRANSLATE_LABELS) {
                                if (low.endsWith(lbl) && hasTranslateButton(container)) {
                                    out = out.slice(0, out.length - lbl.length).trim();
                                    break;
                                }
                            }
                        }
                        return out;
                    }

                    // 取貼文圖片：略過頭像，依出現順序收集大圖（去重），最多 4 張（Discord 圖庫上限）；無圖時退而取影片封面
                    function extractImages(container) {
                        const urls = [];
                        const seenUrl = new Set();
                        for (const img of container.querySelectorAll('img')) {
                            const alt = (img.alt || '').toLowerCase();
                            if (AVATAR_HINTS.some(k => alt.includes(k))) continue;
                            const w = img.naturalWidth || img.width || 0;
                            if (w < 200) continue;  // 過濾頭像等小圖
                            const src = img.currentSrc || img.src;
                            if (!src || seenUrl.has(src)) continue;
                            seenUrl.add(src);
                            urls.push(src);
                            if (urls.length >= 4) break;
                        }
                        if (urls.length === 0) {
                            const v = container.querySelector('video[poster]');
                            const poster = v && v.getAttribute('poster');
                            if (poster) urls.push(poster);
                        }
                        return urls;
                    }

                    const seen = new Set();
                    const results = [];
                    for (const link of document.querySelectorAll('a[href*="/post/"]')) {
                        // 只保留屬於此用戶的貼文連結，排除回覆、引用等其他用戶的連結
                        if (!link.href.includes(userPattern)) continue;
                        const m = link.href.match(/\\/post\\/([^/?#]+)/);
                        if (!m) continue;
                        const pid = m[1];
                        if (seen.has(pid)) continue;
                        seen.add(pid);
                        const container = postContainer(link);
                        results.push({
                            pid,
                            pinned: isPinned(container),
                            text: extractText(container),
                            images: extractImages(container),
                        });
                        if (results.length >= 10) break;
                    }
                    return results;
                }
            """, username)

            if not raw:
                page_title = await page.title()
                print(f"[Threads] 找不到貼文連結，頁面標題：{page_title!r}")
                return None

            results: list[dict] = []
            for item in raw:
                pid: str = item["pid"]
                clean_url = f"https://www.threads.com/@{username}/post/{pid}"
                results.append({
                    "post_id": pid,
                    "url": clean_url,
                    "pinned": item["pinned"],
                    "text": (item.get("text") or "").strip(),
                    "images": item.get("images") or [],
                })

            pinned_ids = [r["post_id"] for r in results if r["pinned"]]
            print(f"[Threads] 共 {len(results)} 則，置頂：{pinned_ids}")
            return results

        except Exception as e:
            print(f"[Threads] 抓取 @{username} 失敗：{e}")
            return None
        finally:
            await browser.close()


# ---------- Threads Embed ----------

# 內文預覽長度上限（Discord embed description 最多 4096，這裡僅取預覽）
THREADS_PREVIEW_LIMIT = 1000


def build_threads_embeds(post: dict, *, title: str, footer: str) -> list[discord.Embed]:
    """依貼文資料組成 Discord embed 清單：標題連結 + 內文預覽 + 圖片。

    多圖時利用「多個 embed 共用同一 url」讓 Discord 併成單一圖庫（最多 4 張）。
    """
    embed = discord.Embed(title=title, url=post["url"], color=0x000000)

    text = (post.get("text") or "").strip()
    if text:
        if len(text) > THREADS_PREVIEW_LIMIT:
            text = text[:THREADS_PREVIEW_LIMIT].rstrip() + "…"
        embed.description = text

    embed.set_footer(text=footer)
    embed.timestamp = datetime.now(timezone.utc)

    images = post.get("images") or []
    if not images:
        return [embed]

    embed.set_image(url=images[0])
    embeds = [embed]
    # 其餘圖片：以相同 url 的額外 embed 串成圖庫（Discord 上限 4 張）
    for img in images[1:4]:
        extra = discord.Embed(url=post["url"], color=0x000000)
        extra.set_image(url=img)
        embeds.append(extra)
    return embeds


# ---------- Background Task ----------

# 每輪通知上限：抓取連續失敗造成盲窗，恢復後可能一次累積多則新貼文。
# 只發最新者，其餘已由 add_threads_seen_ids 標記已讀、不再補發，避免一次爆量。
THREADS_MAX_NOTIFY_PER_POLL = 1


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
        # 貼文順序由新到舊，故第一則為最新
        notify_posts = [p for p in new_posts if not p["pinned"]] or new_posts
        skipped_pinned = len(new_posts) - len(notify_posts)
        capped = notify_posts[:THREADS_MAX_NOTIFY_PER_POLL]
        dropped = len(notify_posts) - len(capped)

        # 先更新 seen（含置頂與被封頂略過者），再發通知（避免重複通知）
        await add_threads_seen_ids(THREADS_USERNAME, fetched_ids)

        channel = client.get_channel(int(THREADS_CHANNEL_ID))
        if not isinstance(channel, discord.TextChannel):
            print(f"[Threads] 找不到頻道 {THREADS_CHANNEL_ID}")
            return

        content, allowed = await get_threads_notify_content()
        for post in capped:
            embeds = build_threads_embeds(
                post,
                title=f"@{THREADS_USERNAME} 發布了新貼文",
                footer="Threads · 自動偵測",
            )
            await channel.send(content=content, embeds=embeds, allowed_mentions=allowed)

        print(f"[Threads] @{THREADS_USERNAME} 發送了 {len(capped)} 則新貼文通知（略過置頂 {skipped_pinned} 則、封頂略過 {dropped} 則）")

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
    if user:
        title = f"@{user.display_name} 的關鍵字統計"
    elif kw_filter:
        title = f"關鍵字「{kw_filter}」前 5 名"
    else:
        title = "各關鍵字前 5 名"

    embed = discord.Embed(title=title, color=0x5865F2)

    if user:
        # 指定成員：單欄列出所有關鍵字
        lines = [f"`{r['keyword']}`：**{r['count']}** 次" for r in rows]
        chunk = "\n".join(lines)
        if len(chunk) > 1020:
            chunk = chunk[:1020] + "\n…"
        embed.add_field(name="關鍵字次數", value=chunk, inline=False)
    else:
        # 依關鍵字分組，每個關鍵字一欄，列前 5 名
        from collections import defaultdict
        grouped: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            grouped[r["keyword"]].append(r)

        for kw, members in grouped.items():
            lines = []
            for i, r in enumerate(members, 1):
                lines.append(f"{i}. **{r['author_tag']}** — **{r['count']}** 次")
            value = "\n".join(lines)
            if len(value) > 1020:
                value = value[:1020] + "\n…"
            embed.add_field(name=f"🔑 {kw}", value=value, inline=True)

        # Discord embed 最多 25 個 field，超過時提示
        if len(grouped) > 25:
            embed.set_footer(text=f"僅顯示前 25 個關鍵字")

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
            "`/threads_role_set <role>` — 設定發布通知標記的身分組\n"
            "`/threads_role_clear` — 取消標記身分組\n"
            "`/threads_role_show` — 顯示目前標記的身分組\n"
            "每 10 分鐘自動偵測一次，有新貼文時發送通知（含內文預覽與大圖）\n"
            "監控對象與通知頻道設定於 `.env`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔔 開機訊息",
        value=(
            "`/startup_channel_set <channel>` — 設定開機訊息發送的頻道\n"
            "`/startup_channel_clear` — 取消開機訊息頻道設定\n"
            "`/startup_channel_show` — 顯示目前開機訊息頻道\n"
            "Bot 上線時會在此頻道發送含版本號的上線訊息"
        ),
        inline=False,
    )
    embed.set_footer(text=f"版本：{VERSION}")
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

    embeds = build_threads_embeds(
        post,
        title=f"@{THREADS_USERNAME} 的最新貼文",
        footer="Threads · 手動查詢",
    )
    await interaction.followup.send(embeds=embeds)


@tree.command(name="threads_role_set", description="設定發布通知時要標記的身分組")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(role="新貼文通知時要標記的身分組")
async def threads_role_set(interaction: discord.Interaction, role: discord.Role):
    await set_setting(THREADS_NOTIFY_ROLE_KEY, str(role.id))
    await interaction.response.send_message(
        f"已設定發布通知標記身分組：{role.mention}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="threads_role_clear", description="取消發布通知標記身分組")
@app_commands.default_permissions(administrator=True)
async def threads_role_clear(interaction: discord.Interaction):
    await set_setting(THREADS_NOTIFY_ROLE_KEY, None)
    await interaction.response.send_message("已取消發布通知的身分組標記。", ephemeral=True)


@tree.command(name="threads_role_show", description="顯示目前發布通知標記的身分組")
@app_commands.default_permissions(administrator=True)
async def threads_role_show(interaction: discord.Interaction):
    role_id = await get_setting(THREADS_NOTIFY_ROLE_KEY)
    if not role_id:
        await interaction.response.send_message("目前未設定發布通知標記身分組。", ephemeral=True)
        return
    await interaction.response.send_message(
        f"目前發布通知會標記：<@&{role_id}>",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ---------- Slash Commands — 開機訊息頻道 ----------

@tree.command(name="startup_channel_set", description="設定開機訊息發送的頻道")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="開機訊息要發送到的頻道")
async def startup_channel_set(interaction: discord.Interaction, channel: discord.TextChannel):
    await set_setting(STARTUP_CHANNEL_KEY, str(channel.id))
    await interaction.response.send_message(
        f"已設定開機訊息頻道：{channel.mention}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="startup_channel_clear", description="取消開機訊息頻道設定")
@app_commands.default_permissions(administrator=True)
async def startup_channel_clear(interaction: discord.Interaction):
    await set_setting(STARTUP_CHANNEL_KEY, None)
    await interaction.response.send_message(
        "已取消開機訊息頻道設定（將改用 `.env` 的 `STARTUP_CHANNEL_ID`，若未設定則不發送）。",
        ephemeral=True,
    )


@tree.command(name="startup_channel_show", description="顯示目前開機訊息頻道")
@app_commands.default_permissions(administrator=True)
async def startup_channel_show(interaction: discord.Interaction):
    cid = await get_startup_channel_id()
    if not cid:
        await interaction.response.send_message("目前未設定開機訊息頻道。", ephemeral=True)
        return
    source = "settings" if await get_setting(STARTUP_CHANNEL_KEY) else ".env"
    await interaction.response.send_message(
        f"目前開機訊息頻道：<#{cid}>（來源：{source}）",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


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


async def send_startup_message():
    """開機後發送上線訊息（含版本號）到設定的頻道；未設定或頻道無效時略過。"""
    cid = await get_startup_channel_id()
    if not cid:
        return
    try:
        channel = client.get_channel(int(cid))
    except (ValueError, TypeError):
        print(f"[開機訊息] 頻道 ID 無效：{cid!r}")
        return
    if not isinstance(channel, discord.TextChannel):
        print(f"[開機訊息] 找不到頻道 {cid}")
        return
    embed = discord.Embed(
        title="千島神社的小精靈已上線",
        description=f"版本：`{VERSION}`",
        color=0x57F287,
    )
    embed.timestamp = datetime.now(timezone.utc)
    try:
        await channel.send(embed=embed)
        print(f"[開機訊息] 已發送至頻道 {cid}")
    except discord.DiscordException as e:
        print(f"[開機訊息] 發送失敗：{e}")


@client.event
async def on_ready():
    await init_db()
    await tree.sync()
    if not check_threads_task.is_running():
        check_threads_task.start()
    print(f"Logged in as {client.user}  (ID: {client.user.id})  版本：{VERSION}")  # type: ignore[union-attr]
    print("------")
    await send_startup_message()


client.run(TOKEN)
