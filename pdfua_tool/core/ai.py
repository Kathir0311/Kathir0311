"""Optional local-AI helpers backed by Ollama.

Everything here degrades gracefully: if the server is unreachable the tool
keeps working and only the AI buttons report themselves as unavailable.
No request leaves the machine the Ollama server runs on.

Models are resolved against what is actually installed: if the configured
model is missing (or left blank = auto), an installed model with the right
capability is used instead, so a fresh server works without editing settings.
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

DEFAULT_URL = "http://localhost:11434"

# Preference order when picking automatically (first installed match wins).
VISION_PREFERENCE = ["qwen3-vl", "qwen2.5vl", "qwen2.5-vl", "gemma3", "llama3.2-vision", "minicpm-v",
                     "llava-llama3", "llava-phi3", "llava", "granite3.2-vision", "mistral-small3",
                     "llama4", "moondream", "bakllava"]
TEXT_PREFERENCE = ["qwen3", "qwen2.5", "llama3.2", "llama3.1", "gemma3", "granite4", "granite3.3",
                   "mistral", "phi4", "phi3", "gemma2", "llama3"]
VISION_NAME = re.compile(r"(vision|llava|bakllava|minicpm-v|moondream|pixtral|qwen[\d.]*-?vl|"
                         r"gemma3:(?!1b)|gemma3$|llama4|mistral-small3|internvl|cogvlm)", re.I)
NOT_CHAT = re.compile(r"(embed|bge|minilm|nomic|rerank|whisper)", re.I)


# One job per thing the tool asks a model to do. A small model is fine for the
# short questions; the picture jobs want the best vision model the machine can
# actually load. "" on a task = use the general text/vision model.
TASKS: dict[str, tuple[str, str]] = {
    "alt_text": ("vision", "Figure alt text"),
    "math": ("vision", "Formulas: read aloud, LaTeX, decorative?"),
    "table_rows": ("vision", "Table: is this row inside the table?"),
    "table_merge_vision": ("vision", "Table continues on the next page (looking at the pages)"),
    "zone": ("vision", "Deep check of a drawn list / table zone"),
    "zone_role": ("vision", "Drawn zone: what is it? (key I / Auto)"),
    "roles": ("text", "Tag role review"),
    "headings": ("text", "Heading levels"),
    "links": ("text", "Link descriptions"),
    "fields": ("text", "Form field labels"),
    "table_summary": ("text", "Table summary"),
    "table_headers": ("text", "Is row 1 a header?"),
    "table_merge": ("text", "Table continues on the next page"),
    "merge": ("text", "Merge two tags across pages"),
    "xref": ("text", "Doubtful cross-reference"),
    "bibliography": ("text", "Reference list: where each entry starts"),
    "zone_list": ("text", "Drawn list with no numbers: where each item starts"),
    "reading_order": ("vision", "Page reading order check"),
    "sample_roles": ("text", "Doubtful tags, with the client's sample files"),
    "brain": ("text", "AI brain: doubtful tags, with the nearest finished examples (every upload)"),
    "study": ("text", "AI brain: study a sample file, write the client's tagging rules"),
}


# Which model family suits which job when nothing is set. The picture jobs that
# run once per figure want the best reader; the ones that run hundreds of times
# (is this row inside the table?) want the quick one. Models that only do OCR
# (deepseek-ocr, glm-ocr) are left out: they transcribe, they do not answer.
TASK_PREFERENCE: dict[str, list[str]] = {
    "alt_text": ["qwen2.5vl", "qwen2.5-vl", "qwen3-vl", "llama3.2-vision", "gemma3",
                 "granite3.2-vision", "minicpm-v", "llava"],
    "math": ["qwen2.5vl", "qwen3-vl", "llama3.2-vision", "granite3.2-vision", "gemma3"],
    "zone": ["qwen3-vl", "qwen2.5vl", "qwen2.5-vl", "gemma3", "moondream", "llava-phi3",
             "granite3.2-vision", "llama3.2-vision"],
    "zone_role": ["qwen3-vl", "qwen2.5vl", "qwen2.5-vl", "gemma3", "moondream", "llava-phi3",
                  "granite3.2-vision", "llama3.2-vision"],
    "table_rows": ["granite3.2-vision", "qwen2.5vl", "qwen3.5", "gemma3"],
    "table_merge_vision": ["granite3.2-vision", "qwen2.5vl", "qwen3.5", "gemma3"],
    "roles": ["llama3.2", "qwen2.5", "granite4.1", "qwen3", "mistral", "phi4"],
    "headings": ["llama3.2", "qwen2.5", "granite4.1", "qwen3", "mistral"],
    "links": ["llama3.2", "qwen2.5", "granite4.1", "qwen3"],
    "fields": ["llama3.2", "qwen2.5", "granite4.1", "qwen3"],
    "table_summary": ["qwen2.5", "llama3.2", "granite4.1", "qwen3"],
    "table_headers": ["llama3.2", "granite4.1", "qwen2.5", "qwen3"],
    "table_merge": ["llama3.2", "granite4.1", "qwen2.5", "qwen3"],
    "merge": ["qwen2.5", "llama3.2", "granite4.1", "qwen3"],
    "xref": ["llama3.2", "granite4.1", "qwen2.5", "qwen3"],
    "bibliography": ["qwen2.5", "qwen3", "llama3.1", "granite4.1", "mistral", "llama3.2"],
    "zone_list": ["qwen2.5", "qwen3", "llama3.1", "granite4.1", "llama3.2"],
    "reading_order": ["qwen2.5vl", "qwen2.5-vl", "qwen3-vl", "gemma3", "llama3.2-vision", "minicpm-v"],
    "sample_roles": ["qwen2.5", "llama3.2", "granite4.1", "qwen3", "mistral"],
    "brain": ["qwen2.5", "qwen3", "llama3.1", "granite4.1", "mistral", "llama3.2", "gemma3"],
    "study": ["qwen2.5", "qwen3", "llama3.1", "mistral", "granite4.1", "llama3.2", "gemma3"],
}
# jobs that run again and again: the smaller model of a family is the better pick
# A drawn zone is answered while the person waits, so it takes the quick model
# of the family too (V4.16): the small one answers "what is this?" just as well
# and in seconds instead of minutes.
FAST_TASKS = {"table_rows", "table_merge_vision", "table_headers", "table_merge",
              "links", "fields", "xref", "headings", "zone", "zone_role"}
OCR_ONLY = re.compile(r"(^|[-_/])ocr", re.I)


# Thinking models spend minutes "reasoning" before they answer a one-line
# question. Where Ollama supports it, the thinking is switched off for the short
# jobs (V4.16); the name tells us which models have it.
THINKS = re.compile(r"(qwen3(?!-vl)|deepseek-r1|magistral|r1-|reason|thinking|gpt-oss|phi4-reasoning)", re.I)


@dataclass
class AIConfig:
    base_url: str = DEFAULT_URL
    text_model: str = ""          # blank = pick automatically
    vision_model: str = ""        # blank = pick automatically
    enabled: bool = True
    timeout: int = 600
    keep_alive: str = "30m"       # how long Ollama keeps the model in memory
    max_image_px: int = 1100      # longest side of a picture sent to the model
    task_models: dict = field(default_factory=dict)   # task -> model ("" = general)

    def to_dict(self) -> dict:
        return asdict(self)

    def for_task(self, task: str) -> str:
        return str((self.task_models or {}).get(task, "") or "").strip()


class OllamaError(RuntimeError):
    pass


class ModelMissing(OllamaError):
    pass


# ------------------------------------------------------------------ http
# The web app serialises everything that touches MuPDF behind one lock. A model
# answering a drawn zone can take a minute on a CPU; the app sets this hook so
# that lock is let go while the tool only waits for Ollama, and the other
# pages of the editor keep working meanwhile.
_WAIT_HOOK = None


def set_wait_hook(factory) -> None:
    """factory() -> a context manager entered around every request to Ollama."""
    global _WAIT_HOOK
    _WAIT_HOOK = factory


def _waiting():
    import contextlib
    if _WAIT_HOOK is None:
        return contextlib.nullcontext()
    try:
        return _WAIT_HOOK()
    except Exception:
        return contextlib.nullcontext()


def _url(cfg: AIConfig, path: str) -> str:
    return (cfg.base_url or DEFAULT_URL).rstrip("/") + path


# how many tokens an answer may take: these are one-line answers or a short
# JSON object; the jobs that answer with a list of line numbers need more
ANSWER_CAP = {"alt_text": 400, "math": 700, "table_summary": 400,
              "roles": 1200, "headings": 1200, "bibliography": 1600,
              "zone_list": 1600, "reading_order": 1600, "brain": 1800, "study": 1400}


def shrink_png(png: bytes, max_px: int = 1100) -> bytes:
    """A picture no bigger than the model needs. A vision model turns every
    tile of the image into tokens, so a full-page crop at 2.2× costs minutes on
    a CPU and says nothing more than the same picture at 1100 px (V4.16)."""
    if not png or max_px <= 0:
        return png
    try:
        import pymupdf
        pix = pymupdf.Pixmap(png)
        n = 0
        while max(pix.width, pix.height) > max_px * (2 ** n) and n < 4:
            n += 1
        if not n:
            return png
        pix.shrink(n)
        return pix.tobytes("png")
    except Exception:
        return png


def _tune(cfg: AIConfig, payload: dict, task: str = "") -> dict:
    """What every call to a model gets: the model stays in memory between
    questions (loading a vision model from disk was most of the minute a zone
    used to take), the picture is no bigger than it needs to be, the answer is
    capped, and a thinking model is told not to think about a one-line
    question."""
    out = dict(payload)
    ka = str(getattr(cfg, "keep_alive", "") or "").strip()
    if ka and "keep_alive" not in out:
        out["keep_alive"] = ka
    opts = dict(out.get("options") or {})
    opts.setdefault("num_predict", ANSWER_CAP.get(task, 256))
    out["options"] = opts
    cap = int(getattr(cfg, "max_image_px", 0) or 0)
    if cap and isinstance(out.get("images"), list):
        imgs = []
        for img in out["images"]:
            if isinstance(img, str):
                try:
                    raw = shrink_png(base64.b64decode(img), cap)
                    img = base64.b64encode(raw).decode()
                except Exception:
                    pass
            imgs.append(img)
        out["images"] = imgs
    if THINKS.search(str(out.get("model") or "")):
        out.setdefault("think", False)
    return out


def _post(cfg: AIConfig, path: str, payload: dict, timeout: int | None = None) -> dict:
    url = _url(cfg, path)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with _waiting():
            with urllib.request.urlopen(req, timeout=timeout or cfg.timeout) as resp:
                return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode(errors="replace")
        if "think" in payload and "think" in body.lower():
            return _post(cfg, path, {k: v for k, v in payload.items() if k != "think"}, timeout)
        if exc.code == 404 and "not found" in body:
            model = payload.get("model", "?")
            _CACHE.pop(cfg.base_url, None)
            raise ModelMissing(f"model '{model}' is not installed on the Ollama server — "
                               f"run: ollama pull {model}  (or choose an installed model in Settings)")
        raise OllamaError(f"Ollama returned HTTP {exc.code}: {body}")
    except urllib.error.URLError as exc:
        raise OllamaError(f"cannot reach Ollama at {cfg.base_url}: {exc.reason}")
    except TimeoutError:
        raise OllamaError(f"Ollama did not answer within {timeout or cfg.timeout}s "
                          "(first use loads the model — try again, or raise the timeout in Settings)")
    except Exception as exc:
        raise OllamaError(f"cannot reach Ollama at {cfg.base_url}: {exc}")


def _get(cfg: AIConfig, path: str) -> dict:
    try:
        with urllib.request.urlopen(_url(cfg, path), timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        raise OllamaError(f"cannot reach Ollama at {cfg.base_url}: {exc}")


# ------------------------------------------------------- model discovery
_CACHE: dict[str, tuple[float, list[dict]]] = {}


_CAPS: dict[tuple[str, str], list[str]] = {}


def _capabilities(cfg: AIConfig, name: str) -> list[str]:
    """Ollama ≥ 0.6 reports capabilities (completion, vision, embedding, …).

    Asked once per model and kept: what a model can do does not change, and
    asking the server about ten models every minute cost a second each time."""
    key = (cfg.base_url or DEFAULT_URL, name)
    if key in _CAPS:
        return _CAPS[key]
    try:
        d = _post(cfg, "/api/show", {"model": name, "name": name}, timeout=15)
        caps = d.get("capabilities")
        if isinstance(caps, list) and caps:
            _CAPS[key] = [str(c) for c in caps]
            return _CAPS[key]
        # older servers: vision models carry a projector
        if d.get("projector_info") or "clip" in json.dumps(d.get("details", {})).lower():
            _CAPS[key] = ["completion", "vision"]
            return _CAPS[key]
    except OllamaError:
        pass
    return _CAPS.get(key, [])


def installed_models(cfg: AIConfig, refresh: bool = False) -> list[dict]:
    """[{name, vision, chat, size}] for every model on the server (cached 60 s)."""
    hit = _CACHE.get(cfg.base_url)
    if hit and not refresh and time.time() - hit[0] < 60:
        return hit[1]
    data = _get(cfg, "/api/tags")
    out = []
    for m in data.get("models", []):
        name = m.get("name") or m.get("model") or ""
        if not name:
            continue
        caps = _capabilities(cfg, name)
        if caps:
            vision = "vision" in caps
            chat = "completion" in caps and "embedding" not in caps
        else:
            vision = bool(VISION_NAME.search(name))
            chat = not NOT_CHAT.search(name)
        out.append({"name": name, "vision": vision, "chat": chat,
                    "size_gb": round((m.get("size") or 0) / 1e9, 1)})
    _CACHE[cfg.base_url] = (time.time(), out)
    return out


def _family(name: str) -> str:
    return name.split(":", 1)[0].lower()


def resolve_model(cfg: AIConfig, kind: str, task: str = "") -> tuple[str, str]:
    """-> (model to use, note). kind is 'vision' or 'text'.

    A model set for this particular job wins; otherwise the general vision or
    text model; otherwise the best installed one for the kind of work."""
    wanted = cfg.for_task(task) if task else ""
    if not wanted:
        wanted = (cfg.vision_model if kind == "vision" else cfg.text_model or "").strip()
    models = installed_models(cfg)
    names = [m["name"] for m in models]
    ok = [m for m in models if (m["vision"] if kind == "vision" else m["chat"])]
    if wanted:
        if wanted in names:
            return wanted, ""
        if ":" not in wanted and f"{wanted}:latest" in names:
            return f"{wanted}:latest", ""
        same = [m["name"] for m in ok if _family(m["name"]) == _family(wanted)]
        if same:
            return same[0], f"'{wanted}' is not installed; using '{same[0]}'"
    if not ok and kind == "text":
        ok = [m for m in models if m["vision"]]        # vision models can do text too
    if not ok:
        need = "a vision model (e.g. qwen2.5vl:7b, llama3.2-vision, gemma3:4b, llava)" \
            if kind == "vision" else "a chat model (e.g. qwen2.5:7b, llama3.1:8b)"
        have = ", ".join(names) or "none"
        raise ModelMissing(f"No suitable model on the Ollama server — install {need}. "
                           f"Installed: {have}. Example: ollama pull "
                           f"{'qwen2.5vl:7b' if kind == 'vision' else 'qwen2.5:7b'}")
    prefs = TASK_PREFERENCE.get(task) or (VISION_PREFERENCE if kind == "vision" else TEXT_PREFERENCE)
    fast = task in FAST_TASKS
    ranked = sorted(ok, key=lambda m: (
        bool(OCR_ONLY.search(m["name"])),          # OCR models transcribe, they don't answer
        kind == "text" and m["vision"],            # text work: plain chat models first
        next((i for i, p in enumerate(prefs) if _family(m["name"]).startswith(p)
              and not (kind == "text" and "vl" in _family(m["name"]))), len(prefs)),
        (m.get("size_gb") or 99) if fast else -(m.get("size_gb") or 0)))
    pick = ranked[0]["name"]
    note = (f"'{wanted}' is not installed; using '{pick}'" if wanted else f"auto-selected '{pick}'")
    return pick, note


def ping(cfg: AIConfig) -> dict:
    models = installed_models(cfg, refresh=True)
    out = {"ok": True, "models": [m["name"] for m in models], "details": models}
    for kind in ("vision", "text"):
        try:
            name, note = resolve_model(cfg, kind)
            out[f"{kind}_model"] = name
            out[f"{kind}_note"] = note
        except OllamaError as exc:
            out[f"{kind}_model"] = ""
            out[f"{kind}_note"] = str(exc)
    jobs = {}
    for task, (kind, label) in TASKS.items():
        try:
            name, _ = resolve_model(cfg, kind, task)
        except OllamaError:
            name = ""
        jobs[task] = {"kind": kind, "label": label, "set": cfg.for_task(task), "uses": name}
    out["tasks"] = jobs
    return out


# ------------------------------------------------------------- helpers
def _json_from(text: str):
    """Models sometimes wrap JSON in prose or code fences."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = text.find(opener), text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                continue
    return None


