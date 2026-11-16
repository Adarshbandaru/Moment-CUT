"""Automated Rumble uploading using Playwright browser automation."""
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from . import config, ctx

log = logging.getLogger("rumble")
_lock = threading.Lock()

RUMBLE_DIR = config.DATA_DIR / "rumble"
RUMBLE_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_FILE = RUMBLE_DIR / "storage_state.json"
SESSION_INFO = RUMBLE_DIR / "session.json"


def is_ready() -> bool:
    """True if Playwright is installed and a Rumble session has been saved."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return STORAGE_FILE.is_file() and SESSION_INFO.is_file()


def session_info() -> dict:
    if not is_ready():
        return {"connected": False}
    try:
        return json.loads(SESSION_INFO.read_text(encoding="utf-8"))
    except Exception:
        return {"connected": False}


def save_cookie_session(cookie_val: str, username: str = "Rumble Creator") -> dict:
    """Directly saves a Rumble session from the u_s cookie."""
    cval = cookie_val.strip()
    if "u_s=" in cval:
        for part in cval.split(";"):
            part = part.strip()
            if part.startswith("u_s="):
                cval = part[4:].strip()
                break

    data = {
        "connected": True,
        "connected_at": time.time(),
        "username": username or "Rumble Creator",
    }
    SESSION_INFO.write_text(json.dumps(data, indent=1), encoding="utf-8")
    state = {
        "cookies": [
            {
                "name": "u_s",
                "value": cval,
                "domain": ".rumble.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    STORAGE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    _register_account(data["username"])
    return {"ok": True, "username": data["username"]}


def _launch_browser(p, headless: bool = False):
    """Launches Edge or Chrome or bundled Chromium."""
    for ch in ("msedge", "chrome", None):
        try:
            return p.chromium.launch(
                channel=ch,
                headless=headless,
                args=["--start-maximized"] if not headless else [],
            )
        except Exception:
            continue
    return p.chromium.launch(headless=headless)


def launch_login():
    """Opens a visible browser window for the user to log in to Rumble."""
    bat = Path(__file__).resolve().parent.parent / "connect_rumble.bat"
    if sys.platform == "win32" and hasattr(os, "startfile") and bat.is_file():
        try:
            os.startfile(str(bat))
            return {"ok": True, "message": "Browser opened. Sign in to your Rumble account."}
        except Exception as e:
            log.warning(f"os.startfile failed: {e}")

    t = threading.Thread(target=ctx.bind(_login_worker), daemon=True)
    t.start()
    return {"ok": True, "message": "Browser opened. Sign in to your Rumble account."}


def _login_worker():
    from playwright.sync_api import sync_playwright

    print("[PeakCut] Starting login worker...", flush=True)
    with _lock:
        try:
            with sync_playwright() as p:
                print("[PeakCut] Opening browser window (Edge / Chrome)...", flush=True)
                browser = _launch_browser(p, headless=False)
                context = browser.new_context(
                    no_viewport=True,
                    storage_state=str(STORAGE_FILE) if STORAGE_FILE.is_file() else None,
                )
                page = context.new_page()
                print("[PeakCut] Navigating to https://rumble.com/login.php ...", flush=True)
                try:
                    page.goto("https://rumble.com/login.php", timeout=60000)
                    page.bring_to_front()
                except Exception as e:
                    print(f"[PeakCut] Navigation notice: {e}", flush=True)

                print("[PeakCut] Browser is ready! Please sign in to your Rumble account.", flush=True)
                print("[PeakCut] (PeakCut will automatically detect when login completes)", flush=True)

                # Wait for user to complete login (up to 5 minutes)
                t0 = time.time()
                logged_in = False
                username = ""
                while time.time() - t0 < 300:
                    time.sleep(2)
                    try:
                        if not browser.is_connected() or page.is_closed():
                            break
                        url = page.url.lower()
                        if "rumble.com" in url and "login" not in url and "auth.rumble.com" not in url:
                            logged_in = True
                            try:
                                user_el = page.query_selector(".header-user-title, [data-user-name], .user-menu-name")
                                if user_el:
                                    username = user_el.inner_text().strip()
                            except Exception:
                                pass
                            break
                    except Exception:
                        break

                if logged_in:
                    context.storage_state(path=str(STORAGE_FILE))
                    data = {
                        "connected": True,
                        "connected_at": time.time(),
                        "username": username or "Rumble Creator",
                    }
                    SESSION_INFO.write_text(json.dumps(data, indent=1), encoding="utf-8")
                    _register_account(data["username"])
                    print(f"\n[PeakCut] SUCCESS! Logged in as: {data['username']}", flush=True)
                else:
                    print("\n[PeakCut] Login was not completed or window was closed.", flush=True)

                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            log.exception(f"Rumble login worker error: {e}")
            print(f"[PeakCut] Error opening browser: {e}", flush=True)


def _register_account(username: str):
    from . import accounts, auth
    try:
        uid = ctx.uid_or_none() or auth.local_user().id
    except Exception:
        uid = "u_local"
    with ctx.as_user(uid):
        accs = accounts.list_accounts()
        existing = next((a for a in accs if a.get("platform") == "rumble"), None)
        acc_id = existing["id"] if existing else ("acc_rumble_" + os.urandom(4).hex())
        acc = {
            "id": acc_id,
            "platform": "rumble",
            "name": username or "Rumble Creator",
            "added": time.time(),
            "detail": "Browser automation",
        }
        accounts._put(acc)
        from . import creators
        for c in creators.all_creators():
            c_accs = list(c.get("accounts", []))
            if acc_id not in c_accs:
                c_accs.append(acc_id)
            c["accounts"] = c_accs
            c["autopost"] = True
            creators.update(c["id"], c)
        ctx.bump()


def upload_video(video_path: Path, title: str, description: str, tags: list = None,
                 visibility: str = "public") -> tuple:
    """Uploads a video to Rumble using the authenticated Playwright context.
    Returns (remote_id, video_url)."""
    if not is_ready():
        raise RuntimeError("Rumble account is not connected. Open Settings to connect Rumble.")
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    from playwright.sync_api import sync_playwright

    with _lock:
        with sync_playwright() as p:
            browser = _launch_browser(p, headless=True)
            context = browser.new_context(
                no_viewport=True,
                storage_state=str(STORAGE_FILE),
            )
            page = context.new_page()
            page.goto("https://rumble.com/upload.php", timeout=60000)
            page.wait_for_load_state("domcontentloaded")

            if "login" in page.url.lower():
                browser.close()
                raise RuntimeError("Rumble session expired. Reconnect Rumble in Settings.")

            # 1. Set video file
            file_input = page.wait_for_selector("input#Filedata, input[name='Filedata']", timeout=30000)
            file_input.set_input_files(str(video_path.resolve()))
            time.sleep(2)

            # 2. Fill Title, Description, Tags
            title_input = page.wait_for_selector("input#title, input[name='title']", timeout=15000)
            title_input.fill(title[:100])

            desc_input = page.query_selector("textarea#description, textarea[name='description']")
            if desc_input:
                desc_input.fill(description[:2000])

            if tags:
                tags_str = ", ".join(t.replace("#", "") for t in tags[:10])
                tags_input = page.query_selector("input#tags, input[name='tags']")
                if tags_input:
                    tags_input.fill(tags_str)

            # 3. Select Category: Entertainment (data-value 15)
            try:
                page.click("input[name='primary-category']")
                time.sleep(0.5)
                cat_opt = page.wait_for_selector(".select-option[data-value='15'], .select-option:has-text('Entertainment')", timeout=5000)
                if cat_opt:
                    cat_opt.click()
            except Exception as e:
                log.warning(f"Could not select category: {e}")

            # 4. Wait for video upload to finish 100%
            for _ in range(300):
                time.sleep(1)
                pct = page.evaluate("() => document.querySelector('.num_percent')?.innerText || document.querySelector('.top_percent')?.innerText || ''")
                if "100" in pct:
                    break

            time.sleep(2)

            # 5. Click Upload (Form 1 submit button)
            page.click("#submitForm")

            # 6. Wait for Form 2, then accept Terms & License
            try:
                page.wait_for_selector("#form2, #submitForm2", timeout=20000)
                time.sleep(1)
                page.click("label[for='crights']")
                page.click("label[for='cterms']")
                page.evaluate("() => { if (document.getElementById('crights')) document.getElementById('crights').checked = true; if (document.getElementById('cterms')) document.getElementById('cterms').checked = true; }")
                time.sleep(1)
                page.click("#submitForm2")
            except Exception as e:
                log.warning(f"Error handling form2: {e}")

            # 7. Wait for published video link in Form 3
            direct_url = ""
            for _ in range(45):
                time.sleep(1)
                val = page.evaluate("() => document.getElementById('direct')?.value || ''")
                if val and "rumble.com" in val:
                    direct_url = val
                    break
                # Also check if redirected to /shorts/... or /v...
                if "/shorts/" in page.url or ("/v" in page.url and "upload" not in page.url):
                    direct_url = page.url
                    break

            browser.close()

            if not direct_url:
                raise RuntimeError("Rumble upload did not return a published video URL.")

            remote_id = direct_url.rstrip("/").split("/")[-1].replace(".html", "")
            return remote_id, direct_url


if __name__ == "__main__":
    _login_worker()
