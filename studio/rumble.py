"""Automated Rumble uploading using Playwright browser automation."""
import json
import logging
import os
import re
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


def _storage_file(account_id: str = None) -> Path:
    if account_id:
        f = RUMBLE_DIR / f"storage_state_{account_id}.json"
        if f.is_file():
            return f
    return STORAGE_FILE


def _session_file(account_id: str = None) -> Path:
    if account_id:
        f = RUMBLE_DIR / f"session_{account_id}.json"
        if f.is_file():
            return f
    return SESSION_INFO


def is_ready(account_id: str = None) -> bool:
    """True if Playwright is installed and a Rumble session has been saved."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    sf = _storage_file(account_id)
    return sf.is_file() and sf.stat().st_size > 10


def session_info(account_id: str = None) -> dict:
    if not is_ready(account_id):
        return {"connected": False}
    try:
        sf = _session_file(account_id)
        if sf.is_file():
            return json.loads(sf.read_text(encoding="utf-8"))
        if SESSION_INFO.is_file():
            return json.loads(SESSION_INFO.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"connected": False}


def parse_cookies(cookie_input: str) -> list[dict]:
    """Universal cookie parser for Rumble: supports JSON (Cookie-Editor, EditThisCookie, Playwright),
    Netscape / curl cookies.txt format, HTTP Cookie header strings, and raw u_s tokens."""
    raw = (cookie_input or "").strip()
    if not raw:
        return []
    now = time.time()
    cookies_dict = {}

    # 1. Try JSON (array of cookie objects, dict, or storage_state object)
    if (raw.startswith("[") and raw.endswith("]")) or (raw.startswith("{") and raw.endswith("}")):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                if "cookies" in data and isinstance(data["cookies"], list):
                    data = data["cookies"]
                else:
                    for k, v in data.items():
                        if isinstance(v, (str, int, float, bool)):
                            cookies_dict[str(k).strip()] = str(v).strip()
                        elif isinstance(v, dict) and "value" in v:
                            cookies_dict[str(k).strip()] = str(v["value"]).strip()
                    data = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                        cookies_dict[str(item["name"]).strip()] = str(item["value"]).strip()
        except Exception:
            pass

    # 2. Try Netscape format or Header format if cookies_dict is empty
    if not cookies_dict:
        lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.strip().startswith("#")]
        for line in lines:
            parts = re.split(r"\t+|\s{2,}", line)
            if len(parts) >= 7 and "." in parts[0]:
                cookies_dict[parts[5].strip()] = parts[6].strip()
            elif ";" in line or "=" in line:
                for pair in line.split(";"):
                    pair = pair.strip()
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k:
                            cookies_dict[k] = v

    # 3. If still empty, check if it's a raw token string (e.g. starting with s%3A or plain token)
    if not cookies_dict and len(raw) > 5 and " " not in raw:
        cookies_dict["u_s"] = raw

    out = []
    expires = int(now + 86400 * 365)
    for name, val in cookies_dict.items():
        val = val.strip().strip("\"'")
        if not val:
            continue
        out.append({
            "name": name,
            "value": val,
            "domain": ".rumble.com",
            "path": "/",
            "expires": expires,
            "httpOnly": name in ("u_s", "u_a", "PHPSESSID"),
            "secure": True,
            "sameSite": "Lax",
        })
    return out


def save_cookie_session(cookie_val: str, username: str = "RajShamani_Shorts",
                        creator_id: str = None, account_id: str = None) -> dict:
    """Directly saves a Rumble session from pasted cookies (JSON, header, or u_s token)."""
    parsed = parse_cookies(cookie_val)
    if not parsed or not any(c["name"] == "u_s" for c in parsed):
        raise ValueError("Could not find a valid Rumble session cookie (u_s). Please copy and paste your Rumble cookies (e.g. using Cookie-Editor JSON export or header string).")

    clean_user = (username or "").strip() or "RajShamani_Shorts"
    acc_id = _register_account(clean_user, creator_id=creator_id, account_id=account_id)

    data = {
        "connected": True,
        "connected_at": time.time(),
        "username": clean_user,
        "account_id": acc_id,
    }
    state = {
        "cookies": parsed,
        "origins": [],
    }

    # Save to account-specific file
    target_storage = RUMBLE_DIR / f"storage_state_{acc_id}.json"
    target_session = RUMBLE_DIR / f"session_{acc_id}.json"
    target_storage.write_text(json.dumps(state, indent=1), encoding="utf-8")
    target_session.write_text(json.dumps(data, indent=1), encoding="utf-8")

    # Also update the primary default files
    STORAGE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    SESSION_INFO.write_text(json.dumps(data, indent=1), encoding="utf-8")

    return {"ok": True, "username": clean_user, "account_id": acc_id}


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


def _register_account(username: str, creator_id: str = None, account_id: str = None) -> str:
    from . import accounts, auth
    try:
        uid = ctx.uid_or_none() or auth.local_user().id
    except Exception:
        uid = "u_local"
    with ctx.as_user(uid):
        accs = accounts.list_accounts()
        clean_user = (username or "").strip() or "RajShamani_Shorts"
        existing = None
        if account_id:
            existing = next((a for a in accs if a.get("id") == account_id), None)
        if not existing:
            existing = next((a for a in accs if a.get("platform") == "rumble" and a.get("name", "").lower() == clean_user.lower()), None)

        if existing:
            acc_id = existing["id"]
        elif account_id:
            acc_id = account_id
        else:
            slug = re.sub(r"[^a-zA-Z0-9_]", "", clean_user)[:20] or os.urandom(4).hex()
            acc_id = f"acc_rumble_{slug}"

        acc = {
            "id": acc_id,
            "platform": "rumble",
            "name": clean_user,
            "added": time.time(),
            "detail": "Rumble session (Playwright)",
        }
        accounts._put(acc)

        # Creator binding: associate with targeted or matching creator
        from . import creators
        target_c = None
        if creator_id:
            target_c = creators.get(creator_id)
        if not target_c:
            for c in creators.all_creators():
                c_name = (c.get("name") or "").lower()
                if c_name and (c_name in clean_user.lower() or clean_user.lower() in c_name or "raj" in c_name):
                    target_c = c
                    break

        if target_c:
            c_accs = list(target_c.get("accounts", []))
            if acc_id not in c_accs:
                c_accs.append(acc_id)
            target_c["accounts"] = c_accs
            target_c["autopost"] = True
            creators.update(target_c["id"], target_c)
        ctx.bump()
        return acc_id


def upload_video(video_path: Path, title: str, description: str, tags: list = None,
                 visibility: str = "public", account_id: str = None) -> tuple:
    """Uploads a video to Rumble using the authenticated Playwright context.
    Returns (remote_id, video_url)."""
    if not is_ready(account_id):
        raise RuntimeError(f"Rumble account '{account_id or 'default'}' is not connected. Open Settings to connect Rumble.")
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    from playwright.sync_api import sync_playwright

    st_file = _storage_file(account_id)
    with _lock:
        with sync_playwright() as p:
            browser = _launch_browser(p, headless=True)
            context = browser.new_context(
                no_viewport=True,
                storage_state=str(st_file),
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

            # 2. Fill Title, Description, Tags (clean comma-separated format)
            title_input = page.wait_for_selector("input#title, input[name='title']", timeout=15000)
            title_input.fill(title[:100])

            desc_input = page.query_selector("textarea#description, textarea[name='description']")
            if desc_input:
                desc_input.fill(description[:2000])

            if tags:
                seen = set()
                clean_tags = []
                for t in tags:
                    clean = re.sub(r'[^a-zA-Z0-9_ ]', '', str(t).replace("#", "")).strip()
                    if clean and clean.lower() not in seen:
                        seen.add(clean.lower())
                        clean_tags.append(clean)
                tags_str = ", ".join(clean_tags[:12])
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
