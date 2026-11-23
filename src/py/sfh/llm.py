"""LLM providers: OpenAI-compatible Chat Completions and Anthropic Messages (both streaming over
urllib) and a deterministic mock.

A stream is an iterable of normalized events (blocking; consume it in a thread):
  ("block", index, block)            new content block (text/thinking empty, tool_use without input)
  ("delta", index, kind, text)       kind: "text" | "thinking" | "partial_json" | "signature"
  ("usage", usage)                   token counts / cost of this response (normalize_usage()), before stop
  ("stop", stop_reason)              end of message
close() aborts it from any thread.

A provider is a dict {"id", "type": "openai"|"anthropic"|"mock", "base_url", "model", "cheap_model",
"api_key_credential"}; see providers()/resolve_provider()."""
import json
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
ANTHROPIC_MODEL = "claude-sonnet-5-5"
ANTHROPIC_CHEAP_MODEL = "claude-haiku-4-5-20251001"
MOCK_CONTEXT_WINDOW = 200000
# effort levels, lowest first: Anthropic's output_config.effort / OpenAI-style reasoning_effort values
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORTS = ["low", "medium", "high"]  # offered when the provider doesn't say what a model supports
MOCK_EFFORTS = ["low", "medium", "high", "max"]
MOCK = {"id": "mock", "type": "mock", "base_url": None, "model": "mock", "cheap_model": "mock",
        "api_key_credential": None}


class LLMError(Exception):
    """retryable: the request may succeed when repeated (timeouts, dropped connections, 429/5xx)."""

    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


NO_PROVIDER = ("No LLM provider is configured. Set one up in the web page (Settings, or the setup screen), "
               "or set $AI_ENDPOINT and $AI_MODEL (and $AI_TOKEN if it needs a key) and restart the server.")


class NoProviderError(LLMError):
    def __init__(self, message=NO_PROVIDER):
        super().__init__(message)


def mock_enabled():
    """The deterministic mock provider only exists for tests: $SFH_MOCK=1 (or `sh FILE --mock`)."""
    return os.environ.get("SFH_MOCK") == "1"


RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
DEFAULT_TIMEOUT = 30          # connect + wait for the response to start (settings.request_timeout)
DEFAULT_IDLE_TIMEOUT = 120    # silence in the middle of a stream (settings.stream_idle_timeout)


def env_provider():
    """The built-in provider from $AI_ENDPOINT/$AI_MODEL/$AI_TOKEN/$AI_CHEAP_MODEL (None if unset)."""
    base, model = os.environ.get("AI_ENDPOINT"), os.environ.get("AI_MODEL")
    if not (base and model):
        return None
    return {"id": "env", "type": "openai", "base_url": base.rstrip("/"), "model": model,
            "cheap_model": os.environ.get("AI_CHEAP_MODEL") or model, "api_key_credential": "AI_TOKEN",
            "pricing_url": os.environ.get("AI_PRICING_URL")}


def providers(settings, creds):
    """All providers: env (if set), the configured ones, then the built-in anthropic and (in test mode,
    mock_enabled()) mock. Without test mode, providers of type mock are left out."""
    out = []
    mock = mock_enabled()
    env = env_provider()
    if env:
        out.append(dict(env, builtin=True))
    for p in settings.get("providers") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        t = p.get("type") or "openai"
        if t == "mock" and not mock:
            continue
        out.append({"id": str(p["id"]), "type": t, "base_url": (p.get("base_url") or "").rstrip("/") or None,
                    "model": p.get("model") or (ANTHROPIC_MODEL if t == "anthropic" else None),
                    "cheap_model": p.get("cheap_model") or p.get("model") or
                    (ANTHROPIC_CHEAP_MODEL if t == "anthropic" else None),
                    "api_key_credential": p.get("api_key_credential"), "pricing_url": p.get("pricing_url"),
                    "builtin": False})
    if not any(p["id"] == "anthropic" for p in out):
        out.append({"id": "anthropic", "type": "anthropic", "base_url": None, "model": ANTHROPIC_MODEL,
                    "cheap_model": ANTHROPIC_CHEAP_MODEL, "api_key_credential": "ANTHROPIC_API_KEY", "builtin": True})
    if mock and not any(p["id"] == "mock" for p in out):
        out.append(dict(MOCK, builtin=True))
    for p in out:
        p["has_key"] = bool(p.get("api_key_credential") and creds.get(p["api_key_credential"]))
    return out


def resolve_provider(settings, creds, provider_id=None):
    """The active provider, with the model/cheap_model settings overrides applied. With provider_id (a
    session's own choice) that provider is returned instead; the global model overrides only apply when
    it is the globally selected one."""
    want = settings.get("provider") or "auto"
    allp = providers(settings, creds)
    if provider_id:
        chosen = next((p for p in allp if p["id"] == provider_id), None)
        if chosen is None:
            raise LLMError("unknown provider %r" % provider_id)
        try:
            glob = resolve_provider(settings, creds)
        except LLMError:
            glob = None
        if glob and glob["id"] == provider_id:
            return glob
        return dict(chosen)
    chosen = None
    if want == "auto":
        configured = [p for p in allp if not p["builtin"]]
        chosen = next((p for p in allp if p["id"] == "env"), None) or (configured[0] if configured else None)
        if chosen is None:
            anth = next(p for p in allp if p["id"] == "anthropic")
            mock = next((p for p in allp if p["id"] == "mock"), None)
            chosen = anth if anth["has_key"] else mock
            if chosen is None:
                raise NoProviderError()
    else:
        chosen = next((p for p in allp if p["id"] == want), None)
        if chosen is None:
            if want == "mock":
                raise NoProviderError()
            raise LLMError("unknown provider %r" % want)
    chosen = dict(chosen)
    if chosen["type"] != "mock":
        chosen["model"] = settings.get("model") or chosen["model"]
        chosen["cheap_model"] = settings.get("cheap_model") or chosen["cheap_model"] or chosen["model"]
    return chosen


