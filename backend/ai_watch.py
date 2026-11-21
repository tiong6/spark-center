"""AI 工具與模型：這台機器上呼叫模型的工具（VS Code 擴充、Codex、Claude Code CLI）落後官方幾版、新版加了哪些模型，
以及各家最近上架的模型。解析都在這裡（純函式）；抓取由呼叫端傳進 get(url, body=None, headers=None) -> bytes。

為什麼要有這個：模型（Fable 5.2、GPT-6.2 Astra）在供應商的伺服器上，不會「安裝」到這台機器；選單裡有沒有
新模型取決於工具版本。2026-11-21 實例：CLI 2.1.326 已有 Fable 5.2（2.1.321 加入），VS Code 擴充還在 2.1.318，
所以 VS Code 的選單看不到；Codex 0.171.0 才加 GPT-6.2，附在 ChatGPT 擴充裡的是 0.171.0-alpha.13。

資料來源都是官方或公開清單，拿不到就回 ok=False＋原因，不猜：
- VS Code Marketplace extensionquery（公開，無金鑰）
- Claude Code CHANGELOG.md（anthropics/claude-code，GitHub raw）
- Codex releases（openai/codex 的 releases.atom：約 350 KB；REST API 一次 4 MB、慢連線會 IncompleteRead，且每小時只給 60 次）
- OpenRouter /api/v1/models（第三方彙整、無金鑰；created 是 OpenRouter 上架時間，不一定等於官方發布日）
"""
import html
import json
import os
import re
import xml.etree.ElementTree as ET

MARKETPLACE_URL = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
CLAUDE_CHANGELOG_URL = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
CODEX_RELEASES_URL = "https://github.com/openai/codex/releases.atom"
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
MODEL_DAYS = 90
# 布告欄上有自己篩選鈕的供應商（OpenRouter id 前綴）；其餘歸「其他」。「~openai」這種是 OpenRouter 的別名路由、openrouter 是它自己的，都不算模型發布
MAJOR_PROVIDERS = ("anthropic", "openai", "google", "deepseek", "qwen", "moonshotai", "z-ai", "minimax", "x-ai",
                   "meta", "meta-llama", "mistralai", "nvidia", "xiaomi", "tencent", "baidu")

# 版本說明裡「提到模型」的寫法：Claude Fable 5.2、Opus 5.5、GPT-6.2 Astra、Gemini 4 Neon、claude-fable-5-2、gpt-6.2-astra
MODEL_RE = re.compile(
    r"\b(?:Claude\s+)?(?:Fable|Mythos|Opus|Sonnet|Haiku)\s+\d+(?:\.\d+)?\b"
    r"|\bGPT-\d+(?:\.\d+)?(?:\s+[A-Z][a-z]+(?:\s+Pro)?(?:\s+and\s+[A-Z][a-z]+(?:\s+Pro)?)?)?"
    r"|\bGemini\s+\d+(?:\.\d+)?(?:\s+[A-Z][a-z]+)?"
    r"|\bclaude-(?:fable|mythos|opus|sonnet|haiku)-\d+(?:-\d+)?\b"
    r"|\bgpt-\d+(?:\.\d+)?(?:-[a-z]+)*\b")


# 「新增／改預設」才算模型消息；「修了在 Opus 4.6 上的 bug」這類順帶提到的不算（2.1.318→2.1.330 十版裡這種有十幾行，會把 Fable 5.2 淹掉）
_NEWS_TAIL_RE = re.compile(r"default (?:\w+ )?model|now the default|model catalog|model picker", re.I)


def is_model_news(line, mentions):
    for m in mentions:
        if re.match(rf"(?:added|add|introduced?|launched?)\s+(?:support\s+for\s+)?(?:the\s+)?(?:claude\s+)?{re.escape(m)}", line, re.I):
            return True
    return bool(_NEWS_TAIL_RE.search(line))


def model_mentions(text):
    """回不重複的提及字串（照出現順序）。"""
    seen, out = set(), []
    for m in MODEL_RE.finditer(text or ""):
        s = re.sub(r"\s+", " ", m.group(0)).strip()
        if s.lower() not in seen:
            seen.add(s.lower()); out.append(s)
    return out


