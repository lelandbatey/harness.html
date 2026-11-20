"""The webfetch tool: fetch a URL (stdlib urllib) and convert HTML to markdown (stdlib html.parser)."""
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_BYTES = 5 * 1024 * 1024
MAX_OUTPUT = 100 * 1024
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 sfh-webfetch")

SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "nav", "footer", "iframe", "head", "form",
             "button", "select", "canvas", "object"}
VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
BLOCK_TAGS = {"p", "div", "section", "article", "main", "header", "aside", "figure", "figcaption", "dl", "dt",
              "dd", "address", "details", "summary", "center"}


class FetchError(Exception):
    pass


class _Converter(HTMLParser):
    def __init__(self, base_url=None):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.out = []          # finished markdown chunks
        self.skip = 0
        self.pre = 0
        self.lists = []        # stack of [kind, counter]
        self.links = []        # stack of (href, start index in out)
        self.quote = 0
        self.table = None      # list of rows, each a list of cell strings
        self.cell = None       # list of chunks of the current cell
        self.title = None
        self._in_title = False

    # output helpers
    def _emit(self, s):
        if self.cell is not None:
            self.cell.append(s)
        else:
            self.out.append(s)

    def _block(self, n=2):
        if self.cell is not None:
            self.cell.append(" ")
            return
        self.out.append("\n" * n)

    def _url(self, href):
        if not href:
            return ""
        return urllib.parse.urljoin(self.base, href) if self.base else href

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title" and self.title is None:
            self._in_title = True
        if tag in SKIP_TAGS or (a.get("aria-hidden") == "true") or "hidden" in a:
            if tag not in VOID_TAGS:
                self.skip += 1
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._block()
            self._emit("#" * int(tag[1]) + " ")
        elif tag in BLOCK_TAGS:
            self._block()
        elif tag == "br":
            self._emit("\n" if self.pre or self.cell is None else " ")
        elif tag == "hr":
            self._block()
            self._emit("---")
            self._block()
        elif tag == "pre":
            self._block()
            self._emit("```\n")
            self.pre += 1
        elif tag == "code" and not self.pre:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "blockquote":
            self._block()
            self.quote += 1
            self._emit("> ")
        elif tag in ("ul", "ol"):
            if not self.lists:
                self._block()
            self.lists.append([tag, 0])
        elif tag == "li":
            if self.cell is None:
                self.out.append("\n")
            depth = max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self._emit("   " * depth + "%d. " % self.lists[-1][1])
            else:
                self._emit("  " * depth + "- ")
        elif tag == "a":
            self.links.append((self._url(a.get("href")), len(self.cell if self.cell is not None else self.out)))
            self._emit("[")
        elif tag == "img":
            alt = (a.get("alt") or "").strip()
            if alt:
                self._emit("![%s](%s)" % (alt, self._url(a.get("src"))))
        elif tag == "table":
            self._block()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.table.append([])
        elif tag in ("td", "th") and self.table is not None:
            if not self.table:
                self.table.append([])
            self.cell = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIP_TAGS:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag) or tag in BLOCK_TAGS:
            self._block()
        elif tag == "pre":
            if self.pre:
                self.pre -= 1
            self._emit("\n```")
            self._block()
        elif tag == "code" and not self.pre:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "blockquote":
            self.quote = max(0, self.quote - 1)
            self._block()
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self._block()
        elif tag == "a" and self.links:
            href, _ = self.links.pop()
            if href and not href.startswith("javascript:"):
                self._emit("](%s)" % href)
            else:
                self._emit("]")
        elif tag in ("td", "th") and self.cell is not None and self.table is not None:
            text = re.sub(r"\s+", " ", "".join(self.cell)).strip().replace("|", "\\|")
            self.table[-1].append(text)
            self.cell = None
        elif tag == "table" and self.table is not None:
            rows = [r for r in self.table if r]
            self.table = None
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                self.out.append("\n".join(lines))
            self._block()

    def handle_data(self, data):
        if self._in_title:
            self.title = (self.title or "") + data
        if self.skip:
            return
        if self.pre:
            self._emit(data)
            return
        text = re.sub(r"\s+", " ", data)
        prev = self.cell if self.cell is not None else self.out
        if not prev or prev[-1].endswith(("\n", "- ", ". ", "> ", "# ")):
            text = text.lstrip()
        if self.quote and "\n" in data:
            text = text.replace("\n", "\n> ")
        self._emit(text)

    def result(self):
        text = "".join(self.out)
        # tidy: drop empty links, trailing spaces, collapse blank lines (outside code fences is close enough)
        text = re.sub(r"\[\s*\]\([^)]*\)", "", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(src, base_url=None):
    p = _Converter(base_url)
    p.feed(src)
    p.close()
    md = p.result()
    title = re.sub(r"\s+", " ", p.title or "").strip()
    if title and not md.startswith("# "):
        md = "# %s\n\n%s" % (title, md)
    return md


def html_to_text(src):
    md = html_to_markdown(src)
    md = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", md)
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)
    md = re.sub(r"^#+ |\*\*|```|`", "", md, flags=re.M)
    return md