def configured(settings, creds):
    """Whether a usable provider is set up (the "P" onboarding signal): the global provider resolves and has
    a model. The token is optional (local endpoints need none), except for the built-in anthropic provider,
    which is only usable with a key. In test mode the mock counts."""
    try:
        p = resolve_provider(settings, creds)
    except LLMError:
        return False
    if p["type"] == "mock":
        return True
    if p["id"] == "anthropic" and p.get("builtin") and not p.get("has_key"):
        return False
    return bool(p.get("model")) and (p["type"] == "anthropic" or bool(p.get("base_url")))


def public(provider):
    return {k: provider.get(k) for k in ("id", "type", "model", "cheap_model")}


def api_key(provider, creds):
    name = provider.get("api_key_credential")
    return creds.get(name) if name else None


def sanitize_messages(messages, model=None):
    """Turns stored messages into a valid Messages API history: drops empty/unsigned blocks, merges
    consecutive same-role messages, and makes sure it starts with a user message. Signed thinking from a
    different model than `model` is dropped (signatures are only valid for the model that made them)."""
    out = []
    for m in messages:
        blocks = []
        other_model = bool(model and m.get("model") and m["model"] != model)
        for b in m["content"]:
            t = b.get("type")
            if t == "text":
                if b.get("text"):
                    blocks.append({"type": "text", "text": b["text"]})
            elif t in ("thinking", "redacted_thinking") and other_model:
                continue
            elif t == "thinking":
                if b.get("signature") and b.get("thinking"):
                    blocks.append({"type": "thinking", "thinking": b["thinking"], "signature": b["signature"]})
            elif t == "redacted_thinking":
                blocks.append({"type": "redacted_thinking", "data": b.get("data", "")})
            elif t == "tool_use":
                inp = b.get("input")
                blocks.append({"type": "tool_use", "id": b["id"], "name": b["name"],
                               "input": inp if isinstance(inp, dict) else {}})
            elif t == "tool_result":
                blocks.append({"type": "tool_result", "tool_use_id": b["tool_use_id"],
                               "content": b.get("content") or "(no output)", "is_error": bool(b.get("is_error"))})
        if not blocks:
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": m["role"], "content": blocks})
    while out and out[0]["role"] != "user":
        out.pop(0)
    # tool_result blocks must come first in a user message
    for m in out:
        if m["role"] == "user":
            m["content"].sort(key=lambda b: 0 if b["type"] == "tool_result" else 1)
    return out


def last_user_text(messages):
    for m in reversed(messages):
        if m["role"] != "user":
            continue
        texts = [b.get("text", "") for b in m["content"] if b.get("type") == "text"]
        if texts:
            return "\n".join(texts)
    return ""


# --- HTTP + SSE plumbing ------------------------------------------------------------------------

def _open(url, headers, body, what, timeout=None, idle_timeout=None, method="POST"):
    """Opens the request. `timeout` bounds connecting and waiting for the response to start;
    afterwards reads time out after `idle_timeout` seconds of silence."""
    timeout = timeout or DEFAULT_TIMEOUT
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        try:
            err = json.loads(detail)["error"]
            detail = err.get("message") or err if isinstance(err, dict) else err
        except Exception:
            pass
        raise LLMError("%s API error %d: %s" % (what, e.code, str(detail or e.reason)[:2000]),
                       retryable=e.code in RETRYABLE_STATUS or e.code >= 500) from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise LLMError("%s API at %s did not respond within %ss" % (what, url, timeout), retryable=True) from None
        raise LLMError("cannot reach %s API at %s: %s" % (what, url, reason), retryable=True) from None
    except (TimeoutError, socket.timeout):
        raise LLMError("%s API at %s did not respond within %ss" % (what, url, timeout), retryable=True) from None
    except (ConnectionError, OSError) as e:
        raise LLMError("cannot reach %s API at %s: %s" % (what, url, e), retryable=True) from None
    if idle_timeout:
        try:
            resp.fp.raw._sock.settimeout(idle_timeout)
        except Exception:
            pass
    return resp


USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _num(v):
    try:
        return float(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def normalize_usage(raw, kind):
    """Provider usage -> {"input_tokens" (uncached), "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "cost"?}. cost (USD) only when the provider reports one (e.g. OpenRouter's
    usage.cost); most don't."""
    if not isinstance(raw, dict):
        return None
    if kind == "anthropic":
        u = {"input_tokens": raw.get("input_tokens") or 0, "output_tokens": raw.get("output_tokens") or 0,
             "cache_read_tokens": raw.get("cache_read_input_tokens") or 0,
             "cache_write_tokens": raw.get("cache_creation_input_tokens") or 0}
    else:
        det = raw.get("prompt_tokens_details") or {}
        cached = det.get("cached_tokens") or raw.get("cache_read_input_tokens") or 0
        u = {"input_tokens": max(0, (raw.get("prompt_tokens") or raw.get("input_tokens") or 0) - cached),
             "output_tokens": raw.get("completion_tokens") or raw.get("output_tokens") or 0,
             "cache_read_tokens": cached,
             "cache_write_tokens": det.get("cache_write_tokens") or raw.get("cache_creation_input_tokens") or 0}
    u = {k: int(_num(v) or 0) for k, v in u.items()}
    for key in ("cost", "total_cost", "cost_usd"):
        c = _num(raw.get(key))
        if c is not None:
            u["cost"] = c
            break
    return u


def add_usage(total, u):
    """Adds one response's usage into a running total {..USAGE_KEYS, cost, calls, priced_calls,
    estimated_calls}. estimated_calls counts the priced responses whose cost came from the provider's price
    list (u["cost_estimated"]) rather than from the provider's response."""
    total = dict(total or {})
    for k in USAGE_KEYS:
        total[k] = int(total.get(k) or 0) + int((u or {}).get(k) or 0)
    total["calls"] = int(total.get("calls") or 0) + 1
    total["cost"] = float(total.get("cost") or 0.0)
    total["priced_calls"] = int(total.get("priced_calls") or 0)
    if u and u.get("cost") is not None:
        total["cost"] += float(u["cost"])
        total["priced_calls"] += 1
    total["estimated_calls"] = int(total.get("estimated_calls") or 0)
    if u and u.get("cost") is not None and u.get("cost_estimated"):
        total["estimated_calls"] += 1
    return total


def backoff_delay(attempt, base=2.0, cap=30.0):
    """Exponential backoff with jitter: attempt 1 -> ~base, 2 -> ~2*base, ... each scaled by 0.5-1.5."""
    return min(cap, float(base) * 2 ** (attempt - 1)) * random.uniform(0.5, 1.5)


def retry_policy(settings):
    """(retries, base_delay, request_timeout, idle_timeout) from settings, with sane bounds."""
    def num(key, default, lo, hi, cast=float):
        try:
            v = cast(settings.get(key))
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))
    return (num("max_retries", 3, 0, 20, int), num("retry_base_delay", 2.0, 0.0, 120.0),
            num("request_timeout", DEFAULT_TIMEOUT, 0.1, 3600.0), num("stream_idle_timeout", DEFAULT_IDLE_TIMEOUT, 0.1, 3600.0))


class RetryingStream:
    """Wraps a stream factory `make(attempt)`; when an attempt fails with a retryable LLMError it waits
    (backoff + jitter) and starts over, at most `retries` times. Before each wait it yields
    ("retry", attempt, retries, delay, error_text): consumers drop the partial output of the failed
    attempt when they see it (a new attempt streams the whole message again)."""

    def __init__(self, make, retries=3, base_delay=2.0):
        self.make = make
        self.retries = int(retries)
        self.base_delay = base_delay
        self.closed = False
        self.cur = None
        self._wake = threading.Event()

    def close(self):
        self.closed = True
        self._wake.set()
        cur = self.cur
        if cur is not None:
            cur.close()

    def __iter__(self):
        attempt = 0
        while not self.closed:
            try:
                self.cur = self.make(attempt)
                if self.closed:
                    self.cur.close()
                    return
                for ev in self.cur:
                    yield ev
                return
            except LLMError as e:
                if self.closed:
                    return
                if not e.retryable or attempt >= self.retries:
                    if attempt:
                        raise LLMError("%s (gave up after %d %s)" % (e, attempt + 1, "attempts"),
                                       e.retryable) from None
                    raise
                attempt += 1
                delay = backoff_delay(attempt, self.base_delay)
                yield ("retry", attempt, self.retries, delay, str(e))
                self._wake.wait(delay)


def call_with_retries(fn, retries=3, base_delay=2.0, sleep=time.sleep):
    """Calls fn() (blocking), retrying retryable LLMErrors with backoff + jitter."""
    attempt = 0
    while True:
        try:
            return fn()
        except LLMError as e:
            if not e.retryable or attempt >= retries:
                raise
            attempt += 1
            sleep(backoff_delay(attempt, base_delay))


class SSEStream:
    """Base for HTTP SSE streams: subclasses implement open() and convert(event_name, data_str)."""

    def __init__(self, timeout=None, idle_timeout=None):
        self.resp = None
        self.closed = False
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.idle_timeout = idle_timeout or DEFAULT_IDLE_TIMEOUT

    def close(self):
        self.closed = True
        r = self.resp
        if r is None:
            return
        try:  # shutdown wakes a reader blocked in recv() on another thread
            sock = r.fp.raw._sock
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            r.close()
        except Exception:
            pass

    def __iter__(self):
        self.resp = self.open()
        if self.closed:
            self.close()
            return
        event = None
        data_lines = []
        try:
            for raw in self.resp:
                if self.closed:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif line == "" and data_lines:
                    data = "\n".join(data_lines)
                    data_lines = []
                    for ev in self.convert(event, data):
                        yield ev
                    event = None
            if data_lines:
                for ev in self.convert(event, "\n".join(data_lines)):
                    yield ev
            for ev in self.finish():
                yield ev
        except (TimeoutError, socket.timeout):
            if self.closed:
                return
            raise LLMError("the provider stopped sending data (no data for %ss)" % self.idle_timeout,
                           retryable=True) from None
        except (OSError, ValueError, AttributeError) as e:
            if self.closed:
                return
            raise LLMError("stream interrupted: %s" % (e or type(e).__name__), retryable=True) from None
        finally:
            try:
                self.resp.close()
            except Exception:
                pass

    def finish(self):
        return ()


# --- Anthropic ---------------------------------------------------------------------------------