def _generate(cfg: AIConfig, kind: str, payload: dict, task: str = "") -> dict:
    try:                                  # V4.31: Dez AI shows Ollama how people here worded similar things
        from .teacher import prompt_extra
        extra = prompt_extra()
        if extra and isinstance(payload.get("prompt"), str):
            payload = dict(payload, prompt=payload["prompt"] + "\n\n" + extra)
    except Exception:
        pass
    model, _ = resolve_model(cfg, kind, task)
    try:
        return _post(cfg, "/api/generate", _tune(cfg, dict(payload, model=model), task))
    except ModelMissing:
        # the server changed under us: resolve again once
        _CACHE.pop(cfg.base_url, None)
        model, _ = resolve_model(cfg, kind, task)
        return _post(cfg, "/api/generate", _tune(cfg, dict(payload, model=model), task))


# ------------------------------------------------------------ alt text
ALT_PROMPT = (
    "You are writing alternative text for a figure in a document that must meet "
    "WCAG 2.2 and PDF/UA. Describe what the image conveys, not what it looks "
    "like decoratively. Rules: one sentence, under 150 characters, no 'image of' "
    "or 'picture of', no trailing period needed, plain factual language. "
    "If the image carries no information and is purely decorative, reply exactly: "
    "DECORATIVE.\n"
    "Nearby document text for context: __CONTEXT__\n"
    "Reply with the alt text only."
)


