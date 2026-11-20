// Unit tests for src/web/markdown.js: node --test test/markdown.test.mjs (also run by make test)
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

// loaded via vm (not require) so a stray package.json up the tree can't interfere
const load = (rel) => {
  const sandbox = { module: { exports: {} } };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(fs.readFileSync(new URL(rel, import.meta.url), "utf8"), sandbox);
  return sandbox.module.exports;
};
const MD = load("../src/web/markdown.js");
const r = (s, o) => MD.render(s, o);
const has = (html, frag) => assert.ok(html.includes(frag), `expected ${JSON.stringify(frag)} in:\n${html}`);
const lacks = (html, frag) => assert.ok(!html.includes(frag), `did not expect ${JSON.stringify(frag)} in:\n${html}`);

test("headings", () => {
  assert.equal(r("# Hi"), "<h1>Hi</h1>\n");
  assert.equal(r("### x ###"), "<h3>x</h3>\n");
  assert.equal(r("#nope"), "<p>#nope</p>\n");
  assert.equal(r("Title\n====="), "<h1>Title</h1>\n");
  assert.equal(r("Sub\n---"), "<h2>Sub</h2>\n");
});

test("paragraphs and breaks", () => {
  assert.equal(r("a\nb\n\nc"), "<p>a\nb</p>\n<p>c</p>\n");
  assert.equal(r("a  \nb"), "<p>a<br>\nb</p>\n");
  assert.equal(r("a\\\nb"), "<p>a<br>\nb</p>\n");
});

test("emphasis", () => {
  assert.equal(r("*a* **b** ***c*** ~~d~~ _e_"), "<p><em>a</em> <strong>b</strong> <em><strong>c</strong></em> <del>d</del> <em>e</em></p>\n");
  assert.equal(r("snake_case_word"), "<p>snake_case_word</p>\n");
  assert.equal(r("**unclosed"), "<p>**unclosed</p>\n");
  assert.equal(r("2 * 3 * 4"), "<p>2 * 3 * 4</p>\n");
  has(r("**bold with `code`**"), "<strong>bold with <code>code</code></strong>");
});

test("inline code", () => {
  assert.equal(r("`a <b>`"), "<p><code>a &lt;b&gt;</code></p>\n");
  assert.equal(r("`` a`b ``"), "<p><code>a`b</code></p>\n");
  assert.equal(r("`unclosed"), "<p>`unclosed</p>\n");
  assert.equal(r("`*no em*`"), "<p><code>*no em*</code></p>\n");
});

test("fenced and indented code", () => {
  assert.equal(r("```py\nx = 1\n```"), '<pre><code class="language-py" data-lang="py">x = 1\n</code></pre>\n');
  assert.equal(r("~~~\na\n~~~"), "<pre><code>a\n</code></pre>\n");
  has(r("```js\nlet a = '<x>';\n"), '<pre class="streaming"><code class="language-js" data-lang="js">let a = &#39;&lt;x&gt;&#39;;\n</code>');
  assert.equal(r("    code\n    more"), "<pre><code>code\nmore\n</code></pre>\n");
  assert.equal(r("````\n```\n````"), "<pre><code>```\n</code></pre>\n");
});

test("blockquotes", () => {
  assert.equal(r("> a\n> b"), "<blockquote>\n<p>a\nb</p>\n</blockquote>\n");
  has(r("> a\n>> b"), "<blockquote>\n<p>a</p>\n<blockquote>");
  assert.equal(r("> a\nlazy"), "<blockquote>\n<p>a\nlazy</p>\n</blockquote>\n");
});

test("lists", () => {
  assert.equal(r("- a\n- b"), "<ul>\n<li>a</li>\n<li>b</li>\n</ul>\n");
  assert.equal(r("3. a\n4. b"), '<ol start="3">\n<li>a</li>\n<li>b</li>\n</ol>\n');
  assert.equal(r("- a\n\n- b"), "<ul>\n<li><p>a</p>\n</li>\n<li><p>b</p>\n</li>\n</ul>\n");
  assert.equal(r("- a\n  - b\n- c"), "<ul>\n<li>a\n<ul>\n<li>b</li>\n</ul>\n</li>\n<li>c</li>\n</ul>\n");
  has(r("1. one\n   ```\n   code\n   ```\n2. two"), "<li>one\n<pre><code>code\n</code></pre>\n</li>");
  assert.equal(r("- a\n* b"), "<ul>\n<li>a</li>\n</ul>\n<ul>\n<li>b</li>\n</ul>\n");
  assert.equal(r("para\n- item"), "<p>para</p>\n<ul>\n<li>item</li>\n</ul>\n");
  assert.equal(r("- a\nlazy"), "<ul>\n<li>a\nlazy</li>\n</ul>\n");
});