def _anthropic_base(provider, creds):
    base = (provider.get("base_url") or creds.get("ANTHROPIC_BASE_URL") or API_URL).rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _anthropic_headers(provider, creds, stream):
    key = api_key(provider, creds)
    if not key:
        raise LLMError("no %s credential configured" % provider.get("api_key_credential"))
    return {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json"}


def _anthropic_post(provider, creds, body, stream, timeout=None, idle_timeout=None):
    return _open(_anthropic_base(provider, creds) + "/v1/messages", _anthropic_headers(provider, creds, stream),
                 body, "Anthropic", timeout, idle_timeout)


def build_body(model, system, messages, tools=None, max_tokens=16000, thinking_budget=0, effort=None,
               adaptive=None):
    """With an effort level: output_config.effort, and adaptive thinking unless the model is known not to
    support it (adaptive False; then the thinking budget applies as without an effort)."""
    body = {"model": model, "max_tokens": int(max_tokens), "messages": sanitize_messages(messages, model)}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    if effort:
        body["output_config"] = {"effort": effort}
        if adaptive is not False:
            body["thinking"] = {"type": "adaptive"}
            return body
    if thinking_budget and int(thinking_budget) > 0:
        budget = max(1024, int(thinking_budget))
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if body["max_tokens"] <= budget:
            body["max_tokens"] = budget + 4096
    return body


class AnthropicStream(SSEStream):
    def __init__(self, provider, creds, body, timeout=None, idle_timeout=None):
        super().__init__(timeout, idle_timeout)
        self.provider = provider
        self.creds = creds
        self.body = dict(body, stream=True)

    def open(self):
        return _anthropic_post(self.provider, self.creds, self.body, True, self.timeout, self.idle_timeout)

    def convert(self, event, raw):
        d = json.loads(raw)
        t = d.get("type") or event
        if t == "message_start":
            self._usage = dict((d.get("message") or {}).get("usage") or {})
        if t == "content_block_start":
            b = dict(d["content_block"])
            if b.get("type") == "tool_use":
                b.pop("input", None)
            elif b.get("type") == "text":
                b["text"] = ""
            elif b.get("type") == "thinking":
                b["thinking"] = ""
                b.setdefault("signature", "")
            yield ("block", d["index"], b)
        elif t == "content_block_delta":
            delta = d["delta"]
            dt = delta.get("type")
            if dt == "text_delta":
                yield ("delta", d["index"], "text", delta.get("text", ""))
            elif dt == "thinking_delta":
                yield ("delta", d["index"], "thinking", delta.get("thinking", ""))
            elif dt == "signature_delta":
                yield ("delta", d["index"], "signature", delta.get("signature", ""))
            elif dt == "input_json_delta":
                yield ("delta", d["index"], "partial_json", delta.get("partial_json", ""))
        elif t == "message_delta":
            sr = (d.get("delta") or {}).get("stop_reason")
            if sr:
                self._stop = sr
            if isinstance(d.get("usage"), dict):  # cumulative counts
                self._usage = dict(getattr(self, "_usage", None) or {}, **{k: v for k, v in d["usage"].items()
                                                                          if v is not None})
        elif t == "message_stop":
            u = normalize_usage(getattr(self, "_usage", None), "anthropic")
            if u:
                yield ("usage", u)
            yield ("stop", getattr(self, "_stop", "end_turn"))
        elif t == "error":
            err = d.get("error") or {}
            raise LLMError("Anthropic stream error: %s" % (err.get("message") or err),
                           retryable=err.get("type") in ("overloaded_error", "api_error", "rate_limit_error"))


# --- OpenAI-compatible (Chat Completions) ------------------------------------------------------

def _openai_headers(provider, creds, stream):
    h = {"content-type": "application/json", "accept": "text/event-stream" if stream else "application/json"}
    key = api_key(provider, creds)
    if key:
        h["authorization"] = "Bearer " + key
    return h


def _tool_result_text(content):
    if isinstance(content, list):
        content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
    return str(content or "(no output)")


def openai_messages(system, messages):
    """Stored (Anthropic-shaped) history -> Chat Completions messages. Thinking is dropped."""
    out = [{"role": "system", "content": system}] if system else []
    for m in messages:
        texts = [b["text"] for b in m["content"] if b.get("type") == "text" and b.get("text")]
        if m["role"] == "assistant":
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in m["content"] if b.get("type") == "tool_use"]
            if not texts and not calls:
                continue
            msg = {"role": "assistant", "content": "\n\n".join(texts) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    text = _tool_result_text(b.get("content"))
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": ("ERROR: " + text) if b.get("is_error") else text})
            if texts:
                out.append({"role": "user", "content": "\n\n".join(texts)})
    return out


def openai_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object",
                                                                                      "properties": {}}}}
            for t in tools or []]


def build_openai_body(model, system, messages, tools=None, max_tokens=16000, effort=None):
    body = {"model": model, "messages": openai_messages(system, messages), "max_tokens": int(max_tokens),
            "stream_options": {"include_usage": True}}
    if effort:
        body["reasoning_effort"] = effort
    if tools:
        body["tools"] = openai_tools(tools)
    return body


REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking")
STOP_REASONS = {"tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens",
                "stop": "end_turn", "content_filter": "refusal"}


