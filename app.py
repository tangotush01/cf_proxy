import asyncio
from contextlib import AsyncExitStack
from quart import Quart, jsonify, request
import os
import sys
 
conda_lib = os.path.join(sys.prefix, "lib")
os.environ["LD_LIBRARY_PATH"] = f"{conda_lib}:{os.environ.get('LD_LIBRARY_PATH', '')}"
from camoufox.async_api import AsyncCamoufox

app = Quart(__name__)

# Global runtime states
exit_stack = None
browser_instance = None

# Tracks open tabs: { "normalized_url": { "page": page_object, "sitekey": "current_sitekey" } }
active_pages = {}

# Locks to prevent race conditions on concurrent requests to the same tab: { "normalized_url": asyncio.Lock() }
page_locks = {}

# Exposes a configurable initialization function on the page's global window scope
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

    // Clean up any stale scripts to prevent collisions
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

@app.route('/get-token')
async def get_token():
    global browser_instance, active_pages, page_locks
    
    # Extract query parameters
    raw_url = request.args.get('url')
    sitekey = request.args.get('sitekey')
    
    if not raw_url or not sitekey:
        return jsonify({
            "status": "error", 
            "message": "Missing required query parameters: 'url' and 'sitekey'."
        }), 400

    # Normalize URL scheme for Playwright navigation
    url = raw_url if raw_url.startswith(('http://', 'https://')) else f"https://{raw_url}"

    # Initialize a lock for this specific URL to prevent overlapping click tasks
    if url not in page_locks:
        page_locks[url] = asyncio.Lock()

    # Process request inside the URL-specific lock
    async with page_locks[url]:
        try:
            page_data = active_pages.get(url)
            
            if not page_data:
                # If the URL is not open, open a new page/tab
                print(f"Opening a new tab for: {url}")
                page = await browser_instance.new_page()
                
                print(f"Navigating to: {url}...")
                await page.goto(url)
                
                print("Injecting sandbox library...")
                await page.evaluate(f"mw:{INJECTION_SCRIPT_TEMPLATE}")
                
                print(f"Initializing Turnstile with sitekey: {sitekey}")
                await page.evaluate(f"mw:window.initializeSandbox('{sitekey}');")
                
                # Keep track of the active page object and its active sitekey
                active_pages[url] = {"page": page, "sitekey": sitekey}
            else:
                page = page_data["page"]
                # If the page exists but the requested sitekey changed, re-initialize
                if page_data["sitekey"] != sitekey:
                    print(f"Sitekey changed from {page_data['sitekey']} to {sitekey}. Re-initializing...")
                    await page.evaluate(f"mw:window.initializeSandbox('{sitekey}');")
                    page_data["sitekey"] = sitekey
                else:
                    print(f"Reusing existing open tab for: {url}")

            # Reset the widget state
            await page.evaluate("mw:window.resetWidget();")
            
            timeout = 30
            poll_interval = 1
            elapsed = 0.0
            token = None

            while elapsed < timeout:
                # Calculate coordinates of the container in the current tab
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

            if token:
                return jsonify({"status": "success", "token": token})
            else:
                return jsonify({"status": "timeout", "message": f"The challenge on {url} was not solved within the limit."}), 408

        except Exception as e:
            # If a tab breaks or crashes, remove it from the tracking cache
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