test("task lists", () => {
  const h = r("- [ ] todo\n- [x] done");
  has(h, '<li class="task-list-item"><input type="checkbox" disabled> todo</li>');
  has(h, '<input type="checkbox" disabled checked> done');
});

test("thematic break", () => {
  assert.equal(r("***"), "<hr>\n");
  assert.equal(r("- - -"), "<hr>\n");
  assert.equal(r("a\n\n---"), "<p>a</p>\n<hr>\n");
});

test("tables", () => {
  const h = r("| a | b | c |\n|:--|:-:|--:|\n| 1 | **2** | 3 |\n| x |");
  has(h, '<th style="text-align:left">a</th><th style="text-align:center">b</th><th style="text-align:right">c</th>');
  has(h, '<td style="text-align:center"><strong>2</strong></td>');
  has(h, '<tr><td style="text-align:left">x</td><td style="text-align:center"></td>');
  has(r("a | b\n--|--\n1 | 2"), "<td>1</td><td>2</td>");
  has(r("| a \\| b | c |\n|---|---|"), "<th>a | b</th>");
  // streaming: half a delimiter row stays a paragraph, no crash
  assert.equal(r("| a | b |\n|--"), "<p>| a | b |\n|--</p>\n");
  has(r("text\n| a | b |\n|---|---|"), "<p>text</p>\n<table>");
});

test("links and images", () => {
  assert.equal(r('[t](http://x.y/a "T")'), '<p><a href="http://x.y/a" title="T" target="_blank" rel="noopener noreferrer">t</a></p>\n');
  has(r("![alt *x*](https://i/p.png)"), '<img src="https://i/p.png" alt="alt x"');
  has(r("<https://a.b/c>"), '<a href="https://a.b/c"');
  has(r("<me@x.org>"), '<a href="mailto:me@x.org">me@x.org</a>');
  has(r("see https://example.com/a_b_c."), '<a href="https://example.com/a_b_c" target="_blank" rel="noopener noreferrer">https://example.com/a_b_c</a>.');
  has(r("(www.commonmark.org/help)"), '<a href="http://www.commonmark.org/help"');
  has(r("[a](<b c>)"), 'href="b c"');
  has(r("[x](foo(bar))"), 'href="foo(bar)"');
  assert.equal(r("[not a link]"), "<p>[not a link]</p>\n");
  assert.equal(r("[a [b] c](u)").includes('href="u"'), true);
  lacks(r("[[nested](a)](b)"), 'href="b"');
});

test("reference links", () => {
  const h = r("[foo] and [bar][Foo] and [Foo][]\n\n[foo]: http://f.o \"Ti\"");
  assert.equal((h.match(/href="http:\/\/f.o"/g) || []).length, 3);
  lacks(h, "[foo]:");
  assert.equal(r("[x][missing]"), "<p>[x][missing]</p>\n");
});

test("escapes and entities", () => {
  assert.equal(r("\\*not\\* \\# x"), "<p>*not* # x</p>\n");
  assert.equal(r("&copy; &amp; &#169; & x"), "<p>&copy; &amp; &#169; &amp; x</p>\n");
});

test("raw html is escaped with html: false", () => {
  const h = r("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\nhi <b>x</b>", { html: false });
  lacks(h, "<script");
  lacks(h, "<img");
  lacks(h, "<b>");
  has(h, "&lt;script&gt;");
});

test("unsafe urls are dropped", () => {
  lacks(r("[x](javascript:alert(1))"), "href");
  lacks(r("[x](JaVa\tScRiPt:alert(1))"), "href");
  lacks(r("<javascript:alert(1)>"), "href");
  lacks(r("![x](data:text/html;base64,AAAA)"), "<img");
  has(r("![x](data:image/png;base64,AAAA)"), "<img");
  lacks(r("[x](vbscript:foo)"), "href");
  lacks(r('[x](http://a "b\\" onclick=\\"c")'), '" onclick');
});

test("local paths in links", () => {
  has(r("[f](/home/u/a.py)"), 'data-local-path="/home/u/a.py"');
  has(r("[f](file:///home/u/my%20file.txt)"), 'data-local-path="/home/u/my file.txt"');
  has(r("[f](src/a.js#L12)"), 'data-local-path="src/a.js" data-line="12"');
  has(r("[f](./x.md:7)"), 'data-local-path="./x.md" data-line="7"');
  lacks(r("[f](https://a.b/c)"), "data-local-path");
  lacks(r("[f](#anchor)"), "data-local-path");
});

