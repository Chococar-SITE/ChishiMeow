"""
執行此腳本一次，用真實瀏覽器登入 Threads，
登入後關閉視窗，Cookie 會自動儲存到 threads_cookies.json。
之後 bot 就會自動載入 Cookie，不需要再登入。
"""
import json
import asyncio
from playwright.async_api import async_playwright

COOKIES_PATH = "./threads_cookies.json"


async def main():
    async with async_playwright() as p:
        # headless=False：開啟真實視窗讓你操作
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        await page.goto("https://www.threads.net/login", wait_until="networkidle")

        print("=== 請在開啟的瀏覽器視窗中登入 Threads ===")
        print("登入完成後，這裡會自動偵測並儲存 Cookie。")
        print("（等待最多 120 秒）")

        # 等待頁面跳轉離開 /login（代表登入成功）
        try:
            await page.wait_for_url(
                lambda url: "/login" not in url,
                timeout=120_000,
            )
        except Exception:
            print("逾時，請重新執行此腳本。")
            await browser.close()
            return

        # 再等一下讓 Cookie 穩定
        await page.wait_for_timeout(2_000)

        cookies = await context.cookies()
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)

        print(f"Cookie 已儲存至 {COOKIES_PATH}（共 {len(cookies)} 筆）")
        print("現在可以重新啟動 bot 了。")
        await browser.close()


asyncio.run(main())