def alt_text(cfg: AIConfig, png_bytes: bytes, context: str = "") -> str:
    data = _generate(cfg, "vision", {
        "prompt": ALT_PROMPT.replace("__CONTEXT__", (context or "none")[:600]),
        "images": [base64.b64encode(png_bytes).decode()],
        "stream": False,
        "options": {"temperature": 0.2},
    }, task="alt_text")
    out = (data.get("response") or "").strip().strip('"')
    out = re.sub(r"^(alt text:|alt:)\s*", "", out, flags=re.I).strip()
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
    return out[:300]


# -------------------------------------------------------- role checking
ROLE_PROMPT = (
    "You are auditing the tag structure of a PDF for PDF/UA compliance. "
    "For each numbered text block below you are given the role a layout "
    "analyser proposed. Decide whether that role is right.\n"
    "Allowed roles: H1 H2 H3 H4 H5 H6 P L Caption Artifact.\n"
    "Use Artifact only for running heads, folios, page numbers and decoration.\n"
    "Headings must not skip levels relative to each other.\n"
    "Reply with JSON only: a list of objects "
    '{"i": <number>, "role": "<role>", "why": "<max 8 words>"}. '
    "Include an object ONLY for blocks whose role should change.\n\n"
    "__BLOCKS__"
)


def review_roles(cfg: AIConfig, blocks: list[dict]) -> list[dict]:
    """blocks: [{i, role, text, size, page}] -> [{i, role, why}]"""
    lines = []
    for b in blocks:
        txt = (b.get("text") or "").replace("\n", " ")[:220]
        lines.append(f'{b["i"]}. [proposed={b["role"]}, size={b.get("size", "?")}pt, '
                     f'page={b.get("page", 0) + 1}] {txt}')
    data = _generate(cfg, "text", {
        "prompt": ROLE_PROMPT.replace("__BLOCKS__", "\n".join(lines)),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1},
    }, task="roles")
    parsed = _json_from(data.get("response") or "")
    if isinstance(parsed, dict):
        for key in ("changes", "results", "blocks", "items"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            if "i" in parsed and "role" in parsed:
                parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    valid = {"H1", "H2", "H3", "H4", "H5", "H6", "P", "L", "Caption", "Artifact"}
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except Exception:
            continue
        role = str(item.get("role", "")).strip()
        if role in valid:
            out.append({"i": i, "role": role, "why": str(item.get("why", ""))[:80]})
    return out


# --------------------------------------------- doubtful tags, with samples
SAMPLE_PROMPT = (
    "You tag PDF documents for accessibility (PDF/UA) the way one client, __CLIENT__, wants them. "
    "This is how the client's own finished files tag their text (size is against the body text):\n"
    "__EXAMPLES__\n"
    "__FIXES__"
    "Now decide the tag of each numbered block below. Allowed tags: H1 H2 H3 H4 H5 H6 P Caption BlockQuote. "
    "Follow the client's examples: text that looks and reads like the client's headings gets their "
    "heading tag, ordinary sentences are P. A Caption is the title of a table or figure. "
    "When you are not sure, keep the proposed tag.\n"
    "Reply with JSON only: a list of objects "
    '{"i": <number>, "role": "<tag>", "why": "<max 8 words>"} '
    "ONLY for blocks whose tag should change.\n\n"
    "Blocks:\n__BLOCKS__"
)


def sample_roles(cfg: AIConfig, client: str, examples: list[dict], fixes: list[dict],
                 blocks: list[dict]) -> list[dict]:
    """The tags the client's samples could not settle (V4.21).
    blocks: [{i, role, text, size, bold, page, votes}] -> [{i, role, why}]"""
    def look(size, bold):
        return f"size ×{round(float(size or 1), 2)}{', bold' if bold else ''}"
    ex = "\n".join(f'- "{(e.get("text") or "")[:100]}" ({look(e.get("size"), e.get("bold"))}) → {e["role"]}'
                   for e in examples[:24]) or "- (no examples)"
    fx = ""
    if fixes:
        fx = ("People corrected the tool like this:\n" + "\n".join(
            f'- "{(x.get("text") or "")[:100]}": tool said {x["from"]} → correct is {x["to"]}' for x in fixes[:10]) + "\n")
    lines = []
    for b in blocks:
        votes = ", ".join(f"{r} {round(sh * 100)}%" for r, sh, *_ in (b.get("votes") or []))
        lines.append(f'{b["i"]}. [proposed={b["role"]}, {look(b.get("size"), b.get("bold"))}, page {b.get("page", 0) + 1}'
                     + (f", samples: {votes}" if votes else "") + f'] {(b.get("text") or "")[:200]}')
    data = _generate(cfg, "text", {
        "prompt": SAMPLE_PROMPT.replace("__CLIENT__", client or "this client").replace("__EXAMPLES__", ex)
                               .replace("__FIXES__", fx).replace("__BLOCKS__", "\n".join(lines)),
        "stream": False, "format": "json", "options": {"temperature": 0.1}}, task="sample_roles")
    parsed = _json_from(data.get("response") or "")
    if isinstance(parsed, dict):
        for key in ("changes", "results", "blocks", "items", "tags"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            parsed = [parsed] if "i" in parsed else []
    valid = {"H1", "H2", "H3", "H4", "H5", "H6", "P", "Caption", "BlockQuote"}
    out = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except Exception:
            continue
        role = str(item.get("role", "")).strip()
        if role in valid:
            out.append({"i": i, "role": role, "why": str(item.get("why", ""))[:80]})
    return out


# ------------------------------------------------------- table headers
TABLE_PROMPT = (
    "Here are the first two rows of a table extracted from a PDF.\n"
    "Row 1: __R1__\nRow 2: __R2__\n"
    'Does row 1 contain column headers? Reply JSON only: {"header": true} or {"header": false}'
)


def table_has_header(cfg: AIConfig, row1: list[str], row2: list[str]) -> bool | None:
    try:
        data = _generate(cfg, "text", {
            "prompt": TABLE_PROMPT.replace("__R1__", " | ".join(row1)[:400])
                                  .replace("__R2__", " | ".join(row2)[:400]),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0},
        }, task="table_headers")
    except OllamaError:
        return None
    parsed = _json_from(data.get("response") or "")
    if isinstance(parsed, dict) and isinstance(parsed.get("header"), bool):
        return parsed["header"]
    return None


# ------------------------------------------------------- table rows
TABLE_ROW_PROMPT = (
    "The image shows part of a page with a table. This line of text is near the "
    "table's edge:\n\"__TEXT__\"\n"
    "Is this text INSIDE the table — in a row enclosed by the table's border "
    "lines, like the other rows — or is it separate text OUTSIDE the table "
    "(below or above it, not enclosed by table borders)? "
    'Reply with JSON only: {"inside": true} or {"inside": false}.'
)


def table_row_inside(cfg: AIConfig, png_bytes: bytes, text: str) -> bool | None:
    """Vision check for one full-width row at a table's edge."""
    data = _generate(cfg, "vision", {
        "prompt": TABLE_ROW_PROMPT.replace("__TEXT__", text[:200].replace('"', "'")),
        "images": [base64.b64encode(png_bytes).decode()],
        "stream": False,
        "options": {"temperature": 0.0},
    }, task="table_rows")
    out = _json_from(data.get("response") or "")
    if isinstance(out, dict) and isinstance(out.get("inside"), bool):
        return out["inside"]
    txt = (data.get("response") or "").lower()
    if "inside" in txt and "outside" not in txt:
        return True
    if "outside" in txt and "inside" not in txt:
        return False
    return None


# ------------------------------------------------- table continues?
CONTINUE_PROMPT = (
    "Two consecutive pages of a PDF each hold a table with the same number of columns. "
    "Decide whether the table on page B is the CONTINUATION of the table on page A "
    "(one table split by a page break) or a NEW table.\n"
    "A continuation usually repeats the same column header (or has none), has no new title, "
    "and carries on the same kind of rows. A NEW table has its own title or heading, a different "
    "header row, or the table on page A visibly ended (closing note, total, empty space).\n\n"
    "PAGE A title/heading above the table: __A_TITLE__\n"
    "PAGE A column header: __A_HEAD__\n"
    "PAGE A last rows:\n__A_ROWS__\n"
    "PAGE A text after the table: __A_TRAIL__\n\n"
    "PAGE B title/heading above the table: __B_TITLE__\n"
    "PAGE B column header: __B_HEAD__\n"
    "PAGE B first rows:\n__B_ROWS__\n\n"
    "Layout hints: __HINTS__\n"
    '__IMAGES__Reply JSON only: {"same_table": true} or {"same_table": false}, with a short reason: '
    '{"same_table": <true|false>, "why": "<max 10 words>"}'
)


def table_continues(cfg: AIConfig, ctx: dict, images: list[bytes] | None = None) -> dict | None:
    """ctx: a_title a_head a_rows a_trail b_title b_head b_rows hints -> {same, why} or None.

    With images (bottom of page A, top of page B) the vision model is used."""
    def rows(v):
        return "\n".join(f"  {r}" for r in (v or [])[:3]) or "  (none)"
    prompt = (CONTINUE_PROMPT
              .replace("__A_TITLE__", (ctx.get("a_title") or "(none)")[:200])
              .replace("__A_HEAD__", (ctx.get("a_head") or "(none)")[:300])
              .replace("__A_ROWS__", rows(ctx.get("a_rows")))
              .replace("__A_TRAIL__", (ctx.get("a_trail") or "(none)")[:200])
              .replace("__B_TITLE__", (ctx.get("b_title") or "(none)")[:200])
              .replace("__B_HEAD__", (ctx.get("b_head") or "(none)")[:300])
              .replace("__B_ROWS__", rows(ctx.get("b_rows")))
              .replace("__HINTS__", (ctx.get("hints") or "none")[:300])
              .replace("__IMAGES__", "The two images are the bottom of page A, then the top of page B.\n"
                       if images else ""))
    payload = {"prompt": prompt, "stream": False, "format": "json", "options": {"temperature": 0.0}}
    if images:
        payload["images"] = [base64.b64encode(b).decode() for b in images]
    data = _generate(cfg, "vision" if images else "text", payload,
                     task="table_merge_vision" if images else "table_merge")
    out = _json_from(data.get("response") or "")
    if isinstance(out, dict) and isinstance(out.get("same_table"), bool):
        return {"same": out["same_table"], "why": str(out.get("why", ""))[:100]}
    return None


# ------------------------------------------------------ heading levels
HEAD_PROMPT = (
    "Below is the heading outline of a document, in reading order, as a layout "
    "analyser proposed it (levels came from font size alone, so they are often "
    "wrong). Decide the correct level of each heading from its wording and its "
    "place in the document.\n"
    "Rules: the first heading is H1; a level may never skip (H1 then H3 is wrong); "
    "a heading that continues the same series as the one before it keeps that level; "
    "a heading that is clearly a sub-point of the one before it goes one level deeper.\n"
    "Reply JSON only: a list of objects {\"i\": <number>, \"level\": <1-6>, "
    "\"why\": \"<max 8 words>\"}. Include an object ONLY for headings whose level "
    "should change.\n\n__HEADS__"
)


def heading_levels(cfg: AIConfig, heads: list[dict]) -> list[dict]:
    """heads: [{i, level, text, page}] -> [{i, level, why}]"""
    lines = [f'{h["i"]}. [H{h["level"]}, page {h.get("page", 0) + 1}] {(h.get("text") or "")[:120]}'
             for h in heads]
    data = _generate(cfg, "text", {
        "prompt": HEAD_PROMPT.replace("__HEADS__", "\n".join(lines)),
        "stream": False, "format": "json", "options": {"temperature": 0.1}}, task="headings")
    parsed = _json_from(data.get("response") or "")
    if isinstance(parsed, dict):
        for key in ("headings", "changes", "results", "items"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            parsed = [parsed] if "i" in parsed else []
    out = []
    for item in parsed if isinstance(parsed, list) else []:
        try:
            i, lvl = int(item["i"]), int(item["level"])
        except Exception:
            continue
        if 1 <= lvl <= 6:
            out.append({"i": i, "level": lvl, "why": str(item.get("why", ""))[:80]})
    return out


# --------------------------------------------------------- link text
LINK_PROMPT = (
    "A PDF link needs a description that makes sense when a screen-reader user "
    "hears it on its own, out of the surrounding sentence (PDF/UA 7.18.5).\n"
    "Link text on the page: __TEXT__\n"
    "Where it goes: __DEST__\n"
    "Sentence around it: __CTX__\n"
    "Write that description: under 80 characters, no 'click here', no 'link to this "
    "page', say what the reader will get. Reply with the description only."
)


def link_description(cfg: AIConfig, text: str, dest: str, context: str = "") -> str:
    data = _generate(cfg, "text", {
        "prompt": (LINK_PROMPT.replace("__TEXT__", (text or "(no visible text)")[:120])
                   .replace("__DEST__", (dest or "unknown")[:200])
                   .replace("__CTX__", (context or "none")[:400])),
        "stream": False, "options": {"temperature": 0.2}}, task="links")
    out = re.sub(r"<think>.*?</think>", "", data.get("response") or "", flags=re.S)
    return re.sub(r'^["\']|["\']$', "", out.strip().split("\n")[0].strip())[:120]


# ----------------------------------------------------- form field labels
FIELD_PROMPT = (
    "A form field in a PDF has no tooltip (/TU), which fails PDF/UA 7.18.6. "
    "From the text printed around it, write the label a screen reader should "
    "announce.\n"
    "Field type: __FT__\nField name: __NAME__\n"
    "Text to the left: __LEFT__\nText to the right: __RIGHT__\nText above: __ABOVE__\n"
    "Reply with the label only: a few words, no colon at the end, no explanation."
)


def field_label(cfg: AIConfig, ft: str, name: str, left: str = "", right: str = "",
                above: str = "") -> str:
    data = _generate(cfg, "text", {
        "prompt": (FIELD_PROMPT.replace("__FT__", ft or "text field").replace("__NAME__", (name or "?")[:60])
                   .replace("__LEFT__", (left or "none")[:120]).replace("__RIGHT__", (right or "none")[:120])
                   .replace("__ABOVE__", (above or "none")[:120])),
        "stream": False, "options": {"temperature": 0.1}}, task="fields")
    out = re.sub(r"<think>.*?</think>", "", data.get("response") or "", flags=re.S)
    return re.sub(r'^["\']|["\']$', "", out.strip().split("\n")[0].strip()).rstrip(":")[:100]


# ------------------------------------------------------- table summary
TABLE_SUMMARY_PROMPT = (
    "Summarise what a data table holds, for a screen-reader user who is about to "
    "read it cell by cell.\n"
    "Caption or heading: __CAP__\nColumn headers: __HEAD__\nFirst rows:\n__ROWS__\n"
    "Reply with one sentence under 140 characters saying what the table lists and "
    "how it is organised. No 'this table', no row counts you cannot see. "
    "Reply with the sentence only."
)


def table_summary(cfg: AIConfig, caption: str, head: list[str], rows: list[str]) -> str:
    data = _generate(cfg, "text", {
        "prompt": (TABLE_SUMMARY_PROMPT.replace("__CAP__", (caption or "none")[:160])
                   .replace("__HEAD__", " | ".join(head)[:300])
                   .replace("__ROWS__", "\n".join(f"  {r}" for r in rows[:4])[:600])),
        "stream": False, "options": {"temperature": 0.2}}, task="table_summary")
    out = re.sub(r"<think>.*?</think>", "", data.get("response") or "", flags=re.S)
    return re.sub(r'^["\']|["\']$', "", out.strip().split("\n")[0].strip())[:200]


# ------------------------------------------------------------- maths
MATH_PROMPT = (
    "The image is a region cut out of a document page. Decide what it is and, if it "
    "is mathematics, write it out for a screen reader.\n"
    "Reply JSON only:\n"
    '{"is_math": <true|false>, "decorative": <true|false>, '
    '"alt": "<the formula read aloud in words, e.g. \'x equals negative b plus or minus '
    "the square root of b squared minus four a c, all over two a'>\", "
    '"latex": "<the same formula in LaTeX, or empty>"}\n'
    "Rules: is_math is true only for an equation, formula or mathematical expression; "
    "decorative is true only when the region carries no information at all. "
    "Read symbols as words (not LaTeX) in alt; keep alt under 300 characters. "
    "Context from the page: __CONTEXT__"
)


def math_from_image(cfg: AIConfig, png_bytes: bytes, context: str = "") -> dict | None:
    """-> {is_math, decorative, alt, latex} or None when the model did not answer."""
    data = _generate(cfg, "vision", {
        "prompt": MATH_PROMPT.replace("__CONTEXT__", (context or "none")[:400]),
        "images": [base64.b64encode(png_bytes).decode()],
        "stream": False, "format": "json", "options": {"temperature": 0.1}}, task="math")
    out = _json_from(data.get("response") or "")
    if not isinstance(out, dict):
        return None
    alt = str(out.get("alt") or "").strip()[:400]
    return {"is_math": bool(out.get("is_math")), "decorative": bool(out.get("decorative")),
            "alt": alt, "latex": str(out.get("latex") or "").strip()[:400]}


# ------------------------------------------- cross-reference disambiguation
XREF_PROMPT = (
    "A document mentions \"__MENTION__\" in this sentence:\n__CTX__\n"
    "Is that a cross-reference to another part of the same document "
    "(a chapter, section, figure, table or bibliography entry), or just ordinary "
    "wording?\nCandidate targets found in the document:\n__CANDS__\n"
    'Reply JSON only: {"is_reference": <true|false>, "target": <candidate number or 0>, '
    '"why": "<max 8 words>"}'
)


def xref_match(cfg: AIConfig, mention: str, context: str, candidates: list[str]) -> dict | None:
    cands = "\n".join(f"  {i + 1}. {c[:120]}" for i, c in enumerate(candidates[:8])) or "  (none)"
    data = _generate(cfg, "text", {
        "prompt": (XREF_PROMPT.replace("__MENTION__", (mention or "")[:80])
                   .replace("__CTX__", (context or "")[:400]).replace("__CANDS__", cands)),
        "stream": False, "format": "json", "options": {"temperature": 0.0}}, task="xref")
    out = _json_from(data.get("response") or "")
    if not isinstance(out, dict) or not isinstance(out.get("is_reference"), bool):
        return None
    try:
        t = int(out.get("target") or 0)
    except Exception:
        t = 0
    return {"is_reference": out["is_reference"], "target": t, "why": str(out.get("why", ""))[:80]}


# ------------------------------------------------- merging two tags by hand
MERGE_PROMPT = (
    "Someone is joining two parts of a document that the layout analyser tagged "
    "separately, usually because a page break split them. Decide whether they are "
    "really one __KIND__, and whether the second part repeats the heading or the "
    "column header of the first (which would then be a running header, not data).\n"
    "END of the first part (page __PA__):\n__A__\n\n"
    "START of the second part (page __PB__):\n__B__\n\n"
    'Reply JSON only: {"same": <true|false>, "repeat_header": <true|false>, '
    '"why": "<max 10 words>"}'
)


def merge_check(cfg: AIConfig, kind: str, a_tail: list[str], b_head: list[str],
                page_a: int = 0, page_b: int = 0) -> dict | None:
    """Is this really one list/table/paragraph, and does part two repeat the header?"""
    def block(rows):
        return "\n".join(f"  {r[:120]}" for r in rows[:4]) or "  (empty)"
    data = _generate(cfg, "text", {
        "prompt": (MERGE_PROMPT.replace("__KIND__", kind).replace("__A__", block(a_tail))
                   .replace("__B__", block(b_head)).replace("__PA__", str(page_a + 1))
                   .replace("__PB__", str(page_b + 1))),
        "stream": False, "format": "json", "options": {"temperature": 0.0}}, task="merge")
    out = _json_from(data.get("response") or "")
    if not isinstance(out, dict) or not isinstance(out.get("same"), bool):
        return None
    return {"same": out["same"], "repeat_header": bool(out.get("repeat_header")),
            "why": str(out.get("why", ""))[:100]}


# ------------------------------------------- a list / table zone, looked at
ZONE_PROMPT = (
    "The image is one region of a document page that a person marked as a "
    "__ROLE__. Read its structure.\n"
    "Reply JSON only:\n"
    '{"kind": "table"|"list"|"paragraphs", "rows": <number of rows>, '
    '"columns": <number of columns>, "header_rows": <0, 1 or 2>, '
    '"header_column": <true|false>, "ordered": <true|false>, "why": "<max 10 words>"}\n'
    "rows/columns are only for a table: count every printed row, the header rows "
    "included, and every column, the row-label column included; a cell spanning "
    "two columns still counts as two columns. ordered says whether a list is "
    "numbered or lettered. header_rows counts the rows at the top that label the "
    "columns; header_column is true when the first column labels the rows. "
    "What the tool read there: __TEXT__"
)


def zone_structure(cfg: AIConfig, png_bytes: bytes, role: str, text: str = "") -> dict | None:
    data = _generate(cfg, "vision", {
        "prompt": (ZONE_PROMPT.replace("__ROLE__", role or "table")
                   .replace("__TEXT__", (text or "nothing")[:500])),
        "images": [base64.b64encode(png_bytes).decode()],
        "stream": False, "format": "json", "options": {"temperature": 0.0}}, task="zone")
    out = _json_from(data.get("response") or "")
    if not isinstance(out, dict):
        return None
    def num(v, lo, hi):
        try:
            return max(lo, min(hi, int(v)))
        except Exception:
            return None
    return {"kind": str(out.get("kind", "") or "").lower()[:20],
            "rows": num(out.get("rows"), 1, 200), "columns": num(out.get("columns"), 1, 60),
            "header_rows": num(out.get("header_rows"), 0, 3),
            "header_column": bool(out.get("header_column")),
            "ordered": bool(out.get("ordered")), "why": str(out.get("why", ""))[:100]}


# ------------------------------------------- a drawn zone: what is it?
ZONE_ROLE_PROMPT = (
    "The image is one region of a document page that a person marked so it can be "
    "tagged for screen readers (PDF/UA). Say what kind of content it is.\n"
    "Reply JSON only:\n"
    '{"role": "heading"|"paragraph"|"list"|"table"|"figure"|"caption"|"formula"|'
    '"note"|"quote"|"code"|"artifact", "level": <1-6 for a heading, else 0>, '
    '"why": "<max 10 words>"}\n'
    "heading: a title for the text that follows it (level 1 = chapter or document "
    "title, 2 = section, 3 = sub-section). paragraph: running text, one or more "
    "paragraphs. list: bulleted, numbered or lettered items. table: data set out in "
    "rows AND columns. figure: a photo, chart, diagram, map or illustration that "
    "carries information. caption: the short label of a figure or table. formula: "
    "mathematics. note: a footnote or side note. quote: a set-off quotation. "
    "artifact: decoration, a page number, a running header or footer, a background "
    "shape — nothing a reader needs.\n"
    "__HINTS__\n"
    "The text the tool read in the region: __TEXT__"
)
ZONE_ROLES = {"heading": "H", "paragraph": "P", "list": "L", "table": "Table",
              "figure": "Figure", "caption": "Caption", "formula": "Formula",
              "note": "Note", "quote": "BlockQuote", "code": "Code", "artifact": "Artifact"}


def zone_role(cfg: AIConfig, png_bytes: bytes, text: str = "", hints: str = "") -> dict | None:
    """-> {role (a tag name: H1…H6, P, L, Table …), level, why} or None."""
    data = _generate(cfg, "vision", {
        "prompt": (ZONE_ROLE_PROMPT.replace("__TEXT__", (text or "nothing")[:600])
                   .replace("__HINTS__", hints or "")),
        "images": [base64.b64encode(png_bytes).decode()],
        "stream": False, "format": "json", "options": {"temperature": 0.0}}, task="zone_role")
    out = _json_from(data.get("response") or "")
    if not isinstance(out, dict):
        return None
    kind = str(out.get("role", "") or "").strip().lower()
    role = ZONE_ROLES.get(kind)
    if role is None:
        return None
    try:
        level = max(0, min(6, int(out.get("level") or 0)))
    except Exception:
        level = 0
    if role == "H":
        role = f"H{level or 2}"
    return {"role": role, "level": level, "why": str(out.get("why", ""))[:100]}


# ------------------------------------------- a reference list: the entries
BIB_PROMPT = (
    "These are the printed lines of a reference list (bibliography) from a book, in "
    "reading order, one per row, numbered. One entry can run over several lines. "
    "Which lines START a new entry? An entry usually starts with the author's "
    "surname and initials (\"Adams, M. J. (1990).\"), an organisation name, or a "
    "number; a line that continues the title, the publisher or the page numbers of "
    "the entry above does not start one.\n__LINES__\n"
    'Reply JSON only: {"starts": [<line numbers that start an entry>]}'
)


def bib_entry_starts(cfg: AIConfig, lines: list[str]) -> list[int] | None:
    """-> the 0-based indexes of the lines that start an entry, or None."""
    if not lines:
        return []
    shown = lines[:160]
    body = "\n".join(f"{i + 1}: {t[:160]}" for i, t in enumerate(shown))
    data = _generate(cfg, "text", {
        "prompt": BIB_PROMPT.replace("__LINES__", body),
        "stream": False, "format": "json", "options": {"temperature": 0.0}}, task="bibliography")
    out = _json_from(data.get("response") or "")
    if not isinstance(out, dict) or not isinstance(out.get("starts"), list):
        return None
    got = set()
    for v in out["starts"]:
        try:
            k = int(v) - 1
        except Exception:
            continue
        if 0 <= k < len(shown):
            got.add(k)
    got.add(0)
    return sorted(got)


LIST_PROMPT = (
    "These are the printed lines of a list from a document, in reading order, one per "
    "row, numbered. One list item can run over several lines. Which lines START a new "
    "item? An item usually starts with a bullet, a number or a letter, or is set "
    "further out than the lines that continue it.\n__LINES__\n"
    'Reply JSON only: {"starts": [<line numbers that start an item>]}'
)


def list_item_starts(cfg: AIConfig, lines: list[str]) -> list[int] | None:
    """-> the 0-based indexes of the lines that start a list item, or None."""
    if not lines:
        return []
    shown = lines[:120]
    body = "\n".join(f"{i + 1}: {t[:160]}" for i, t in enumerate(shown))
    data = _generate(cfg, "text", {
        "prompt": LIST_PROMPT.replace("__LINES__", body),
        "stream": False, "format": "json", "options": {"temperature": 0.0}}, task="zone_list")
    out = _json_from(data.get("response") or "")
    if not isinstance(out, dict) or not isinstance(out.get("starts"), list):
        return None
    got = {0}
    for v in out["starts"]:
        try:
            k = int(v) - 1
        except Exception:
            continue
        if 0 <= k < len(shown):
            got.add(k)
    return sorted(got)


# ------------------------------------------- a page's reading order, looked at
ORDER_PROMPT = (
    "The image is one page of a document. Each red box is one block of content and "
    "its red number is the order a screen reader will read it in. Would a sighted "
    "reader go through the page in that order? The natural order: the title and "
    "headings before the text under them; a column top to bottom before the next "
    "column; a caption together with its figure or table; a sidebar or box after "
    "the main text it sits beside, unless it is clearly meant to be read first; "
    "footnotes last.\nThe blocks:\n__BLOCKS__\n"
    'Reply JSON only: {"ok": <true if the order is already right>, '
    '"order": [<every block number, in the right reading order>], '
    '"why": "<max 15 words>"}'
)


def reading_order(cfg: AIConfig, png_bytes: bytes, blocks: list[str]) -> dict | None:
    """-> {ok, order (1-based numbers, complete), why} or None."""
    k = len(blocks)
    data = _generate(cfg, "vision", {
        "prompt": ORDER_PROMPT.replace("__BLOCKS__", "\n".join(
            f"  {i + 1}: {b[:90]}" for i, b in enumerate(blocks[:80]))),
        "images": [base64.b64encode(png_bytes).decode()],
        "stream": False, "format": "json", "options": {"temperature": 0.0}}, task="reading_order")
    out = _json_from(data.get("response") or "")
    if not isinstance(out, dict):
        return None
    seen: list[int] = []
    for v in out.get("order") or []:
        try:
            x = int(v)
        except Exception:
            continue
        if 1 <= x <= k and x not in seen:
            seen.append(x)
    ok = bool(out.get("ok"))
    if len(seen) < max(2, int(0.8 * k)):
        if ok:
            return {"ok": True, "order": list(range(1, k + 1)), "why": str(out.get("why", ""))[:120]}
        return None
    for x in range(1, k + 1):                 # a block the model left out keeps its neighbour
        if x not in seen:
            seen.insert(seen.index(x - 1) + 1 if x - 1 in seen else 0, x)
    return {"ok": ok and seen == list(range(1, k + 1)), "order": seen,
            "why": str(out.get("why", ""))[:120]}


# ------------------------------------------------ the self-learning brain (V4.25)
BRAIN_PROMPT = (
    "You are the tagging brain of a PDF accessibility tool. Decide the structure tag of each text block "
    "so the PDF meets PDF/UA-1 (ISO 14289-1) and WCAG 2.2, the way a careful accessibility specialist "
    "would tag it by hand.\n"
    "Rules:\n"
    "- H1-H6 only for real headings: a short title for the text that follows it, never a full sentence or "
    "a paragraph. Levels follow the outline of the document and never skip (an H2 is followed by H2 or H3, "
    "not H4).\n"
    "- P for running text, sentences, labels, list-like lines and anything that is not a heading.\n"
    "- Caption only for the title or label of a figure or table printed next to it.\n"
    "- BlockQuote only for a quotation set off from the text.\n"
    "- Text that looks and reads like the finished examples gets the same tag as those examples; the "
    "finished examples come from correctly tagged files and outweigh the tool's own guess.\n"
    "__CLIENT_RULES__"
    "Each block shows the tag the tool proposed, how it looks (size against the body text, bold, font), "
    "the heading before it, the tool's own vote, and similar texts from finished files with their tags.\n\n"
    "Blocks:\n__BLOCKS__\n\n"
    'Reply with JSON only: {"tags": [{"i": <number>, "role": "<H1|H2|H3|H4|H5|H6|P|Caption|BlockQuote>", '
    '"confidence": <0.0-1.0>, "why": "<max 10 words>"}]} with one object for EVERY block.'
)
BRAIN_ROLES = {"H1", "H2", "H3", "H4", "H5", "H6", "P", "Caption", "BlockQuote"}


def brain_roles(cfg: AIConfig, client: str, rules: list[str], blocks: list[dict]) -> list[dict]:
    """blocks: [{i, role, text, size, bold, font, page, after, votes, similar}] -> [{i, role, confidence, why}]"""
    def look(b):
        return f"size ×{round(float(b.get('size') or 1), 2)}{', bold' if b.get('bold') else ''}" + \
            (f", font {b['font']}" if b.get("font") else "")
    lines = []
    for b in blocks:
        votes = " · ".join(f"{r} {round(sh * 100)}%" for r, sh in (b.get("votes") or [])[:3])
        head = (f'[proposed={b["role"]}, {look(b)}, page {int(b.get("page") or 0) + 1}'
                + (f', after {b["after"]}' if b.get("after") else "")
                + (f", tool votes: {votes}" if votes else "") + "]")
        lines.append(f'{b["i"]}. {head} {(b.get("text") or "")[:220]}')
        sim = b.get("similar") or []
        if sim:
            lines.append("   similar finished texts: " + "; ".join(
                f'"{(x.get("text") or "")[:70]}" → {x["role"]}' for x in sim[:3]))
    cr = ""
    if rules:
        cr = (f"How the client {client} tags its files (learned from its finished files):\n"
              + "\n".join(f"- {r}" for r in rules[:10]) + "\n")
    data = _generate(cfg, "text", {
        "prompt": BRAIN_PROMPT.replace("__CLIENT_RULES__", cr).replace("__BLOCKS__", "\n".join(lines)),
        "stream": False, "format": "json", "options": {"temperature": 0.1}}, task="brain")
    parsed = _json_from(re.sub(r"<think>.*?</think>", "", data.get("response") or "", flags=re.S))
    if isinstance(parsed, dict):
        for key in ("tags", "blocks", "results", "items", "changes"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            parsed = [parsed] if "i" in parsed else []
    out = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except Exception:
            continue
        role = str(item.get("role", "")).strip()
        if role not in BRAIN_ROLES:
            continue
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.7))))
        except Exception:
            conf = 0.7
        out.append({"i": i, "role": role, "confidence": conf, "why": str(item.get("why", ""))[:80]})
    return out