test("local paths in inline code", () => {
  has(r("`/x/y.py`"), '<code class="local-path" data-local-path="/x/y.py">');
  has(r("`src/a.js:12`"), 'data-local-path="src/a.js" data-line="12"');
  has(r("`./foo`"), 'data-local-path="./foo"');
  has(r("`main.go:3:5`"), 'data-local-path="main.go" data-line="3"');
  has(r("`README.md`"), 'data-local-path="README.md"');
  for (const s of ["`x = 1`", "`and/or`", "`foo()`", "`https://a.b/c.py`", "`obj.method`", "`/`", "`1.5`"]) lacks(r(s), "data-local-path");
  lacks(r("`/x.py`", { localPaths: false }), "data-local-path");
});

test("resolved links: only paths that existed are clickable", () => {
  const links = { "src/a.js": { path: "/w/src/a.js", dir: false }, Makefile: { path: "/w/Makefile", dir: false }, docs: { path: "/w/docs", dir: true } };
  has(r("`src/a.js:12`", { links }), 'data-local-path="src/a.js" data-line="12" data-abs="/w/src/a.js"');
  has(r("`Makefile`", { links }), 'data-abs="/w/Makefile"'); // the loose rule: existence decides
  lacks(r("`src/missing.js`", { links }), "data-local-path");
  lacks(r("`/x/y.py`", { links: {} }), "local-path");
  has(r("[d](docs)", { links }), 'data-abs="/w/docs" data-dir="1"');
  const dead = r("[gone](src/gone.py)", { links });
  has(dead, 'class="local-missing"');
  lacks(dead, "href=");
  has(r("[web](https://a.b/c)", { links }), 'href="https://a.b/c"');
});

test("streaming tolerance: every prefix renders without throwing", () => {
  const doc = "# T\n\nSome *emph* and `code` [l](http://a) ![i](b)\n\n```js\nx\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n> q\n\n- [ ] t\n  1. n\n\n[r]: http://r\n";
  for (let i = 0; i <= doc.length; i++) assert.equal(typeof r(doc.slice(0, i)), "string");
});

test("performance: large doc renders quickly", () => {
  const chunk = "## Section\n\nText with **bold**, _em_, `code`, and a [link](http://x).\n\n- a\n- b\n\n```py\nprint(1)\n```\n\n";
  const big = chunk.repeat(400);
  const t = Date.now();
  r(big);
  assert.ok(Date.now() - t < 1000, "too slow");
  r("*".repeat(5000) + "a" + "_".repeat(5000));
});

// ---- raw HTML (sanitized pass-through) ------------------------------------------------------

test("inline html passes through, markdown still works around it", () => {
  assert.equal(r("hi <b>x</b> and *em* <kbd>Ctrl</kbd>"), "<p>hi <b>x</b> and <em>em</em> <kbd>Ctrl</kbd></p>\n");
  assert.equal(r("<b>*md inside*</b>"), "<p><b><em>md inside</em></b></p>\n");
  has(r("a<br>b<br/>c"), "a<br>b<br>c");
  has(r("H<sub>2</sub>O <mark>m</mark>"), "H<sub>2</sub>O <mark>m</mark>");
});

test("html inside code spans, fences and indented code stays text", () => {
  assert.equal(r("`<b>x</b>`"), "<p><code>&lt;b&gt;x&lt;/b&gt;</code></p>\n");
  has(r("```html\n<div onclick=x>a</div>\n```"), "&lt;div onclick=x&gt;a&lt;/div&gt;");
  has(r("    <b>code</b>"), "&lt;b&gt;code&lt;/b&gt;");
});

test("html blocks: type 6 ends at a blank line, markdown resumes", () => {
  assert.equal(r("<details>\n<summary>More</summary>\n\nSome **md**\n\n</details>\n\nafter"),
    "<details>\n<summary>More</summary>\n<p>Some <strong>md</strong></p>\n</details>\n<p>after</p>\n");
  has(r("para\n<div>interrupts</div>"), "<p>para</p>\n<div>interrupts</div>");
  has(r("<table><tr><td>1</td></tr></table>"), "<table><tr><td>1</td></tr></table>");
  assert.equal(r("<!-- a note\n\nstill comment -->\n\ntext"), "\n<p>text</p>\n");
  assert.equal(r("<https://a.b>"), '<p><a href="https://a.b" target="_blank" rel="noopener noreferrer">https://a.b</a></p>\n');
});

