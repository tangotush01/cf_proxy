import asyncio
import time
import os
import sys
import psutil
from contextlib import AsyncExitStack
from quart import Quart, jsonify, request

conda_lib = os.path.join(sys.prefix, "lib")
os.environ["LD_LIBRARY_PATH"] = f"{conda_lib}:{os.environ.get('LD_LIBRARY_PATH', '')}"
from camoufox.async_api import AsyncCamoufox

app = Quart(__name__)

# Configurable memory threshold (in percentage)
MEMORY_THRESHOLD_PERCENT = 80.0

# Global runtime states
exit_stack = None
browser_instance = None

# Tracks open tabs: { "normalized_url": { "page": page_object, "sitekey": "current_sitekey", "last_used": float } }
active_pages = {}

# Locks to prevent race conditions on concurrent requests to the same tab
page_locks = {}

INJECTION_SCRIPT_TEMPLATE = """
window.initializeSandbox = function(sitekey) {
    document.body.innerHTML = `
        <div style="display: flex; justify-content: center; align-items: center; height: 100vh; flex-direction: column; font-family: Arial, sans-serif; background-color: #f3f4f6;">
            <div style="padding: 30px; background: white; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); text-align: center;">
                <h3 style="color: #333; margin-top: 0;">Local Turnstile Sandbox</h3>
                <div id="turnstile-container"></div>
            </div>
        </div>
    `;

    window.latestToken = null;
    window.widgetId = null;

    window.onloadTurnstileCallback = function() {
        window.widgetId = turnstile.render('#turnstile-container', {
            sitekey: sitekey,
            callback: function(token) {
                window.latestToken = token;
                console.log("Token generated:", token);
            }
        });
    };

    window.resetWidget = function() {
        window.latestToken = null;
        if (window.widgetId !== null) {
            turnstile.reset(window.widgetId);
        }
    };

    const oldScript = document.getElementById('turnstile-script');
    if (oldScript) oldScript.remove();

    const script = document.createElement('script');
    script.id = 'turnstile-script';
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onloadTurnstileCallback';
    script.async = true;
    script.defer = true;
    document.head.appendChild(script);
};
"""

async def cleanup_idle_pages(current_url=None):
    """
    Checks memory usage and closes the least recently used (LRU) inactive tabs
    if system memory exceeds MEMORY_THRESHOLD_PERCENT.
    """
    mem_usage = psutil.virtual_memory().percent
    if mem_usage < MEMORY_THRESHOLD_PERCENT:
        return

    print(f"Memory threshold exceeded ({mem_usage}% >= {MEMORY_THRESHOLD_PERCENT}%). Initiating cleanup...")

    # Sort tabs by least recently used
    sorted_pages = sorted(
        active_pages.items(),
        key=lambda item: item[1].get("last_used", 0)
    )

    for url, page_data in sorted_pages:
        # Skip the page currently handling the active request
        if url == current_url:
            continue

        lock = page_locks.get(url)
        
        # Only close tabs that are not actively locked by another request
        if lock and not lock.locked():
            async with lock:
                try:
                    print(f"Closing idle tab for URL: {url}")
                    await page_data["page"].close()
                except Exception as e:
                    print(f"Error closing page {url}: {e}")
                finally:
                    active_pages.pop(url, None)
                    page_locks.pop(url, None)

            # Re-check memory; stop cleanup if usage has dropped below threshold
            if psutil.virtual_memory().percent < MEMORY_THRESHOLD_PERCENT:
                print("Memory dropped below threshold. Stopping cleanup.")
                break


@app.route('/get-token')
async def get_token():
    global browser_instance, active_pages, page_locks
    
    raw_url = request.args.get('url')
    sitekey = request.args.get('sitekey')
    
    if not raw_url or not sitekey:
        return jsonify({
            "status": "error", 
            "message": "Missing required query parameters: 'url' and 'sitekey'."
        }), 400

    url = raw_url if raw_url.startswith(('http://', 'https://')) else f"https://{raw_url}"

    # Check and free up memory before allocating new browser tab resources
    await cleanup_idle_pages(current_url=url)

    if url not in page_locks:
        page_locks[url] = asyncio.Lock()

    async with page_locks[url]:
        try:
            page_data = active_pages.get(url)
            
            if not page_data:
                print(f"Opening a new tab for: {url}")
                page = await browser_instance.new_page()
                
                print(f"Navigating to: {url}...")
                await page.goto(url)
                
                print("Injecting sandbox library...")
                await page.evaluate(f"mw:{INJECTION_SCRIPT_TEMPLATE}")
                
                print(f"Initializing Turnstile with sitekey: {sitekey}")
                await page.evaluate(f"mw:window.initializeSandbox('{sitekey}');")
                
                active_pages[url] = {
                    "page": page, 
                    "sitekey": sitekey, 
                    "last_used": time.time()
                }
            else:
                page = page_data["page"]
                # Update timestamp on activity
                page_data["last_used"] = time.time()
                
                if page_data["sitekey"] != sitekey:
                    print(f"Sitekey changed from {page_data['sitekey']} to {sitekey}. Re-initializing...")
                    await page.evaluate(f"mw:window.initializeSandbox('{sitekey}');")
                    page_data["sitekey"] = sitekey
                else:
                    print(f"Reusing existing open tab for: {url}")

            await page.evaluate("mw:window.resetWidget();")
            
            timeout = 30
            poll_interval = 1
            elapsed = 0.0
            token = None

            while elapsed < timeout:
                box = await page.evaluate("""
                    () => {
                        const el = document.querySelector('#turnstile-container');
                        if (!el) return null;
                        const rect = el.getBoundingClientRect();
                        return { x: rect.left, y: rect.top, width: rect.width, height: rect.height };
                    }
                """)

                if box:
                    click_x = int(box['x'] + (box['width'] / 2))
                    click_y = int(box['y'] + (box['height'] / 2))
                    await page.mouse.click(click_x, click_y)
                
                token = await page.evaluate("mw:window.latestToken;")
                if token:
                    break
                    
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            # Update timestamp after request completion
            if url in active_pages:
                active_pages[url]["last_used"] = time.time()

            if token:
                return jsonify({"status": "success", "token": token})
            else:
                return jsonify({"status": "timeout", "message": f"The challenge on {url} was not solved within the limit."}), 408

        except Exception as e:
            if url in active_pages:
                del active_pages[url]
            return jsonify({"status": "error", "message": str(e)}), 500

@app.before_serving
async def initialize_test_environment():
    global exit_stack, browser_instance
    
    print("Launching Camoufox...")
    exit_stack = AsyncExitStack()
    
    browser_instance = await exit_stack.enter_async_context(
        AsyncCamoufox(
            headless=True,
            main_world_eval=True,
            humanize=True,
            window=(1036, 703)
        )
    )
    print("Browser running. Awaiting dynamic requests...")

@app.after_serving
async def cleanup_test_environment():
    global exit_stack
    if exit_stack:
        print("Closing browser context and active tabs...")
        await exit_stack.aclose()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