STUDY_PROMPT = (
    "You are an expert in PDF/UA-1 and WCAG 2.2 document accessibility. A finished, correctly tagged PDF "
    "of the client __CLIENT__ was measured by a tagging tool. Study how it is tagged and write down the "
    "client's tagging rules, so the tool tags the next file of this kind the same way.\n\n"
    "__DIGEST__\n\n"
    'Reply with JSON only: {"doc_type": "<one of: __TYPES__>", '
    '"summary": "<one sentence: what this document is and how it is structured>", '
    '"rules": [{"rule": "<one concrete rule, max 25 words, e.g. which look or wording gets which tag>", '
    '"tag": "<the tag it is about>"}]} — at most 10 rules, the most useful first.'
)


def study_sample(cfg: AIConfig, client: str, digest: str, types: list[str]) -> dict | None:
    data = _generate(cfg, "text", {
        "prompt": STUDY_PROMPT.replace("__CLIENT__", client or "every client").replace("__DIGEST__", digest[:6000])
                              .replace("__TYPES__", ", ".join(types)),
        "stream": False, "format": "json", "options": {"temperature": 0.2}}, task="study")
    out = _json_from(re.sub(r"<think>.*?</think>", "", data.get("response") or "", flags=re.S))
    if not isinstance(out, dict):
        return None
    rules = []
    for r in out.get("rules") or []:
        if isinstance(r, dict) and str(r.get("rule") or "").strip():
            rules.append({"rule": " ".join(str(r["rule"]).split())[:200], "tag": str(r.get("tag") or "")[:20]})
        elif isinstance(r, str) and r.strip():
            rules.append({"rule": " ".join(r.split())[:200], "tag": ""})
    dt = str(out.get("doc_type") or "").strip().lower().replace(" ", "_")
    return {"doc_type": dt if dt in types else "", "summary": " ".join(str(out.get("summary") or "").split())[:300],
            "rules": rules[:10]}