class OpenAIStream(SSEStream):
    def __init__(self, provider, creds, body, timeout=None, idle_timeout=None):
        super().__init__(timeout, idle_timeout)
        self.provider = provider
        self.creds = creds
        self.body = dict(body, stream=True)
        self.next_index = 0
        self.cur = None            # (kind, index) of the open text/thinking block
        self.calls = {}            # tool call index -> {"block": idx|None, "id", "name", "args"}
        self.finish_reason = None
        self.stopped = False
        self.usage = None

    def open(self):
        url = self.provider["base_url"].rstrip("/") + "/chat/completions"
        return _open(url, _openai_headers(self.provider, self.creds, True), self.body, "OpenAI-compatible",
                     self.timeout, self.idle_timeout)

    def _new_block(self, block):
        idx = self.next_index
        self.next_index += 1
        return idx, ("block", idx, block)

    def _piece(self, kind, text):
        if not text:
            return
        if not self.cur or self.cur[0] != kind:
            idx, ev = self._new_block({"type": kind, kind: ""})
            self.cur = (kind, idx)
            yield ev
        yield ("delta", self.cur[1], kind, text)

    def convert(self, event, raw):
        if raw.strip() == "[DONE]":
            yield from self.finish()
            return
        d = json.loads(raw)
        if d.get("error"):
            err = d["error"]
            raise LLMError("OpenAI-compatible stream error: %s" % (err.get("message") if isinstance(err, dict)
                                                                   else err))
        if isinstance(d.get("usage"), dict):
            self.usage = dict(d["usage"])
            if d.get("cost") is not None and "cost" not in self.usage:
                self.usage["cost"] = d["cost"]
        for ch in d.get("choices") or []:
            delta = ch.get("delta") or ch.get("message") or {}
            for f in REASONING_FIELDS:
                v = delta.get(f)
                if isinstance(v, str):
                    yield from self._piece("thinking", v)
            if isinstance(delta.get("content"), str):
                yield from self._piece("text", delta["content"])
            for tc in delta.get("tool_calls") or []:
                yield from self._tool_delta(tc)
            if ch.get("finish_reason"):
                self.finish_reason = ch["finish_reason"]

    def _tool_delta(self, tc):
        i = tc.get("index", len(self.calls))
        c = self.calls.setdefault(i, {"block": None, "id": None, "name": "", "args": ""})
        fn = tc.get("function") or {}
        if tc.get("id"):
            c["id"] = c["id"] or tc["id"]
        if fn.get("name"):
            c["name"] += fn["name"] if c["block"] is None else ""
        c["args"] += fn.get("arguments") or ""
        if c["block"] is None and c["name"]:
            self.cur = None
            idx, ev = self._new_block({"type": "tool_use", "id": c["id"] or "call_" + os.urandom(6).hex(),
                                       "name": c["name"]})
            c["block"] = idx
            yield ev
            if c["args"]:
                yield ("delta", idx, "partial_json", c["args"])
        elif c["block"] is not None and fn.get("arguments"):
            yield ("delta", c["block"], "partial_json", fn["arguments"])

    def finish(self):
        if self.stopped:
            return
        self.stopped = True
        has_calls = any(c["block"] is not None for c in self.calls.values())
        sr = STOP_REASONS.get(self.finish_reason or "stop", "end_turn")
        if has_calls and sr == "end_turn":
            sr = "tool_use"
        u = normalize_usage(self.usage, "openai")
        if u:
            yield ("usage", u)
        yield ("stop", sr)


# --- mock --------------------------------------------------------------------------------------

BASH_RE = re.compile(r"\[\[bash:(.*?)\]\]", re.S)
FLAKY_RE = re.compile(r"\[\[flaky:(\d+)\]\]")      # the first N attempts fail mid-stream (retryable)
TOOL_RE = re.compile(r"\[\[tool:(\w+):(\{.*?\})\]\]", re.S)  # any tool call with a JSON input
SUBAGENT_RE = re.compile(r"\[\[subagent:((?:[^\[\]]|\[\[[^\[\]]*\]\])*)\]\]", re.S)  # allows one nested [[..]]
FORK_RE = re.compile(r"\[\[fork:((?:[^\[\]]|\[\[[^\[\]]*\]\])*)\]\]", re.S)          # forked subagent thread