test("svg blocks run to their end tag (blank lines allowed) and are kept", () => {
  const h = r('<svg viewBox="0 0 10 10" width="100">\n  <defs><marker id="arr"><path d="M0,0 L1,1"/></marker></defs>\n\n  <line x1="0" marker-end="url(#arr)"/>\n  <use href="#arr"/>\n</svg>\ntext after');
  has(h, '<svg viewBox="0 0 10 10" width="100">');
  has(h, '<marker id="mdu-arr"><path d="M0,0 L1,1"/></marker>');
  has(h, '<line x1="0" marker-end="url(#mdu-arr)"/>');
  has(h, '<use href="#mdu-arr"/>');
  has(h, "</svg>\n<p>text after</p>");
  has(r("<svg><text>a</text></svg>"), "<svg><text>a</text></svg>");
});

test("sanitizer: dangerous tags, handlers and urls are removed", () => {
  const h = r('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n<iframe src="https://e"></iframe><object data=x></object><embed src=x>\n\nok');
  lacks(h, "<script"); lacks(h, "alert"); lacks(h, "onerror"); lacks(h, "<iframe"); lacks(h, "<object"); lacks(h, "<embed");
  has(h, '<img src="x" loading="lazy">');
  const s = (x) => MD.sanitizeHtml(x);
  assert.equal(s('<style>body{display:none}</style><b>x</b>'), "<b>x</b>");
  assert.equal(s('<form action="/x"><input name=a><button>b</button></form>'), "b");
  assert.equal(s('<base href="http://evil/"><meta http-equiv=refresh content=0><link rel=stylesheet href=x>t'), "t");
  assert.equal(s('<a href="javascript:alert(1)">j</a>'), "<a>j</a>");
  assert.equal(s('<a href="java&#115;cript:alert(1)">j</a>'), "<a>j</a>");
  assert.equal(s('<a href="javascript&colon;alert(1)">j</a><a href="java&Tab;script:x">k</a>'), "<a>j</a><a>k</a>");
  assert.equal(s('<a href="  JaVaScRiPt:x">j</a>'), "<a>j</a>");
  assert.equal(s('<img src="data:text/html,x"><img src="data:image/png;base64,AA">'), '<img loading="lazy"><img src="data:image/png;base64,AA" loading="lazy">');
  assert.equal(s('<svg><a xlink:href="javascript:alert(1)"><text>t</text></a></svg>'), "<svg><a><text>t</text></a></svg>");
  assert.equal(s('<svg><use href="https://evil/x.svg#a"/></svg>'), "<svg><use/></svg>");
  assert.equal(s('<svg><animate attributeName="href" to="javascript:alert(1)"/><set attributeName="onclick" to="x"/></svg>'), "<svg></svg>");
  assert.equal(s('<svg><foreignObject><div>x</div></foreignObject></svg>'), "<svg><div>x</div></svg>");
  assert.equal(s('<div onmouseover="x()" data-local-path="/etc/passwd" name="n" class="c" id="i">x</div>'), '<div class="c" id="mdu-i">x</div>');
  assert.equal(s('<p title="a&quot;b" style="color: red">x</p>'), '<p title="a&quot;b" style="color: red">x</p>');
  assert.equal(s('<!-- hidden --><!doctype html><?xml x?>t'), "t");
  assert.equal(s('a < b && c > d'), "a &lt; b &amp;&amp; c &gt; d");
  assert.equal(s('<textarea><b>x</b></textarea><select><option>o</select>y'), "y");
});

test("sanitizer: styles are filtered per declaration", () => {
  const s = (x) => MD.sanitizeHtml(x);
  assert.equal(s('<div style="position:fixed;top:0;color:red;background:url(http://x/y.png)">x</div>'), '<div style="top: 0; color: red">x</div>');
  assert.equal(s('<div style="position: relative; fill: url(#g)">x</div>'), '<div style="position: relative; fill: url(#mdu-g)">x</div>');
  assert.equal(s('<div style="width: expression(alert(1))">x</div>'), "<div>x</div>");
  assert.equal(s('<div style="background: u\\72l(x)">x</div>'), "<div>x</div>");
  assert.equal(s('<div style="position:absolute;inset:0">x</div>'), '<div style="inset: 0">x</div>');
});

test("sanitizer: output is balanced", () => {
  const s = (x) => MD.sanitizeHtml(x);
  assert.equal(s("x</div></p> y"), "x y");
  assert.equal(s("<b><i>z</b>"), "<b><i>z</i></b>");
  assert.equal(s("<div><span>open"), "<div><span>open</span></div>");
  assert.equal(r("one <b>bold\n\ntwo"), "<p>one <b>bold</b></p>\n<p>two</p>\n");
  assert.equal(r("<div>\nopen block"), "<div>\nopen block\n</div>\n");
  has(r("- <div>\n  item\n- next"), "<li><div>\nitem\n</div>\n</li>");
});