EMBED_PREFERENCE = ["nomic-embed-text", "mxbai-embed-large", "bge-m3", "snowflake-arctic-embed", "all-minilm",
                    "granite-embedding", "embeddinggemma"]


def embedding_model(cfg: AIConfig) -> str:
    """The installed embedding model ('' when there is none)."""
    models = installed_models(cfg)
    embs = [m["name"] for m in models if not m["chat"] or NOT_CHAT.search(m["name"])]
    embs = [n for n in embs if re.search(r"embed|bge|minilm|nomic|arctic", n, re.I)]
    if not embs:
        return ""
    embs.sort(key=lambda n: next((i for i, p in enumerate(EMBED_PREFERENCE) if _family(n).startswith(p)),
                                 len(EMBED_PREFERENCE)))
    return embs[0]


def embed(cfg: AIConfig, texts: list[str], model: str) -> list[list[float]]:
    """Vectors from the embedding model (Ollama /api/embed, or the older /api/embeddings)."""
    if not texts:
        return []
    payload = {"model": model, "input": [t[:500] for t in texts]}
    ka = str(getattr(cfg, "keep_alive", "") or "").strip()
    if ka:
        payload["keep_alive"] = ka
    try:
        d = _post(cfg, "/api/embed", payload, timeout=max(60, int(cfg.timeout or 60)))
        vecs = d.get("embeddings")
        if isinstance(vecs, list) and len(vecs) == len(texts):
            return vecs
    except ModelMissing:
        raise
    except OllamaError as exc:
        if "404" not in str(exc):
            raise
    out = []
    for t in texts:
        d = _post(cfg, "/api/embeddings", {"model": model, "prompt": t[:500]}, timeout=60)
        out.append(d.get("embedding") or [])
    return out


# ------------------------------------------------ Ollama teaches Dez AI (V4.31)
# Every question above goes through a gate (core/teacher.py): Dez AI answers first when it has been taught the answer
# (its lessons, or a student proven against Ollama's own answers), and asks Ollama — keeping the answer as a lesson —
# when it has not. `fn(cfg, ..., _direct=True)` asks Ollama only, as before.
try:
    from . import teacher as _teacher
    _teacher.install(globals())
except Exception:                                            # the tool works without it
    pass