class MockStream:
    CHUNK = 6

    def __init__(self, messages, attempt=0, effort=None):
        self.messages = messages
        self.attempt = attempt
        self.effort = effort
        self.closed = False
        self._wake = threading.Event()

    def close(self):
        self.closed = True
        self._wake.set()

    def _chunks(self, s):
        for i in range(0, len(s), self.CHUNK):
            yield s[i:i + self.CHUNK]

    def __iter__(self):
        # peer-sessions notes are not prompts; consecutive user messages (tool results, then delivered peer
        # messages) count as one, like the real providers see them
        msgs = []
        for m in self.messages:
            content = [b for b in m["content"] if not b.get("peer_sessions")]
            if not content:
                continue
            if msgs and msgs[-1]["role"] == m["role"]:
                msgs[-1] = dict(msgs[-1], content=msgs[-1]["content"] + content)
            else:
                msgs.append(dict(m, content=content))
        last = msgs[-1] if msgs else {"role": "user", "content": []}
        text = last_user_text(msgs)
        # tool/control triggers ([[bash:]], [[subagent:]], [[fork:]], [[tool:]]) only match the newest text block
        trigger_text = next((b.get("text", "") for b in reversed(last["content"]) if b.get("type") == "text"), text)
        # tests shorten these via $SFH_MOCK_DELAY / $SFH_MOCK_SLOW_DELAY (seconds per streamed chunk)
        delay = float(os.environ.get("SFH_MOCK_SLOW_DELAY" if "[[slow]]" in text else "SFH_MOCK_DELAY",
                                     0.3 if "[[slow]]" in text else 0.02))
        results = [b for b in last["content"] if b.get("type") == "tool_result"]
        flaky = FLAKY_RE.search(trigger_text)
        idx = 0
        if self.effort and "[[noeffort]]" in trigger_text:  # a model that refuses effort levels
            raise LLMError("Mock API error 400: this model does not support the effort parameter")
        if not results:
            yield ("block", idx, {"type": "thinking", "thinking": "", "signature": ""})
            for c in self._chunks("Considering%s: " % (" (effort %s)" % self.effort if self.effort else "") + text):
                if self._sleep(delay):
                    return
                yield ("delta", idx, "thinking", c)
            if flaky and self.attempt < int(flaky.group(1)):
                raise LLMError("mock provider not responding (attempt %d)" % (self.attempt + 1), retryable=True)
            yield ("delta", idx, "signature", "mock-signature")
            idx += 1
            m = BASH_RE.search(trigger_text)
            calls = [("bash", {"command": m.group(1)})] if m else [
                ("spawn", {"description": "mock task", "prompt": p, "context": "fresh"}) for p in SUBAGENT_RE.findall(trigger_text)] + [
                ("spawn", {"description": "mock fork", "prompt": p, "context": "fork"}) for p in FORK_RE.findall(trigger_text)]
            if not calls:
                calls = [(n, json.loads(j)) for n, j in TOOL_RE.findall(trigger_text)]
            if calls:
                for name, inp in calls:
                    tid = "toolu_mock" + os.urandom(6).hex()
                    yield ("block", idx, {"type": "tool_use", "id": tid, "name": name})
                    for c in self._chunks(json.dumps(inp)):
                        if self._sleep(delay):
                            return
                        yield ("delta", idx, "partial_json", c)
                    idx += 1
                yield ("usage", self._usage(text, json.dumps([c[1] for c in calls])))
                yield ("stop", "tool_use")
                return
            reply = "**Mock reply** to: " + text
            if "[[history]]" in text:  # reports what the model was given (tests of /compact)
                first = next((b.get("text", "") for b in self.messages[0]["content"] if b.get("type") == "text"), "")
                reply = "History: %d messages; first: %s" % (len(self.messages), first[:80].replace("\n", " "))
        else:
            content = results[0].get("content")
            if isinstance(content, list):
                content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
            reply = "Tool said: " + str(content or "")[:200]
        yield ("block", idx, {"type": "text", "text": ""})
        for c in self._chunks(reply):
            if self._sleep(delay):
                return
            yield ("delta", idx, "text", c)
        yield ("usage", self._usage(text, reply))
        yield ("stop", "end_turn")

    def _usage(self, prompt, reply):
        """Deterministic fake usage; the mock "reports" a cost of $1 per million tokens."""
        u = {"input_tokens": len(prompt) // 4 + 10, "output_tokens": len(reply) // 4 + 1,
             "cache_read_tokens": 0, "cache_write_tokens": 0}
        u["cost"] = (u["input_tokens"] + u["output_tokens"]) / 1e6
        return u

    def _sleep(self, s):
        """Returns True when closed."""
        if self.closed:
            return True
        self._wake.wait(s)
        return self.closed


# --- entry points ------------------------------------------------------------------------------

def stream(provider, creds, model, system, messages, tools=None, max_tokens=16000, thinking_budget=0,
           timeout=None, idle_timeout=None, attempt=0, effort=None, adaptive=None):
    """One attempt. See retrying_stream() for the version that retries. effort: a level of EFFORT_LEVELS
    (None = send none); adaptive: whether the model supports adaptive thinking (None = unknown)."""
    t = provider["type"]
    if t == "mock":
        return MockStream(messages, attempt, effort)
    model = model or provider["model"]
    if t == "anthropic":
        return AnthropicStream(provider, creds, build_body(model, system, messages, tools, max_tokens,
                                                           thinking_budget, effort, adaptive), timeout, idle_timeout)
    if t == "openai":
        return OpenAIStream(provider, creds, build_openai_body(model, system, messages, tools, max_tokens, effort),
                            timeout, idle_timeout)
    raise LLMError("unknown provider type %r" % t)


def retrying_stream(settings, provider, creds, model, system, messages, tools=None, max_tokens=16000,
                    thinking_budget=0, effort=None, adaptive=None):
    """stream() wrapped in a RetryingStream using the timeout/retry settings."""
    retries, base, timeout, idle = retry_policy(settings)
    return RetryingStream(lambda attempt: stream(provider, creds, model, system, messages, tools, max_tokens,
                                                 thinking_budget, timeout, idle, attempt, effort, adaptive),
                          retries, base)


EFFORT_ERROR_RE = re.compile(r"effort|output_config|reasoning|adaptive", re.I)


def effort_rejected(error):
    """Whether a failed request looks like the provider refusing the effort parameter (a 400/422 naming
    it), so the request can be repeated without one."""
    msg = str(error or "")
    return (isinstance(error, LLMError) and not error.retryable and bool(re.search(r"error 4(00|22)\b", msg))
            and bool(EFFORT_ERROR_RE.search(msg)))


def clamp_effort(level, supported):
    """The level to send for a wanted level: itself when supported (or support is unknown: supported None),
    else the highest supported level below it, else the lowest supported one; None when there is none."""
    if not level:
        return None
    if supported is None:
        return level
    if not supported:
        return None
    if level in supported:
        return level
    rank = {x: i for i, x in enumerate(EFFORT_LEVELS)}
    want = rank.get(level, len(EFFORT_LEVELS))
    ordered = sorted(supported, key=lambda x: rank.get(x, len(EFFORT_LEVELS)))
    below = [x for x in ordered if rank.get(x, len(EFFORT_LEVELS)) <= want]
    return below[-1] if below else ordered[0]