def vkey(v):
    """版本比較鍵：0.171.0-alpha.13 < 0.171.0-alpha.13.1 < 0.171.0 < 0.172.0-alpha.1。認不得的段落當 0。"""
    v = (v or "").strip().lstrip("v")
    main, _, pre = v.partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in main.split("."))
    if not pre:
        return (nums, 1, ())
    parts = tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"[.]", pre))
    return (nums, 0, parts)


def newer(a, b):
    """a 比 b 新？任一邊空就 False（不知道就不說有新版）。"""
    return bool(a and b) and vkey(a) > vkey(b)


# ---------- VS Code 擴充 ----------

def vscode_installed(path):
    """讀 ~/.vscode/extensions/extensions.json（VS Code 自己的清單，比列資料夾準：舊版資料夾會殘留）。
    回 [{id, version, pre, platform}]；讀不到回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    out = []
    for e in data if isinstance(data, list) else []:
        try:
            md = e.get("metadata") or {}
            out.append({"id": e["identifier"]["id"].lower(), "version": e["version"],
                        "pre": bool(md.get("preRelease") or md.get("isPreReleaseVersion")),
                        "platform": md.get("targetPlatform") if md.get("targetPlatform") not in (None, "undefined") else None})
        except (KeyError, TypeError):
            continue
    out.sort(key=lambda x: x["id"])
    return out


def marketplace_body(ids):
    """flags 951＝每個平台只回最新一版（含屬性：PreRelease 標記在屬性裡）。最新一版是預覽版、而本機裝的是正式版時
    這裡看不到最新正式版，要用 marketplace_latest_url 逐一補查（VS Code 自己也這樣做）。"""
    return {"filters": [{"criteria": [{"filterType": 7, "value": i} for i in ids], "pageSize": max(len(ids), 1)}], "flags": 951}


def marketplace_latest_url(eid):
    """單一擴充：每個平台的最新預覽版＋最新正式版都在（約 50 KB）。全版本清單（flags 403）一個擴充就 8 MB、20 個要 30 秒。"""
    pub, name = eid.split(".", 1)
    return f"https://marketplace.visualstudio.com/_apis/public/gallery/vscode/{pub}/{name}/latest"


def wrap_single(ext):
    """單一擴充端點回的是擴充物件本身；包成 extensionquery 的形狀，給 marketplace_latest 用。"""
    return {"results": [{"extensions": [ext]}]}


def marketplace_latest(resp, installed, platform):
    """resp：extensionquery 的 JSON。對每個已安裝擴充挑「這台平台能裝的最新版」：
    targetPlatform 是本機平台或沒有平台（通用）；已安裝的是正式版就不挑預覽版。
    回 ({id: {version, date, name}}, [Marketplace 有這個擴充、但回應裡沒有可用版本的 id]）——後者要用 marketplace_latest_url 補查。"""
    want = {e["id"]: e for e in installed}
    out, missing = {}, []
    for res in (resp or {}).get("results", []):
        for ext in res.get("extensions", []):
            eid = f"{ext['publisher']['publisherName']}.{ext['extensionName']}".lower()
            inst = want.get(eid)
            if not inst:
                continue
            for v in ext.get("versions", []):
                props = {p["key"]: p["value"] for p in v.get("properties", [])}
                is_pre = props.get("Microsoft.VisualStudio.Code.PreRelease") == "true"
                if v.get("targetPlatform") not in (None, platform) or (is_pre and not inst["pre"]):
                    continue
                cur = out.get(eid)
                if cur is None or vkey(v["version"]) > vkey(cur["version"]):
                    out[eid] = {"version": v["version"], "date": v.get("lastUpdated"), "name": ext.get("displayName") or eid}
            if eid not in out:
                missing.append(eid)
    return out, missing


# ---------- 版本說明 ----------

def parse_claude_changelog(md):
    """CHANGELOG.md → [(version, [bullet…])]，新的在前。"""
    out, cur = [], None
    for line in (md or "").splitlines():
        m = re.match(r"^##\s+(\d+\.\d+\.\d+\S*)\s*$", line)
        if m:
            cur = (m.group(1), []); out.append(cur); continue
        if cur and line.startswith("- "):
            cur[1].append(line[2:].strip())
    return out


_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_codex_atom(text):
    """releases.atom → [(version, [bullet…], prerelease, date)]。連結是 …/tag/rust-v0.171.0；feed 沒有 prerelease 欄位，
    版本帶「-」（0.173.0-alpha.4）就當預覽版。內文是 HTML 的 <li>；「#62106 Add …」是 PR 清單，和功能說明重複，不收。"""
    out = []
    for e in ET.fromstring(text).findall(f"{_ATOM}entry"):
        link = e.find(f"{_ATOM}link")
        m = re.search(r"/tag/(?:rust-v)?(\d+\.\d+\.\d+\S*)$", link.get("href", "") if link is not None else "")
        if not m:
            continue
        body = (e.findtext(f"{_ATOM}content") or "")
        bullets = []
        for li in re.findall(r"<li>(.*?)</li>", body, re.S):
            s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", li))).strip()
            if s and not re.match(r"#\d", s):
                bullets.append(s)
        ver = m.group(1)
        out.append((ver, bullets, "-" in ver, e.findtext(f"{_ATOM}updated")))
    return out


def notes_between(entries, installed, latest):
    """installed < 版本 ≤ latest 的條目，只留「新增／改預設模型」的那幾行（is_model_news）。回 {"versions": 幾版, "models": [{version, mentions, line}]}。
    installed 拿不到就不算（不知道從哪裡開始比）。"""
    if not installed or not latest:
        return {"versions": 0, "models": []}
    picked = [e for e in entries if vkey(installed) < vkey(e[0]) <= vkey(latest)]
    models = []
    for e in picked:
        for line in e[1]:
            ms = model_mentions(line)
            if ms and is_model_news(line, ms):
                models.append({"version": e[0], "mentions": ms, "line": line[:300]})
    return {"versions": len(picked), "models": models}


def latest_stable(entries):
    vs = [e[0] for e in entries if not (len(e) > 2 and e[2]) and "-" not in e[0]]
    return max(vs, key=vkey) if vs else None


# ---------- 模型上架 ----------

def _per_mtok(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f * 1e6, 4) if f >= 0 else None   # OpenRouter 用 -1 表示「依路由而定」


def recent_models(data, now, days=MODEL_DAYS):
    """OpenRouter /models → 近 days 天上架的模型，新的在前。:batch／:free 這類同一模型的變體只留一筆。
    open_weights：OpenRouter 有填 Hugging Face 權重 id（可以下載到本機跑；能不能在這台跑還要看大小）。"""
    seen, out = set(), []
    for m in (data or {}).get("data", []):
        mid = (m.get("id") or "").split(":")[0]
        prov = mid.split("/")[0]
        created = m.get("created") or 0
        if not prov or prov.startswith("~") or prov == "openrouter" or mid in seen or now - created > days * 86400:
            continue
        seen.add(mid)
        pr = m.get("pricing") or {}
        out.append({"id": mid, "name": re.sub(r"\s*\((?:batch|free)\)$", "", m.get("name") or mid), "provider": prov,
                    "major": prov in MAJOR_PROVIDERS, "created": created, "context": m.get("context_length"),
                    "price_in": _per_mtok(pr.get("prompt")), "price_out": _per_mtok(pr.get("completion")),
                    "hf": m.get("hugging_face_id") or None, "desc": (m.get("description") or "")[:400]})
    out.sort(key=lambda x: (-x["created"], x["id"]))
    return out


def claude_cli_version(text):
    m = re.search(r"(\d+\.\d+\.\d+)", text or "")
    return m.group(1) if m else None


def codex_bundled(ext_dir):
    """Codex CLI 附在 VS Code ChatGPT 擴充裡（bin/linux-aarch64/codex）；~/.local/bin/codex 是轉接腳本。
    回最新那個擴充資料夾裡的 codex 路徑（和轉接腳本同一套挑法：sort -V 最後一個），沒有就 None。"""
    try:
        dirs = [d for d in os.listdir(ext_dir) if d.startswith("openai.chatgpt-")]
    except OSError:
        return None
    for d in sorted(dirs, key=lambda d: vkey(d.removeprefix("openai.chatgpt-").rsplit("-linux", 1)[0]), reverse=True):
        p = os.path.join(ext_dir, d, "bin", "linux-aarch64", "codex")
        if os.access(p, os.X_OK):
            return p
    return None