test("raw html links: local paths and in-page anchors", () => {
  has(r('<a href="https://x.y">l</a>'), '<a href="https://x.y" target="_blank" rel="noopener noreferrer">l</a>');
  has(r('<a href="/home/u/a.py">p</a>'), 'data-local-path="/home/u/a.py"');
  has(r('<a href="#sec">s</a> <h3 id="sec">S</h3>'), '<a href="#mdu-sec">s</a> <h3 id="mdu-sec">S</h3>');
  lacks(r('[outer <a href="https://b">inner</a>](https://a)'), 'href="https://b"');
});

test("streaming: half-received tags and blocks are held back", () => {
  const st = (x) => r(x, { streaming: true });
  assert.equal(st("a <span sty"), "<p>a </p>\n");
  assert.equal(r("a <span sty"), "<p>a &lt;span sty</p>\n"); // not a tag once the message is complete
  assert.equal(st("x <"), "<p>x </p>\n");
  assert.equal(st("a < b"), "<p>a &lt; b</p>\n");
  assert.equal(st('<svg viewBox="0 0 1 1">\n<rect/>'), '<div class="md-pending">rendering svg…</div>\n');
  has(r('<svg viewBox="0 0 1 1">\n<rect/>'), "<svg"); // final text: render what's there
  assert.equal(st("<div>\n<p>hello <b"), "<div>\n<p>hello \n</p></div>\n");
  assert.equal(st("first <b\n\nsecond"), "<p>first &lt;b</p>\n<p>second</p>\n"); // only the tail is held
  // every prefix of a document full of html renders, and never shows a tag as text
  const doc = 'Intro <b>bold</b> text\n\n<div align="center">\n<img src="https://i/x.png" alt="x">\n</div>\n\n<svg width="10">\n<rect x="1"/>\n</svg>\n\n<details><summary>s</summary>\n\nbody\n\n</details>\n';
  for (let i = 0; i <= doc.length; i++) {
    const h = st(doc.slice(0, i));
    assert.equal(typeof h, "string");
    assert.ok(!/&lt;\/?[a-z]/.test(h), `tag shown as text at ${i}: ${h}`);
  }
});

test("artifacts: fence renders a sandbox mount point with source", () => {
  const h = r('```artifact title="My <demo>"\n<!doctype html>\n<script>go()</script>\n```\n\nafter');
  has(h, '<div class="artifact" data-title="My &lt;demo&gt;">');
  has(h, '<span class="artifact-title">My &lt;demo&gt;</span>');
  has(h, '<pre class="artifact-src" hidden><code class="language-html" data-lang="html">&lt;!doctype html&gt;\n&lt;script&gt;go()&lt;/script&gt;\n</code></pre>');
  has(h, '<div class="artifact-frame"></div>');
  for (const act of ["source", "full", "open", "download"]) has(h, `data-artifact-act="${act}"`);
  has(h, "<p>after</p>");
  lacks(h, "<script");
  // while streaming: a placeholder until the fence closes; then the full artifact
  const p = r("```artifact\n<p>a</p>\n<p>b", { streaming: true });
  has(p, '<div class="artifact pending">');
  has(p, "building artifact… 2 lines");
  lacks(p, "artifact-frame");
  has(r("```artifact\n<p>a</p>\n```", { streaming: true }), "artifact-frame");
  has(r("```artifact\n<p>a</p>"), "artifact-frame"); // a stored message with an unclosed fence still shows it
  has(r("~~~~artifact\n```\nnested\n```\n~~~~"), "```\nnested\n```");
  lacks(r("```artifacts\nx\n```"), "artifact-frame");
});

test("artifacts: titles and srcdoc", () => {
  assert.equal(MD.artifactTitle('artifact title="A b"'), "A b");
  assert.equal(MD.artifactTitle("artifact title='Q'"), "Q");
  assert.equal(MD.artifactTitle("artifact title=One two"), "One");
  assert.equal(MD.artifactTitle("artifact Some title"), "Some title");
  assert.equal(MD.artifactTitle("artifact"), "Artifact");
  const d = MD.artifactDoc("<!DOCTYPE html><html><body>x</body></html>");
  assert.ok(d.startsWith('<!DOCTYPE html><meta http-equiv="Content-Security-Policy"'), d);
  has(d, "connect-src https:");
  has(d, "sfhArtifactHeight");
  const plain = MD.artifactDoc("<p>x</p>", { autoHeight: false });
  assert.ok(plain.startsWith("<meta http-equiv"));
  lacks(plain, "sfhArtifactHeight");
});
