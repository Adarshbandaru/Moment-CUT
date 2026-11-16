"""Talking to the AI service. You bring your own key, and choose the service and model in the dashboard
(Settings, AI) or in .env. What is saved from the dashboard is kept in ai.json in the data folder and wins.

    PEAKCUT_AI_PROVIDER   gemini | openai | xai | groq | openrouter | anthropic | ollama | custom
    PEAKCUT_AI_MODELS     one or more model ids, comma separated, tried in this order
    PEAKCUT_AI_KEY        your key (or PEAKCUT_AI_KEY_FILE, a file holding it)
    PEAKCUT_AI_BASE_URL   only for "custom" (any service that speaks the OpenAI chat format), or to override

Every service is asked for the same thing: JSON that follows a small schema. Gemini takes the schema directly.
The others are told the schema in the prompt and asked for a JSON object, which works with any chat model."""
import json
import os
import re
import threading

import httpx

from . import config

# name -> (label shown on the pages, address, how it is spoken to, where to get a key, an example model id)
PROVIDERS = {
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini", "https://aistudio.google.com/apikey", "gemini-2.5-flash"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "https://platform.openai.com/api-keys", "gpt-4o-mini"),
    "xai": ("Grok", "https://api.x.ai/v1", "openai", "https://console.x.ai", "grok-4"),
    "meta": ("Meta (Llama)", "https://api.meta.ai/v1", "openai", "https://auth.meta.com", "llama-3.3-70b-instruct"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1", "openai", "https://platform.deepseek.com", "deepseek-chat"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys", "llama-3.3-70b-versatile"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "https://openrouter.ai/keys", "google/gemini-2.5-flash"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "anthropic", "https://console.anthropic.com", ""),
    "ollama": ("Ollama (on this machine)", "http://127.0.0.1:11434/v1", "openai", "https://ollama.com", "llama3.1"),
    "custom": ("Custom (OpenAI-compatible)", "", "openai", "", ""),
}
ALIASES = {"google": "gemini", "grok": "xai", "claude": "anthropic", "local": "ollama", "llama": "meta", "meta-llama": "meta"}
NO_KEY = {"ollama", "custom"}       # a model running on your own machine needs no key

# ---------------------------------------------------------------- the settings in use
_lock = threading.Lock()
_saved = {"at": None, "data": {}}
_key_index = 0


def _file():
    return config.DATA_DIR / "ai.json"


def _from_dashboard() -> dict:
    """What was saved from the dashboard, read again only when the file changes."""
    f = _file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _saved["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _saved.update(at=at, data=data if isinstance(data, dict) else {})
    return _saved["data"]


def detect_key_provider(k: str) -> str:
    """Infer the AI provider from key format if unambiguous."""
    k = (k or "").strip()
    if k.startswith("AIzaSy"):
        return "gemini"
    if k.startswith("gsk_"):
        return "groq"
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("sk-or-"):
        return "openrouter"
    if k.startswith("xai-"):
        return "xai"
    return ""


def providers_config() -> dict:
    """All configured providers, their models, keys and addresses."""
    d = _from_dashboard()
    p_dict = dict(d.get("providers") or {})
    curr_p = str(d.get("provider") or config.AI_PROVIDER or "").strip().lower()
    curr_p = ALIASES.get(curr_p, curr_p)
    if curr_p and curr_p in PROVIDERS:
        cfg = p_dict.setdefault(curr_p, {})
        cfg.setdefault("models", list(d.get("models") or config.AI_MODELS))
        cfg.setdefault("keys", keys())
        cfg.setdefault("base_url", str(d.get("base_url") or ""))
    return p_dict


def fallback_chain() -> list:
    """The ordered list of provider IDs to use, failing over when quota is exceeded."""
    d = _from_dashboard()
    curr_p = str(d.get("provider") or config.AI_PROVIDER or "").strip().lower()
    curr_p = ALIASES.get(curr_p, curr_p)
    all_p = providers_config()
    saved_chain = [ALIASES.get(str(x).lower(), str(x).lower()) for x in (d.get("fallback_chain") or [])
                   if ALIASES.get(str(x).lower(), str(x).lower()) in PROVIDERS]
    chain = []
    if curr_p and curr_p in PROVIDERS:
        chain.append(curr_p)
    for p in saved_chain:
        if p not in chain and p in PROVIDERS and (all_p.get(p, {}).get("keys") or p in NO_KEY):
            chain.append(p)
    for p in all_p:
        if p not in chain and p in PROVIDERS and (all_p[p].get("keys") or p in NO_KEY):
            chain.append(p)
    return chain or ([curr_p] if curr_p else ["gemini"])


def keys() -> list:
    """All configured keys for the active AI provider, preserving pool order."""
    d = _from_dashboard()
    p = str(d.get("provider") or config.AI_PROVIDER or "").strip().lower()
    p = ALIASES.get(p, p)
    if d.get("provider"):
        ks = d.get("keys")
        if isinstance(ks, list) and ks:
            clean = [str(x).strip() for x in ks if str(x).strip()]
            if clean:
                return clean
        raw = d.get("key")
    else:
        raw = config.ai_key()
    return [k.strip() for k in re.split(r"[\r\n,;]+", str(raw or "")) if k.strip()]


def active_key() -> str:
    """The current active key in the fallback pool."""
    global _key_index
    ks = keys()
    if not ks:
        return ""
    with _lock:
        return ks[_key_index % len(ks)]


def shift_key(reason: str = "") -> str:
    """Auto-shift to the next API key in the pool on rate limit / quota exhaustion."""
    global _key_index
    ks = keys()
    if not ks:
        return ""
    with _lock:
        prev = ks[_key_index % len(ks)]
        _key_index = (_key_index + 1) % len(ks)
        nxt = ks[_key_index % len(ks)]
    try:
        from . import alerts
        prev_hint = f"...{prev[-4:]}" if len(prev) >= 4 else "key"
        nxt_hint = f"...{nxt[-4:]}" if len(nxt) >= 4 else "key"
        alerts.notify(f"AI key tier/quota limit reached on {prev_hint}. Switched to {nxt_hint} ({reason or 'auto-shift'})", kind="ai")
    except Exception:
        pass
    return nxt


def current() -> dict:
    """Provider, models, address and key: what the dashboard saved, or else what .env says."""
    d = _from_dashboard()
    p = str(d.get("provider") or config.AI_PROVIDER or "").strip().lower()
    ks = keys()
    curr_k = active_key()
    return {"provider": ALIASES.get(p, p),
            "models": list(d["models"]) if d.get("provider") else list(config.AI_MODELS),
            "base_url": (str(d.get("base_url") or "") if d.get("provider") else config.AI_BASE_URL).rstrip("/"),
            "key": curr_k,
            "keys": ks}


def provider() -> str:
    return current()["provider"]


def models() -> list:
    return current()["models"]


def key() -> str:
    return active_key()


def label() -> str:
    return PROVIDERS.get(provider(), ("",))[0]


def base_url() -> str:
    c = current()
    return c["base_url"] or PROVIDERS.get(c["provider"], ("", ""))[1]


def ready() -> bool:
    c = current()
    return bool(c["provider"] in PROVIDERS and c["models"] and base_url() and (c["key"] or c["provider"] in NO_KEY))


def public() -> dict:
    """What the settings page may see."""
    c = current()
    ks = c.get("keys") or ([c["key"]] if c.get("key") else [])
    if len(ks) > 1:
        hint = ", ".join(f"...{k[-4:]}" for k in ks if len(k) >= 4)
    else:
        hint = c["key"][-4:] if len(c.get("key", "")) >= 12 else ""
    all_p = providers_config()
    chain = fallback_chain()
    configured = []
    for prov in chain:
        info = all_p.get(prov, {})
        p_keys = info.get("keys", [])
        if p_keys or prov in NO_KEY:
            configured.append({
                "id": prov,
                "label": PROVIDERS[prov][0],
                "keys_count": len(p_keys),
                "model": (info.get("models") or [PROVIDERS[prov][4]])[0]
            })
    chain_names = [f"{PROVIDERS[p][0]} ({len(all_p.get(p, {}).get('keys', []))} keys)" for p in chain if p in PROVIDERS and all_p.get(p, {}).get("keys")]
    summary = " ➔ ".join(chain_names) if chain_names else ""
    return {"provider": c["provider"], "models": c["models"], "base_url": c["base_url"], "has_key": bool(ks),
            "keys_count": len(ks),
            "key_hint": hint,
            "ready": ready(), "label": label(),
            "saved_here": bool(_from_dashboard().get("provider")),
            "providers": [{"id": k, "label": v[0], "address": v[1], "needs_key": k not in NO_KEY, "key_url": v[3], "example": v[4]}
                          for k, v in PROVIDERS.items()],
            "configured_providers": configured,
            "fallback_chain": chain,
            "fallback_summary": summary,
            "all_providers": {k: {"keys_count": len(v.get("keys", [])), "has_key": bool(v.get("keys")), "models": v.get("models", [])} for k, v in all_p.items()}}


def save(provider_id: str, model_ids, address: str = "", new_key=None, fallback_chain=None) -> dict:
    """Save the choice made in the dashboard. Preserves keys for other configured providers
    so that multi-provider auto-failover works seamlessly."""
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if isinstance(model_ids, str):
        model_ids = model_ids.split(",")
    ms = [str(m).strip()[:120] for m in (model_ids or []) if str(m).strip()][:5]
    if not ms:
        raise ValueError("Enter the model to use.")
    if any(not re.fullmatch(r"[\w.\-:/@]+", m) for m in ms):
        raise ValueError("A model id has letters, digits and . - : / only.")
    address = str(address or "").strip().rstrip("/")[:300]
    if address and not re.fullmatch(r"https?://[^\s]+", address):
        raise ValueError("The address has to start with http:// or https://")
    if p == "custom" and not address:
        raise ValueError("A custom service needs its address, for example http://127.0.0.1:1234/v1")

    old_data = _from_dashboard()
    all_providers = dict(old_data.get("providers") or {})
    old = current()

    if new_key is None:
        saved_keys = all_providers.get(p, {}).get("keys") or (old.get("keys") if old["provider"] == p else [])
    else:
        raw_key = str(new_key or "").strip()[:8000]
        raw_list = [k.strip() for k in re.split(r"[\r\n,;]+", raw_key) if k.strip()]
        saved_keys = []
        for k in raw_list:
            detected = detect_key_provider(k)
            if detected and detected != p:
                target_p = all_providers.setdefault(detected, {"models": [PROVIDERS[detected][4]], "keys": [], "base_url": ""})
                if k not in target_p["keys"]:
                    target_p["keys"].append(k)
            else:
                saved_keys.append(k)

    if not saved_keys and p not in NO_KEY:
        existing_k = all_providers.get(p, {}).get("keys")
        if existing_k:
            saved_keys = existing_k
        else:
            raise ValueError("Paste the key for this service.")

    primary_k = saved_keys[0] if saved_keys else ""
    all_providers[p] = {"models": ms, "keys": saved_keys, "base_url": address}

    chain = [p]
    if fallback_chain and isinstance(fallback_chain, list):
        for f in fallback_chain:
            fp = ALIASES.get(str(f).lower(), str(f).lower())
            if fp in all_providers and fp not in chain and (all_providers[fp].get("keys") or fp in NO_KEY):
                chain.append(fp)
    for prov in all_providers:
        if prov not in chain and (all_providers[prov].get("keys") or prov in NO_KEY):
            chain.append(prov)

    with _lock:
        f = _file()
        tmp = f.with_suffix(".tmp")
        payload = json.dumps({
            "provider": p,
            "models": ms,
            "base_url": address,
            "key": primary_k,
            "keys": saved_keys,
            "providers": all_providers,
            "fallback_chain": chain
        })
        for attempt in range(20):
            try:
                tmp.write_text(payload, encoding="utf-8")
                try:
                    os.chmod(tmp, 0o600)        # the key is only for whoever runs the server
                except OSError:
                    pass
                tmp.replace(f)
                break
            except OSError:
                if attempt == 19:
                    f.write_text(payload, encoding="utf-8")
                else:
                    import time
                    time.sleep(0.05)
        _saved["at"] = None
    return public()


def list_models(provider_id=None, address=None, with_key=None) -> list:
    """The model ids a service offers, so the settings page can show a list to pick from."""
    c = current()
    p = ALIASES.get(str(provider_id or c["provider"]).lower(), str(provider_id or c["provider"]).lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if with_key:
        parsed_keys = [k.strip() for k in re.split(r"[\r\n,;]+", str(with_key)) if k.strip()]
        k = parsed_keys[0] if parsed_keys else ""
    else:
        k = (c["key"] if c["provider"] == p else "")
    base = str(address or "").strip().rstrip("/") or (c["base_url"] if c["provider"] == p else "") or PROVIDERS[p][1]
    if not base:
        raise ValueError("Enter the service's address first.")
    how = PROVIDERS[p][2]
    if how == "gemini":
        r = httpx.get(base, params={"pageSize": 200}, headers={"x-goog-api-key": k}, timeout=20)
    elif how == "anthropic":
        r = httpx.get(f"{base}/models", params={"limit": 200}, headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=20)
    else:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {k}"} if k else {}, timeout=20)
    if r.status_code != 200:
        raise AIError(f"HTTP {r.status_code} {r.text[:200].replace(k or chr(0), '<key>')}")
    data = r.json()
    if how == "gemini":
        found = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    else:
        found = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({m for m in found if m})[:400]


def test() -> dict:
    """One tiny request with the settings in use, to prove the key, the address and the model all work."""
    ms = models()
    if not ready():
        raise ValueError("Save a service, a model and a key first.")
    out, sent, wrote = call(ms[0], 'Answer with a JSON array that holds the single string "ok".', {"type": "ARRAY", "items": {"type": "STRING"}})
    ks = keys()
    curr_k = active_key()
    hint = f"...{curr_k[-4:]}" if len(curr_k) >= 4 else "active"
    chain = fallback_chain()
    all_p = providers_config()
    chain_names = [f"{PROVIDERS[p][0]} ({len(all_p.get(p, {}).get('keys', []))}k)" for p in chain if p in PROVIDERS and all_p.get(p, {}).get("keys")]
    chain_info = f" | Failover: {' ➔ '.join(chain_names)}" if len(chain_names) > 1 else ""
    pool_info = f" ({len(ks)} keys in {label()} pool{chain_info})" if (len(ks) > 1 or len(chain_names) > 1) else ""
    return {"ok": True, "model": ms[0], "tokens": sent + wrote, "answer": str(out)[:80], "active_key": hint, "keys_count": len(ks), "pool_info": pool_info, "fallback_chain": chain}


class AIError(RuntimeError):
    pass


problem = {"text": "", "at": 0.0}       # why the AI last failed, for the dashboard; cleared when it answers again


def explain(e) -> str:
    """What went wrong with an AI call, in words the owner can act on."""
    text, low = str(e), str(e).lower()
    name = (models() or ["?"])[0]
    if "404" in text or "not found" in low or "does not exist" in low or "unknown model" in low or "not_found" in low:
        return f"{label()} doesn't know the model \"{name}\". Open Settings, AI, press Find models and pick one from the list."
    if "401" in text or "403" in text or "api key" in low or "api_key" in low or "unauthorized" in low or "permission" in low:
        return f"{label()} refused the key. Paste a working key under Settings, AI."
    if "429" in text or "quota" in low or "rate limit" in low or "exhausted" in low or "resource_exhausted" in low:
        return f"{label()} says the key has hit its limit or has no credit left. Check the account with {label()}, or add fallback keys in Settings, AI."
    if "connect" in low or "timed out" in low or "timeout" in low or "getaddrinfo" in low or "refused" in low:
        return f"PeakCut couldn't reach {label()}. Check the internet connection" + (" and that Ollama is running." if provider() == "ollama" else ".")
    return ""


def note_problem(e):
    import time
    why = explain(e)
    if why:
        problem.update(text=why, at=time.time())


def clear_problem():
    problem.update(text="", at=0.0)


def _fail(r, model, k=None):
    hide = k or key() or "\0"
    text = r.text[:300].replace(hide, "<key>")
    raise AIError(f"HTTP {r.status_code} {text}")


# ---------------------------------------------------------------- the schema, said in words for chat models
def _json_schema(s):
    """Gemini's schema spelling (ARRAY, OBJECT...) as ordinary JSON Schema."""
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_json_schema(x) for x in s]
    return s


def _wrapped(prompt, schema):
    return (prompt + "\n\nAnswer with JSON only, no other text and no code fences: one object with a single key "
            "\"items\", whose value follows this JSON Schema:\n" + json.dumps(_json_schema(schema)))


def _parse(text):
    """The JSON in an answer, even if the model wrapped it in a code fence or a sentence."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        out = json.loads(text)
    except ValueError:
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if not m:
            raise AIError("The AI did not answer with JSON.")
        out = json.loads(m.group(0))
    if isinstance(out, dict) and "items" in out:
        out = out["items"]
    return out


# ---------------------------------------------------------------- one call
def call(model: str, prompt: str, schema: dict):
    """Ask one model. Returns (the parsed JSON answer, tokens sent, tokens written back).
    Automatically cycles across multiple keys of the active provider, and if that provider
    hits rate/quota limit (429), automatically fails over to the next provider in the
    fallback chain (e.g. Gemini (3 keys) -> Groq -> Meta)."""
    chain = fallback_chain()
    all_p = providers_config()
    last_err = None

    for prov_idx, prov_id in enumerate(chain):
        p_cfg = all_p.get(prov_id, {})
        p_keys = p_cfg.get("keys") if prov_id in all_p else (keys() if prov_id == provider() else [])
        if not p_keys and prov_id not in NO_KEY:
            continue
        p_keys = p_keys or [""]
        p_models = p_cfg.get("models") or ([model] if prov_id == provider() else [PROVIDERS[prov_id][4]])
        p_model = p_models[0]
        p_base = (p_cfg.get("base_url") if prov_id in all_p else base_url()) or PROVIDERS[prov_id][1]
        p_how = PROVIDERS[prov_id][2]
        fn = {"gemini": _gemini, "openai": _openai, "anthropic": _anthropic}[p_how]

        prov_exhausted = False
        for k_idx, k in enumerate(p_keys):
            try:
                ans, sent, wrote = fn(p_model, prompt, schema, k=k, base=p_base)
                clear_problem()
                return ans, sent, wrote
            except AIError as e:
                last_err = e
                err_text = str(e).lower()
                is_quota = any(w in err_text for w in [
                    "429", "resource_exhausted", "quota", "rate limit", "ratelimit",
                    "tier", "limit exceeded", "exceeded your current quota", "overloaded",
                    "credit", "insufficient_quota", "usage limit"
                ])
                if is_quota:
                    if k_idx < len(p_keys) - 1:
                        shift_key(reason=f"Quota on {PROVIDERS[prov_id][0]} key {k_idx + 1}")
                        import time
                        time.sleep(0.4)
                        continue
                    else:
                        prov_exhausted = True
                        break
                else:
                    break
            except Exception as e:
                last_err = e
                break

        if prov_exhausted and prov_idx < len(chain) - 1:
            next_prov = chain[prov_idx + 1]
            try:
                from . import alerts
                alerts.notify(f"⚠️ {PROVIDERS[prov_id][0]} quota exceeded across all keys. Auto-failing over to {PROVIDERS[next_prov][0]} ({PROVIDERS[next_prov][4]})...", kind="ai")
            except Exception:
                pass
            continue

    if last_err:
        note_problem(last_err)
        raise last_err


def _gemini(model, prompt, schema, k=None, base=None):
    use_k = k or active_key()
    b = (base or base_url()).rstrip("/")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema}}
    r = httpx.post(f"{b}/{model}:generateContent", json=body, headers={"x-goog-api-key": use_k}, timeout=120)
    if r.status_code != 200:
        _fail(r, model, k=use_k)
    data = r.json()
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    u = data.get("usageMetadata", {})
    sent = int(u.get("promptTokenCount") or 0)
    return json.loads(text), sent, max(0, int(u.get("totalTokenCount") or 0) - sent)   # the answer, and any thinking billed with it


def _openai(model, prompt, schema, k=None, base=None):
    use_k = k or active_key()
    b = (base or base_url()).rstrip("/")
    headers = {"Authorization": f"Bearer {use_k}"} if use_k else {}
    body = {"model": model, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    # Most services accept these two extras; a few models refuse one of them, so they are dropped on a refusal.
    extras = [{"temperature": 0.4, "response_format": {"type": "json_object"}}, {"response_format": {"type": "json_object"}}, {}]
    r = None
    for extra in extras:
        r = httpx.post(f"{b}/chat/completions", json={**body, **extra}, headers=headers, timeout=180)
        if r.status_code not in (400, 422):
            break
    if r.status_code != 200:
        _fail(r, model, k=use_k)
    data = r.json()
    u = data.get("usage") or {}
    return _parse(data["choices"][0]["message"].get("content")), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _anthropic(model, prompt, schema, k=None, base=None):
    use_k = k or active_key()
    b = (base or base_url()).rstrip("/")
    body = {"model": model, "max_tokens": 8192, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    r = httpx.post(f"{b}/messages", json=body, timeout=180,
                   headers={"x-api-key": use_k, "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        _fail(r, model, k=use_k)
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return _parse(text), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
