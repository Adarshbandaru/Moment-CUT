"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import secrets
import threading
import time
from pathlib import Path

import json

from . import accounts, alerts, config, creators, ctx, jobs, live

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")
_upload_lock = threading.Lock()
_wake_event = threading.Event()


def _wake_uploader():
    _wake_event.set()


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        for i in range(10):
            try:
                tmp.replace(_FILE)
                break
            except OSError:
                if i == 9:
                    try:
                        _FILE.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
                        try:
                            tmp.unlink(missing_ok=True)
                        except OSError:
                            pass
                    except OSError:
                        pass
                time.sleep(0.05)


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error")}
        jobs.save(job)


def _next_slot(account_id, gap_min, cap):
    """Earliest time that respects the gap between posts and the daily cap for this account."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        lt = time.localtime(t + 86400)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day
    return t


def enqueue(job, clip, account_ids, now=False) -> list:
    c = creators.get(job.get("creator")) or creators.DEFAULTS
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                   and p["status"] in OPEN + ("posted",) for p in _posts):
                continue  # never post the same clip to the same account twice
            due = time.time() if now else _next_slot(aid, c.get("post_gap_min", 1), c.get("daily_cap", 50))
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job.get("creator"),
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None}
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    if now and made:
        _wake_uploader()
    return made


def _process_now(uid, post_ids):
    time.sleep(1)
    try:
        with ctx.as_user(uid):
            due = [p for p in list(_posts) if p["id"] in post_ids and p["status"] == "scheduled"]
            for p in due:
                with _lock:
                    if p["status"] != "scheduled":
                        continue
                    p["status"] = "posting"
                _post(p)
    except Exception:
        pass


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    from . import autopilot
    ap_on = autopilot.is_autopilot()

    cid = job.get("creator")
    c = creators.get(cid) if cid else None

    all_accs = accounts.list_accounts()

    if ap_on:
        to_post = []
        if c and c.get("accounts"):
            to_post = list(c["accounts"])
        else:
            to_post = [a["id"] for a in all_accs if a.get("platform") in ("youtube", "rumble")]

        from . import rumble
        if rumble.is_ready():
            for a in all_accs:
                if a.get("platform") == "rumble" and a["id"] not in to_post:
                    to_post.append(a["id"])

        if to_post:
            made = enqueue(job, clip, to_post, now=True)
            if made:
                p_names = ", ".join(set(p.get("platform", "app").capitalize() for p in made))
                alerts.notify(f"Auto-pilot queued clip for {p_names}: {clip.get('title') or clip.get('hook')}", kind="post")
                if c:
                    creators.log(c["id"], f"Auto-pilot queued for {p_names}: {clip.get('title') or clip.get('hook')}")
            return

    if not c:
        # If job was created without an explicit creator, check if any creator has autopost on
        active = [cr for cr in creators.all_creators() if cr.get("autopost") and cr.get("accounts")]
        if active:
            c = active[0]
            job["creator"] = c["id"]
            jobs.save(job)
    if not c:
        return
    if not c.get("autopost") or not c.get("accounts"):
        return
    if clip.get("score", 0) < c.get("post_min_score", 0):
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator):
    base_tags = list(clip.get("hashtags", []))
    if "IShowSpeedVerse_IN" not in base_tags:
        base_tags.append("IShowSpeedVerse_IN")
    tags = " ".join("#" + h for h in base_tags)
    footer = creator.get("post_footer", "") or "Content by @IShowSpeedVerse_IN | #IShowSpeedVerse_IN"
    return "\n\n".join(x for x in (clip.get("description", ""), tags, footer) if x)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")

        # Anti-repetition: Ensure this clip was not already posted to this account
        already = [
            x for x in _posts
            if x.get("id") != p["id"]
            and x.get("account") == p["account"]
            and x.get("status") == "posted"
            and x.get("remote_id")
        ]
        if any(x.get("job") == p["job"] and x.get("clip") == p["clip"] for x in already):
            p.update(status="cancelled", error="Duplicate: this clip was already posted.")
            _save()
            _mirror(p)
            return

        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)
        if acc["platform"] == "youtube":
            yt_cap = f"{cap}\n\nChannel: @IShowSpeedVerse_IN\n#Shorts #IShowSpeedVerse_IN"
            yt_tags = list(set(clip.get("hashtags", []) + ["IShowSpeedVerse_IN", "Shorts"]))
            rid, url = accounts.youtube_upload(acc, path, title, yt_cap, yt_tags, creator["privacy"])
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
        elif acc["platform"] == "rumble":
            from . import rumble
            rid, url = rumble.upload_video(path, title, cap, clip.get("hashtags", []), creator.get("privacy", "public"))
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        p["tries"] += 1
        err_msg = str(e)
        p["error"] = err_msg[-300:]
        is_limit = any(x in err_msg.lower() for x in [
            "uploadlimitexceeded", "exceeded the number of videos", "upload limit",
            "quota exceeded", "video uploads per day", "quota metric"
        ])
        if is_limit:
            p.update(status="failed", error="YouTube daily upload limit reached (10 videos/day max for new accounts). Try again tomorrow.")
            creators.log(p["creator"], f"YouTube daily upload limit reached on {p['account_name']}: {title}")
            alerts.notify(f"YouTube daily limit reached on {p['account_name']}. Remaining uploads will resume tomorrow.", kind="post")
        elif p["tries"] < 3 and clip and acc:
            backoff = 30 * (2 ** (p["tries"] - 1))  # 30s for try 1, 60s for try 2
            p.update(status="scheduled", due_at=time.time() + backoff)
            creators.log(p["creator"], f"Upload attempt {p['tries']}/3 failed on {p['account_name']}: retrying in {backoff}s...")
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']} after 3 attempts: {p['error']}")
            alerts.notify(f"Post failed on {p['account_name']} ({p['title']}) after 3 attempts: {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def _run():
    """Sequential upload worker: uploads videos one by one until all queued posts finish."""
    while True:
        # Check every 5s or wake up instantly when a clip is ready
        _wake_event.wait(timeout=5)
        _wake_event.clear()

        while True:
            from . import autopilot
            ap_on = autopilot.is_autopilot()

            next_p = None
            with _lock:
                candidates = []
                now_t = time.time()
                for p in _posts:
                    if p.get("status") != "scheduled" or not p.get("user"):
                        continue
                    # Retries must respect backoff delay even when Auto-Pilot is ON
                    if p.get("tries", 0) > 0 and (p.get("due_at") or 0) > now_t:
                        continue
                    if ap_on or (p.get("due_at") or 0) <= now_t:
                        candidates.append(p)

                if not candidates:
                    break

                # Sort by due_at or created time so oldest / first created uploads first
                candidates.sort(key=lambda p: (p.get("due_at") or 0, p.get("created") or 0))
                p = candidates[0]

                if not ap_on:
                    try:
                        with ctx.as_user(p["user"]):
                            if accounts.settings().get("paused"):
                                break
                    except Exception:
                        pass

                p["status"] = "posting"
                _save()
                next_p = p

            if not next_p:
                break

            with ctx.as_user(next_p["user"]):
                _mirror(next_p)

            # Upload ONE video exclusively with _upload_lock so uploads NEVER overlap
            with _upload_lock:
                try:
                    with ctx.as_user(next_p["user"]):
                        _post(next_p)
                except Exception as e:
                    with _lock:
                        next_p["status"] = "failed"
                        next_p["error"] = str(e)[:300]
                        _save()
                    with ctx.as_user(next_p["user"]):
                        _mirror(next_p)

            # 2s breather between uploads so YouTube / platforms process each video cleanly
            time.sleep(2)


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)
    _wake_uploader()


def retry_all_failed() -> int:
    """Reset tries and re-schedule all failed or cancelled uploads for the signed-in user."""
    retried_posts = []
    with _lock:
        for p in _mine():
            if p.get("status") in ("failed", "cancelled"):
                p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
                retried_posts.append(p)
        if retried_posts:
            _save()
    for p in retried_posts:
        _mirror(p)
    if retried_posts:
        _wake_uploader()
    return len(retried_posts)


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p["due_at"]))[:15]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