def complete(provider, creds, prompt, model=None, max_tokens=1000, settings=None, usage=None):
    """One-shot, non-streaming text completion (titles), retried like streams. Mock: echoes the prompt's
    first line. `usage` (a dict) receives the response's normalize_usage() when the provider reports it."""
    t = provider["type"]
    if t == "mock":
        return prompt.strip().splitlines()[0] if prompt.strip() else ""
    retries, base, timeout, _ = retry_policy(settings or {})
    model = model or provider["cheap_model"] or provider["model"]
    body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}

    def once():
        try:
            if t == "openai":
                url = provider["base_url"].rstrip("/") + "/chat/completions"
                with _open(url, _openai_headers(provider, creds, False), body, "OpenAI-compatible", timeout,
                           timeout * 4) as r:
                    data = json.loads(r.read())
                if usage is not None:
                    usage.update(normalize_usage(data.get("usage"), "openai") or {})
                msg = ((data.get("choices") or [{}])[0].get("message") or {})
                return msg.get("content") or ""
            with _anthropic_post(provider, creds, body, False, timeout, timeout * 4) as r:
                data = json.loads(r.read())
            if usage is not None:
                usage.update(normalize_usage(data.get("usage"), "anthropic") or {})
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        except (TimeoutError, socket.timeout):
            raise LLMError("the provider stopped sending data", retryable=True) from None
    return call_with_retries(once, retries, base)


def find_model(models, model):
    """The entry of models ([{"id", ...}]) for a model id: exact, else with a provider prefix on either
    side ("anthropic/claude-x" ↔ "claude-x"), else None."""
    if not model:
        return None
    m = next((x for x in models if x.get("id") == model), None)
    if m is None:
        m = next((x for x in models if x.get("id") and (model.endswith("/" + x["id"]) or x["id"].endswith("/" + model))),
                 None)
    return m


def model_override(settings, provider_id, model):
    """The user's override for a model from the `model_overrides` setting: {provider id or "*": {model id:
    {"context_window": int, "efforts": [level, ...]}}}. The provider's own entry wins over "*"; ids match like
    find_model. Returns {"context_window"?: int, "efforts"?: [...]} (only valid values; efforts [] = the model
    takes no effort level) or {}."""
    table = settings.get("model_overrides")
    if not isinstance(table, dict) or not model:
        return {}
    for pid in (provider_id, "*"):
        per = table.get(pid) if pid else None
        if not isinstance(per, dict):
            continue
        entry = find_model([dict(v, id=k) for k, v in per.items() if isinstance(v, dict)], model)
        if not entry:
            continue
        out = {}
        try:
            cw = int(entry.get("context_window"))
            if cw > 0:
                out["context_window"] = cw
        except (TypeError, ValueError):
            pass
        eff = entry.get("efforts")
        if isinstance(eff, list):
            out["efforts"] = sort_efforts(str(x) for x in eff)
        if out:
            return out
    return {}


def sort_efforts(levels):
    """Unique effort levels, lowest first (unknown names last, in their order)."""
    rank = {x: i for i, x in enumerate(EFFORT_LEVELS)}
    out = []
    for x in levels:
        if x and x not in out:
            out.append(x)
    return sorted(out, key=lambda x: rank.get(x, len(EFFORT_LEVELS)))


def model_efforts(m):
    """(efforts, adaptive) a provider's model-list entry tells: Anthropic-style capabilities.effort /
    capabilities.thinking.types.adaptive, an explicit list of reasoning efforts, or OpenRouter-style
    supported_parameters naming reasoning. Either is None when the entry doesn't say."""
    caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
    efforts = adaptive = None
    eff = caps.get("effort")
    if isinstance(eff, dict):
        efforts = [] if eff.get("supported") is False else sort_efforts(
            k for k, v in eff.items() if isinstance(v, dict) and v.get("supported"))
    if efforts is None:
        for key in ("reasoning_efforts", "supported_reasoning_efforts", "reasoning_effort_levels", "efforts"):
            v = m.get(key) if isinstance(m.get(key), list) else caps.get(key)
            if isinstance(v, list):
                efforts = sort_efforts(str(x) for x in v)
                break
    if efforts is None and isinstance(m.get("supported_parameters"), list):
        params = m["supported_parameters"]
        efforts = list(DEFAULT_EFFORTS) if ("reasoning" in params or "reasoning_effort" in params) else []
    think = caps.get("thinking")
    if isinstance(think, dict):
        types = think.get("types") if isinstance(think.get("types"), dict) else {}
        ad = types.get("adaptive")
        adaptive = bool(isinstance(ad, dict) and ad.get("supported")) if think.get("supported") is not False else False
    return efforts, adaptive


def list_models(provider, creds, timeout=15):
    """Model ids the provider offers: GET {base}/models (OpenAI-compatible) or /v1/models (Anthropic).
    Returns [{"id", "name"?, "context_window"?, "efforts"?, "adaptive_thinking"?}], sorted by id. Mock: just "mock"."""
    t = provider["type"]
    if t == "mock":
        return [{"id": "mock", "context_window": MOCK_CONTEXT_WINDOW, "efforts": list(MOCK_EFFORTS)}]
    if t == "openai":
        if not provider.get("base_url"):
            raise LLMError("provider %s has no base_url" % provider.get("id"))
        url = provider["base_url"].rstrip("/") + "/models"
        headers = _openai_headers(provider, creds, False)
        what = "OpenAI-compatible"
    elif t == "anthropic":
        url = _anthropic_base(provider, creds) + "/v1/models?limit=1000"
        headers = _anthropic_headers(provider, creds, False)
        what = "Anthropic"
    else:
        raise LLMError("unknown provider type %r" % t)
    headers.pop("content-type", None)
    try:
        with _open(url, headers, None, what, timeout, timeout, method="GET") as r:
            data = json.loads(r.read())
    except (TimeoutError, socket.timeout):
        raise LLMError("%s API at %s did not answer within %ss" % (what, url, timeout), retryable=True) from None
    except ValueError:
        raise LLMError("%s API at %s returned something that is not JSON" % (what, url)) from None
    items = data.get("data") or data.get("models") or [] if isinstance(data, dict) else data
    out = {}
    for m in items if isinstance(items, list) else []:
        if isinstance(m, str):
            out[m] = {"id": m}
        elif isinstance(m, dict):
            mid = m.get("id") or m.get("name") or m.get("model")
            if mid:
                e = {"id": str(mid)}
                name = m.get("display_name") or (m.get("name") if m.get("name") != mid else None)
                if name:
                    e["name"] = str(name)
                top = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
                for cw in (m.get("context_window"), m.get("context_length"), m.get("max_context_length"),
                           m.get("max_input_tokens"), top.get("context_length")):
                    if _num(cw):
                        e["context_window"] = int(_num(cw))
                        break
                efforts, adaptive = model_efforts(m)
                if efforts is not None:
                    e["efforts"] = efforts
                if adaptive is not None:
                    e["adaptive_thinking"] = adaptive
                out[str(mid)] = e
    return [out[k] for k in sorted(out)]