def _charset(headers, raw):
    cs = headers.get_content_charset()
    if not cs:
        m = re.search(rb"""<meta[^>]+charset=["']?([\w-]+)""", raw[:4096], re.I)
        cs = m.group(1).decode("ascii", "replace") if m else "utf-8"
    try:
        return raw.decode(cs, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def cap(text, limit=MAX_OUTPUT):
    if len(text.encode("utf-8")) <= limit:
        return text
    cut = text.encode("utf-8")[:limit].decode("utf-8", "ignore")
    return cut + "\n\n[... truncated: output capped at %d KB of %d KB ...]" % (
        limit // 1024, len(text.encode("utf-8")) // 1024)


def fetch(url, fmt="markdown", timeout=30):
    """Returns the page as markdown/text/html. Raises FetchError."""
    url = str(url or "").strip()
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise FetchError("only http and https URLs are supported")
    fmt = fmt or "markdown"
    if fmt not in ("markdown", "text", "html"):
        raise FetchError("format must be markdown, text or html")
    try:
        timeout = min(120.0, max(1.0, float(timeout or 30)))
    except (TypeError, ValueError):
        timeout = 30.0
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,text/plain,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_BYTES + 1)
            ctype = (r.headers.get_content_type() or "").lower()
            final = r.geturl()
            headers = r.headers
    except urllib.error.HTTPError as e:
        raise FetchError("HTTP %d %s for %s" % (e.code, e.reason, url))
    except urllib.error.URLError as e:
        raise FetchError("fetch failed: %s" % e.reason)
    except (OSError, ValueError) as e:
        raise FetchError("fetch failed: %s" % e)
    note = ""
    if len(raw) > MAX_BYTES:
        raw = raw[:MAX_BYTES]
        note = "\n\n[... response truncated at %d MB ...]" % (MAX_BYTES // 1048576)
    is_html = ctype in ("text/html", "application/xhtml+xml") or (
        not ctype and re.search(rb"<html|<!doctype html", raw[:1024], re.I))
    textual = is_html or ctype.startswith("text/") or ctype in (
        "application/json", "application/xml", "application/javascript", "application/x-javascript",
        "application/ld+json", "application/rss+xml", "application/atom+xml", "image/svg+xml") \
        or ctype.endswith("+json") or ctype.endswith("+xml")
    if not textual or b"\0" in raw[:4096]:
        raise FetchError("%s is binary content (%s, %d bytes); not returned" % (final, ctype or "unknown", len(raw)))
    body = _charset(headers, raw)
    if is_html and fmt == "markdown":
        body = html_to_markdown(body, final)
    elif is_html and fmt == "text":
        body = html_to_text(body)
    head = "URL: %s\nContent-Type: %s\n\n" % (final, ctype or "unknown")
    return cap(head + body + note)