# --- pricing (cost estimates when the provider doesn't report a cost) ---------------------------

PRICING_PATH = "/settings/api/pricing"
_PRICE_KEYS = {  # our key -> accepted spellings ($ per million tokens)
    "input": ("inputPerMTok", "input_per_mtok", "input"),
    "output": ("outputPerMTok", "output_per_mtok", "output"),
    "cache_read": ("cacheReadPerMTok", "cache_read_per_mtok", "cache_read"),
    "cache_write": ("cacheWritePerMTok", "cache_write_per_mtok", "cache_write"),
}


def pricing_urls(provider, creds):
    """Where to look for the provider's price list: its `pricing_url` if configured ("" = don't look),
    else GET {origin}/settings/api/pricing, then {base_url}/settings/api/pricing if that differs."""
    if provider.get("pricing_url") is not None:
        return [provider["pricing_url"]] if provider["pricing_url"] else []
    if provider["type"] == "anthropic":
        base = _anthropic_base(provider, creds)
    elif provider["type"] == "openai":
        base = (provider.get("base_url") or "").rstrip("/")
    else:
        return []
    if not base:
        return []
    p = urllib.parse.urlsplit(base)
    if not p.scheme or not p.netloc:
        return []
    out = ["%s://%s%s" % (p.scheme, p.netloc, PRICING_PATH)]
    if base + PRICING_PATH not in out:
        out.append(base + PRICING_PATH)
    return out


def _norm_price(p):
    """One model's entry -> {"input", "output", "cache_read", "cache_write"} with the fields present."""
    if not isinstance(p, dict):
        return {}
    out = {}
    for k, names in _PRICE_KEYS.items():
        for n in names:
            v = _num(p.get(n))
            if v is not None and v >= 0:
                out[k] = v
                break
    return out


def parse_pricing(data):
    """A pricing response -> {model id (lowercase): price}. Understands {"builtin": {model: {...PerMTok}},
    "overrides": {...}} (overrides win, field by field), a flat {model: {...}} map, and a list of
    {"model"|"id", ...}. Models without both an input and an output price are dropped."""
    tables = []
    if isinstance(data, dict):
        known = [k for k in ("builtin", "default", "defaults", "models", "pricing", "prices", "data", "overrides")
                 if isinstance(data.get(k), (dict, list))]
        tables = [data[k] for k in known] if known else [data]
    elif isinstance(data, list):
        tables = [data]
    out = {}
    for t in tables:
        items = t.items() if isinstance(t, dict) else (
            (e.get("model") or e.get("id"), e) for e in t if isinstance(e, dict))
        for mid, p in items:
            norm = _norm_price(p)
            if mid and norm:
                key = str(mid).lower()
                out[key] = dict(out.get(key) or {}, **norm)
    return {k: v for k, v in out.items() if "input" in v and "output" in v}


def fetch_pricing(provider, creds, timeout=5):
    """The provider's price list (parse_pricing format), or None if it has none / can't be reached."""
    for url in pricing_urls(provider, creds):
        try:
            headers = (_anthropic_headers if provider["type"] == "anthropic" else _openai_headers)(
                provider, creds, False)
        except LLMError:
            headers = {}
        headers.pop("content-type", None)
        try:
            with _open(url, headers, None, "pricing", timeout, timeout, method="GET") as r:
                table = parse_pricing(json.loads(r.read()))
        except Exception:
            continue
        if table:
            return table
    return None


def find_price(table, model):
    """The price for a model id: exact match, else without a "vendor/" prefix, a "[1m]"-style suffix or a
    "-YYYYMMDD" date suffix."""
    if not table or not model:
        return None
    m = str(model).lower()
    cands = [m]
    for x in list(cands):
        cands.append(x.rsplit("/", 1)[-1])
    for x in list(cands):
        cands.append(re.sub(r"\[[^\]]*\]$", "", x))
    for x in list(cands):
        cands.append(re.sub(r"-\d{8}$", "", x))
    return next((table[c] for c in cands if c in table), None)


def usage_cost(u, price):
    """USD for one response's usage at a price ($/M tokens); cache prices default to the input price."""
    inp = price["input"]
    return (int(u.get("input_tokens") or 0) * inp + int(u.get("output_tokens") or 0) * price["output"] +
            int(u.get("cache_read_tokens") or 0) * price.get("cache_read", inp) +
            int(u.get("cache_write_tokens") or 0) * price.get("cache_write", inp)) / 1e6
