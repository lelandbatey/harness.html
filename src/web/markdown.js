// SFHMarkdown: a small streaming-friendly GFM renderer.
// render(text, opts) -> html string. Raw HTML in the input is always escaped (LLM output is untrusted).
// Tolerates incomplete input (unclosed fences, half tables, unclosed emphasis) so it can re-render a
// growing draft every animation frame.
(function (root) {
  "use strict";

  var ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return ESC[c]; }); }

  var RE_FENCE = /^( {0,3})(`{3,}|~{3,})(.*)$/;
  var RE_FENCE_CLOSE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;
  var RE_ATX = /^ {0,3}(#{1,6})(?=[ \t]|$)(.*)$/;
  var RE_HR = /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$/;
  var RE_QUOTE = /^ {0,3}>/;
  var RE_LIST = /^( {0,3})([*+-]|(\d{1,9})([.)]))([ \t]+|$)(.*)$/;
  var RE_SETEXT = /^ {0,3}(=+|-+)[ \t]*$/;
  var RE_BLANK = /^[ \t]*$/;
  var RE_DELIM_ROW = /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;
  var RE_REFDEF = /^ {0,3}\[((?:[^\\\[\]]|\\.){1,999})\]:[ \t]*(<[^<>\n]*>|\S+)(?:[ \t]+("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\((?:[^()\\]|\\.)*\)))?[ \t]*$/;
  var RE_PUNCT = /[!-\/:-@\[-`{-~]/;
  var RE_UPUNCT;
  try { RE_UPUNCT = new RegExp("[!-\\/:-@\\[-`{-~\\p{P}\\p{S}]", "u"); } catch (e) { RE_UPUNCT = RE_PUNCT; }
  var RE_ENT = /&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});/y;
  var RE_AUTOLINK = /<([A-Za-z][A-Za-z0-9+.\-]{1,31}:[^\s<>]*)>/y;
  var RE_EMAIL = /<([A-Za-z0-9.!#$%&'*+\/=?^_`{|}~\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*)>/y;
  var RE_BARE = /(?:https?:\/\/[^\s<]+|www\.[^\s<]+)/iy;

  // ---- raw HTML (CommonMark-style recognition; output goes through sanitizeHtml) ------------
  var H_ATTR = "(?:\\s+[A-Za-z_:][A-Za-z0-9_.:-]*(?:\\s*=\\s*(?:[^\\s\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)";
  var H_OPEN = "<[A-Za-z][A-Za-z0-9-]*" + H_ATTR + "*\\s*\\/?>";
  var H_CLOSE = "<\\/[A-Za-z][A-Za-z0-9-]*\\s*>";
  var CMT = "<" + "!--"; // (split: the build refuses that sequence inside inline scripts)
  var RE_HTML_INLINE = new RegExp("(?:" + H_OPEN + "|" + H_CLOSE + "|" + CMT + "[\\s\\S]*?-->)", "y");
  var RE_HTML_LINE7 = new RegExp("^ {0,3}(?:" + H_OPEN + "|" + H_CLOSE + ")[ \\t]*$");
  var RE_HTML_START = /^ {0,3}<(\/?)([A-Za-z][A-Za-z0-9-]*)(?=[\s\/>]|$)/;
  // a tag that has started arriving but is not complete yet (only held back while streaming)
  var RE_PARTIAL_TAG = /<(?:\/?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?|!-{0,2}[^>]*)?$/y;
  var RE_PARTIAL_TAG_END = /<(?:\/?[A-Za-z][^<>]*|!-{0,2}[^>]*)?$/;
  function wordSet(s) { var o = {}; s.split(" ").forEach(function (w) { o[w] = true; }); return o; }
  // CommonMark HTML block "type 6" names: the block runs until a blank line
  var HTML_BLOCK6 = wordSet("address article aside base basefont blockquote body caption center col colgroup dd details " +
    "dialog dir div dl dt fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hr html " +
    "iframe legend li link main menu menuitem nav noframes ol optgroup option p param search section summary table " +
    "tbody td tfoot th thead title tr track ul");
  // blocks that run until their end tag (blank lines allowed inside: diagrams often have them)
  var HTML_RAW = wordSet("script pre style textarea svg");

  // sanitizer allowlists (lowercase); everything else is unwrapped (tag dropped, content kept) …
  var SAFE_TAGS = wordSet("a abbr address article aside b bdi bdo big blockquote br caption center cite code col colgroup " +
    "dd del details dfn div dl dt em figcaption figure font footer h1 h2 h3 h4 h5 h6 header hr i img ins kbd li main " +
    "mark meter nav ol p pre progress q rp rt ruby s samp section small span strike strong sub summary sup table tbody " +
    "td tfoot th thead time tr tt u ul var wbr " +
    // inline SVG
    "svg g path rect circle ellipse line polyline polygon text tspan textpath defs marker lineargradient radialgradient " +
    "stop clippath mask pattern symbol use title desc image filter fedropshadow fegaussianblur feoffset feblend " +
    "fecolormatrix femerge femergenode feflood fecomposite animate animatetransform animatemotion mpath");
  // … except these, which are dropped together with their content
  var DROP_TAGS = wordSet("script style iframe object embed frame frameset applet noscript noembed noframes template " +
    "textarea xmp plaintext select");
  var VOID_TAGS = wordSet("br hr img wbr col");
  var SAFE_ATTRS = wordSet("class style title lang dir role hidden id href src alt width height align valign colspan " +
    "rowspan scope span start reversed type value min max low high optimum open datetime border cellpadding " +
    "cellspacing bgcolor color face size abbr headers loading decoding " +
    "viewbox xmlns xmlns:xlink version preserveaspectratio x y x1 y1 x2 y2 cx cy r rx ry d points fill fill-opacity " +
    "fill-rule stroke stroke-width stroke-dasharray stroke-dashoffset stroke-linecap stroke-linejoin stroke-opacity " +
    "stroke-miterlimit opacity transform transform-origin font-family font-size font-weight font-style text-anchor " +
    "dominant-baseline alignment-baseline baseline-shift letter-spacing word-spacing text-decoration dx dy rotate " +
    "textlength lengthadjust marker-start marker-mid marker-end markerwidth markerheight markerunits refx refy orient " +
    "offset stop-color stop-opacity gradientunits gradienttransform spreadmethod fx fy fr clip-path clip-rule mask " +
    "patternunits patterncontentunits patterntransform xlink:href pathlength visibility display vector-effect " +
    "paint-order filter stddeviation in in2 result mode values operator k1 k2 k3 k4 flood-color flood-opacity dur " +
    "repeatcount repeatdur attributename attributetype from to by begin end keytimes keysplines calcmode additive " +
    "accumulate path keypoints maskunits maskcontentunits clippathunits filterunits primitiveunits pointer-events " +
    "shape-rendering text-rendering overflow writing-mode startoffset method spacing side");
  var ID_PREFIX = "mdu-"; // raw-HTML ids are namespaced so they can't collide with the app's own ids

  function isBlank(l) { return RE_BLANK.test(l); }
  function leading(l) { var n = 0; while (l.charCodeAt(n) === 32) n++; return n; }
  function expandTabs(line) {
    if (line.indexOf("\t") < 0) return line;
    var out = "", i = 0;
    for (; i < line.length; i++) {
      var c = line[i];
      if (c === "\t") out += "    ".slice(out.length % 4);
      else if (c === " ") out += c;
      else break;
    }
    return out + line.slice(i);
  }
  function stripIndent(l, n) { var k = 0; while (k < n && l[k] === " ") k++; return l.slice(k); }

  function listMarker(line) {
    var m = RE_LIST.exec(line);
    if (!m) return null;
    var indent = m[1].length, marker = m[2], sp = m[5].length, rest = m[6];
    var ordered = !!m[3];
    var ci, first;
    if (rest === "") { ci = indent + marker.length + 1; first = ""; }
    else if (sp > 4) { ci = indent + marker.length + 1; first = new Array(sp).join(" ") + rest; }
    else { ci = indent + marker.length + sp; first = rest; }
    return { ordered: ordered, bullet: ordered ? m[4] : marker, start: ordered ? parseInt(m[3], 10) : null,
             ci: ci, first: first, empty: rest === "" };
  }

  function isFenceStart(l) {
    var m = RE_FENCE.exec(l);
    return !!m && !(m[2][0] === "`" && m[3].indexOf("`") >= 0);
  }

  // Does an HTML block start on this line? -> {blank: true} (ends at a blank line), {endTag: name} or
  // {comment: true}; null if not. inPara: "type 7" blocks (any lone complete tag) can't interrupt a paragraph.
  var htmlOn = true; // render(text, {html: false}) turns raw HTML recognition off (it is escaped instead)
  function htmlBlockKind(l, inPara) {
    if (!htmlOn) return null;
    if (/^ {0,3}</.test(l) && l.trim().slice(0, 4) === CMT) return { comment: true };
    var m = RE_HTML_START.exec(l);
    if (m) {
      var name = m[2].toLowerCase();
      if (!m[1] && HTML_RAW[name]) return { endTag: name };
      if (HTML_BLOCK6[name]) return { blank: true };
    }
    if (!inPara && RE_HTML_LINE7.test(l)) return { blank: true };
    return null;
  }

  // Can this line interrupt a paragraph?
  function startsBlock(l) {
    if (isFenceStart(l) || RE_ATX.test(l) || RE_HR.test(l) || RE_QUOTE.test(l) || htmlBlockKind(l, true)) return true;
    var mk = listMarker(l);
    return !!mk && !mk.empty && (!mk.ordered || mk.start === 1);
  }

  function splitRow(line) {
    var s = line.trim();
    if (s[0] === "|") s = s.slice(1);
    if (s.length && s[s.length - 1] === "|" && s[s.length - 2] !== "\\") s = s.slice(0, -1);
    var cells = [], cur = "";
    for (var i = 0; i < s.length; i++) {
      var c = s[i];
      if (c === "\\" && s[i + 1] === "|") { cur += "|"; i++; continue; }
      if (c === "|") { cells.push(cur.trim()); cur = ""; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }
  function tableAt(lines, i) {
    if (i + 1 >= lines.length) return null;
    var head = lines[i], delim = lines[i + 1];
    if (head.indexOf("|") < 0 || !RE_DELIM_ROW.test(delim) || leading(head) > 3) return null;
    var cells = splitRow(head), al = splitRow(delim);
    if (cells.length !== al.length) return null;
    if (delim.indexOf("|") < 0 && cells.length < 2) return null;
    return {
      head: cells,
      align: al.map(function (a) {
        var l = a[0] === ":", r = a[a.length - 1] === ":";
        return l && r ? "center" : r ? "right" : l ? "left" : "";
      })
    };
  }

  function normLabel(s) { return s.trim().replace(/\s+/g, " ").toLowerCase().toUpperCase(); }
  function unescapeStr(s) { return s.replace(/\\([!-\/:-@\[-`{-~])/g, "$1"); }

  // ---- block parsing -------------------------------------------------------------------------

  function parseBlocks(lines, ctx) {
    var out = [], i = 0, sawBlank = false;
    function push(node) { node.blankBefore = sawBlank && out.length > 0; sawBlank = false; out.push(node); }
    while (i < lines.length) {
      var line = lines[i], m, t;
      if (isBlank(line)) { sawBlank = true; i++; continue; }

      if ((m = RE_FENCE.exec(line)) && !(m[2][0] === "`" && m[3].indexOf("`") >= 0)) {
        var indent = m[1].length, fch = m[2][0], flen = m[2].length, info = unescapeStr(m[3].trim());
        var body = [], closed = false;
        i++;
        while (i < lines.length) {
          var cm = RE_FENCE_CLOSE.exec(lines[i]);
          if (cm && cm[1][0] === fch && cm[1].length >= flen) { i++; closed = true; break; }
          body.push(stripIndent(lines[i], indent));
          i++;
        }
        if (!closed) while (body.length && isBlank(body[body.length - 1])) body.pop();
        push({ t: "code", info: info, text: body.join("\n"), open: !closed });
        continue;
      }
      if ((m = RE_ATX.exec(line))) {
        push({ t: "h", level: m[1].length, text: m[2].replace(/(?:^|[ \t]+)#+[ \t]*$/, "").trim() });
        i++;
        continue;
      }
      if (RE_HR.test(line)) { push({ t: "hr" }); i++; continue; }
      if (leading(line) >= 4) {
        var code = [];
        while (i < lines.length && (leading(lines[i]) >= 4 || isBlank(lines[i]))) {
          code.push(lines[i].slice(Math.min(4, leading(lines[i]))));
          i++;
        }
        while (code.length && isBlank(code[code.length - 1])) code.pop();
        push({ t: "code", info: "", text: code.join("\n"), open: false });
        continue;
      }
      var hk = htmlBlockKind(line, false);
      if (hk) {
        var hl = [], hclosed = false;
        if (hk.blank) {
          while (i < lines.length && !isBlank(lines[i])) hl.push(lines[i++]);
          hclosed = i < lines.length;
        } else {
          var depth = 0, closes = 0;
          var reO = hk.endTag ? new RegExp("<" + hk.endTag + "(?=[\\s\\/>]|$)", "gi") : null;
          var reC = hk.endTag ? new RegExp("<\\/" + hk.endTag + "\\s*>", "gi") : null;
          while (i < lines.length) {
            var hline = lines[i];
            hl.push(hline);
            i++;
            if (hk.endTag) {
              var nc = (hline.match(reC) || []).length;
              depth += (hline.match(reO) || []).length - nc;
              closes += nc;
              if (closes && depth <= 0) { hclosed = true; break; }
            } else if (hline.indexOf("-->", hl.length === 1 ? hline.indexOf(CMT) + 4 : 0) >= 0) { hclosed = true; break; }
          }
        }
        push({ t: "html", text: hl.join("\n"), open: !hclosed, raw: !hk.blank, tag: hk.endTag || "" });
        continue;
      }
      if (RE_QUOTE.test(line)) {
        var inner = [], inFence = false;
        while (i < lines.length) {
          var l = lines[i];
          if (RE_QUOTE.test(l)) {
            var stripped = l.replace(/^ {0,3}> ?/, "");
            if (isFenceStart(stripped)) inFence = !inFence;
            inner.push(stripped);
            i++;
          } else if (!isBlank(l) && inner.length && !isBlank(inner[inner.length - 1]) && !inFence && !startsBlock(l)) {
            inner.push(l); // lazy continuation
            i++;
          } else break;
        }
        push({ t: "quote", kids: parseBlocks(inner, ctx) });
        continue;
      }
      if (listMarker(line)) {
        var res = parseList(lines, i, ctx);
        push(res.node);
        i = res.end;
        continue;
      }
      if ((t = tableAt(lines, i))) {
        var rows = [];
        i += 2;
        while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines[i])) {
          var cells = splitRow(lines[i]);
          var row = [];
          for (var c = 0; c < t.head.length; c++) row.push(cells[c] || "");
          rows.push(row);
          i++;
        }
        push({ t: "table", head: t.head, align: t.align, rows: rows });
        continue;
      }

      // paragraph (possibly setext heading), with leading link reference definitions
      var para = [line.replace(/^[ \t]+/, "")], setext = 0;
      i++;
      while (i < lines.length) {
        var nl = lines[i];
        if (isBlank(nl)) break;
        if ((m = RE_SETEXT.exec(nl))) {
          // a lone "===" line after reference definitions only is not a heading; good enough here
          setext = m[1][0] === "=" ? 1 : 2;
          i++;
          break;
        }
        if (startsBlock(nl) || tableAt(lines, i)) break;
        para.push(nl.replace(/^[ \t]+/, ""));
        i++;
      }
      while (para.length && (m = RE_REFDEF.exec(para[0]))) {
        var key = normLabel(m[1]);
        if (key && !(key in ctx.refs)) {
          var dest = m[2][0] === "<" ? m[2].slice(1, -1) : m[2];
          ctx.refs[key] = { dest: unescapeStr(dest), title: m[3] ? unescapeStr(m[3].slice(1, -1)) : null };
        }
        para.shift();
      }
      if (!para.length) { if (setext === 2) push({ t: "hr" }); continue; }
      var txt = para.join("\n").replace(/[ \t]+$/, "");
      if (setext) push({ t: "h", level: setext, text: txt });
      else push({ t: "p", text: txt });
    }
    return out;
  }

  function parseList(lines, i, ctx) {
    var first = listMarker(lines[i]);
    var node = { t: "list", ordered: first.ordered, start: first.start, loose: false, items: [] };
    while (i < lines.length) {
      var mk = listMarker(lines[i]);
      if (!mk || mk.ordered !== first.ordered || mk.bullet !== first.bullet || RE_HR.test(lines[i])) break;
      var itemLines = [mk.first], blanks = 0, inFence = isFenceStart(mk.first);
      i++;
      while (i < lines.length) {
        var l = lines[i];
        if (isBlank(l)) {
          // an empty first line followed by a blank ends the item
          if (itemLines.length === 1 && itemLines[0] === "") break;
          itemLines.push(""); blanks++; i++;
          continue;
        }
        if (leading(l) >= mk.ci) {
          var s = l.slice(mk.ci);
          if (isFenceStart(s) || RE_FENCE_CLOSE.test(s)) inFence = !inFence;
          itemLines.push(s); blanks = 0; i++;
          continue;
        }
        if (blanks === 0 && !inFence && !startsBlock(l) && !listMarker(l) && !isBlank(itemLines[itemLines.length - 1])) {
          itemLines.push(l.replace(/^[ \t]+/, "")); i++; // lazy continuation
          continue;
        }
        break;
      }
      var trailing = 0;
      while (itemLines.length && isBlank(itemLines[itemLines.length - 1])) { itemLines.pop(); trailing++; }
      var kids = parseBlocks(itemLines, ctx);
      for (var k = 1; k < kids.length; k++) if (kids[k].blankBefore) node.loose = true;
      var task = null;
      if (kids.length && kids[0].t === "p") {
        var tm = /^\[([ xX])\](?:[ \t]+|$)/.exec(kids[0].text);
        if (tm) { task = tm[1] !== " "; kids[0].text = kids[0].text.slice(tm[0].length); }
      }
      node.items.push({ kids: kids, task: task });
      if (trailing && i < lines.length) {
        var nm = listMarker(lines[i]);
        if (nm && nm.ordered === first.ordered && nm.bullet === first.bullet) node.loose = true;
        else { i -= trailing; break; }
      }
    }
    return { node: node, end: i };
  }

  // ---- inline parsing ------------------------------------------------------------------------

  function isWs(c) { return c === undefined || /\s/.test(c); }
  function isPunct(c) { return c !== undefined && RE_UPUNCT.test(c); }

  function parseLinkTail(src, p) {
    var n = src.length, dest, title = null, j;
    while (p < n && /[ \t\n]/.test(src[p])) p++;
    if (src[p] === "<") {
      j = p + 1;
      while (j < n && src[j] !== ">" && src[j] !== "\n" && src[j] !== "<") { if (src[j] === "\\") j++; j++; }
      if (src[j] !== ">") return null;
      dest = src.slice(p + 1, j);
      p = j + 1;
    } else {
      var depth = 0;
      j = p;
      while (j < n) {
        var c = src[j];
        if (c === "\\" && j + 1 < n && RE_PUNCT.test(src[j + 1])) { j += 2; continue; }
        if (c === "(") depth++;
        else if (c === ")") { if (depth === 0) break; depth--; }
        else if (/[\s\x00-\x1f]/.test(c)) break;
        j++;
      }
      dest = src.slice(p, j);
      p = j;
    }
    var p2 = p;
    while (p2 < n && /[ \t\n]/.test(src[p2])) p2++;
    var q = src[p2];
    if (p2 > p && (q === '"' || q === "'" || q === "(")) {
      var close = q === "(" ? ")" : q;
      j = p2 + 1;
      while (j < n && src[j] !== close) { if (src[j] === "\\") j++; j++; }
      if (j >= n) return null;
      title = src.slice(p2 + 1, j);
      p = j + 1;
      while (p < n && /[ \t\n]/.test(src[p])) p++;
    } else p = p2;
    if (src[p] !== ")") return null;
    return { dest: unescapeStr(dest), title: title === null ? null : unescapeStr(title), end: p + 1 };
  }

  function parseInline(src, ctx) {
    var nodes = [], brackets = [], text = "", i = 0, n = src.length, m;
    function flush() { if (text) { nodes.push({ t: "text", v: text }); text = ""; } }
    while (i < n) {
      var c = src[i];
      if (c === "\\") {
        var nx = src[i + 1];
        if (nx === "\n") { flush(); nodes.push({ t: "br" }); i += 2; while (src[i] === " ") i++; continue; }
        if (nx !== undefined && RE_PUNCT.test(nx)) { text += nx; i += 2; continue; }
        text += "\\"; i++;
        continue;
      }
      if (c === "`") {
        var j = i;
        while (src[j] === "`") j++;
        var len = j - i, k = j, found = -1;
        while (k < n) {
          var s = src.indexOf("`", k);
          if (s < 0) break;
          var e = s;
          while (src[e] === "`") e++;
          if (e - s === len) { found = s; break; }
          k = e;
        }
        if (found < 0) { text += src.slice(i, j); i = j; continue; }
        var code = src.slice(j, found).replace(/\n/g, " ");
        if (code.length > 2 && code[0] === " " && code[code.length - 1] === " " && /[^ ]/.test(code)) code = code.slice(1, -1);
        flush();
        nodes.push({ t: "code", v: code });
        i = found + len;
        continue;
      }
      if (c === "*" || c === "_" || c === "~") {
        var r = i;
        while (src[r] === c) r++;
        var before = src[i - 1], after = src[r];
        var lf = !isWs(after) && (!isPunct(after) || isWs(before) || isPunct(before));
        var rf = !isWs(before) && (!isPunct(before) || isWs(after) || isPunct(after));
        var open, cls;
        if (c === "_") { open = lf && (!rf || isPunct(before)); cls = rf && (!lf || isPunct(after)); }
        else { open = lf; cls = rf; }
        flush();
        nodes.push({ t: "delim", ch: c, n: r - i, orig: r - i, open: open, close: cls });
        i = r;
        continue;
      }
      if (c === "!" && src[i + 1] === "[") {
        flush();
        nodes.push({ t: "text", v: "![" });
        brackets.push({ idx: nodes.length - 1, image: true, pos: i + 2, active: true });
        i += 2;
        continue;
      }
      if (c === "[") {
        flush();
        nodes.push({ t: "text", v: "[" });
        brackets.push({ idx: nodes.length - 1, image: false, pos: i + 1, active: true });
        i++;
        continue;
      }
      if (c === "]") {
        var op = brackets[brackets.length - 1];
        if (!op || !op.active) { if (op) brackets.pop(); text += "]"; i++; continue; }
        var inner = src.slice(op.pos, i), dest = null, title = null, end = i + 1, lt;
        if (src[i + 1] === "(" && (lt = parseLinkTail(src, i + 2))) { dest = lt.dest; title = lt.title; end = lt.end; }
        if (dest === null) {
          var label = inner, lend = i + 1;
          if (src[i + 1] === "[") {
            var ce = src.indexOf("]", i + 2);
            if (ce >= 0 && src.slice(i + 2, ce).indexOf("[") < 0) {
              var lab = src.slice(i + 2, ce);
              if (lab.trim()) label = lab;
              lend = ce + 1;
            }
          }
          var ref = ctx.refs[normLabel(label)];
          if (ref && label.trim()) { dest = ref.dest; title = ref.title; end = lend; }
        }
        if (dest === null) { brackets.pop(); text += "]"; i++; continue; }
        flush();
        var kids = nodes.splice(op.idx + 1);
        nodes.pop();
        processEmphasis(kids);
        nodes.push({ t: op.image ? "img" : "link", dest: dest, title: title, kids: kids });
        brackets.pop();
        if (!op.image) for (var b = 0; b < brackets.length; b++) if (!brackets[b].image) brackets[b].active = false;
        i = end;
        continue;
      }
      if (c === "<") {
        RE_AUTOLINK.lastIndex = i;
        if ((m = RE_AUTOLINK.exec(src))) { flush(); nodes.push({ t: "autolink", href: m[1], v: m[1] }); i += m[0].length; continue; }
        RE_EMAIL.lastIndex = i;
        if ((m = RE_EMAIL.exec(src))) { flush(); nodes.push({ t: "autolink", href: "mailto:" + m[1], v: m[1] }); i += m[0].length; continue; }
        if (htmlOn) {
          RE_HTML_INLINE.lastIndex = i;
          if ((m = RE_HTML_INLINE.exec(src))) { flush(); nodes.push({ t: "html", v: m[0] }); i += m[0].length; continue; }
          // streaming: the tail of the last block may be a tag still arriving; don't flash it as text
          RE_PARTIAL_TAG.lastIndex = i;
          if (ctx.holdTail && n - i <= 400 && RE_PARTIAL_TAG.test(src)) { i = n; continue; }
        }
        text += "<"; i++;
        continue;
      }
      if (c === "&") {
        RE_ENT.lastIndex = i;
        if ((m = RE_ENT.exec(src))) { flush(); nodes.push({ t: "raw", v: m[0] }); i += m[0].length; continue; }
        text += "&"; i++;
        continue;
      }
      if (c === "\n") {
        var hard = / {2,}$/.test(text);
        text = text.replace(/ +$/, "");
        flush();
        nodes.push(hard ? { t: "br" } : { t: "text", v: "\n" });
        i++;
        while (src[i] === " ") i++;
        continue;
      }
      if ((c === "h" || c === "w" || c === "H" || c === "W") && (i === 0 || /[\s*_~(]/.test(src[i - 1]))) {
        RE_BARE.lastIndex = i;
        if ((m = RE_BARE.exec(src))) {
          var u = trimBareUrl(m[0]);
          if (/^https?:\/\/[\w\-\[]/i.test(u) || /^www\.[\w\-]+\.\w/i.test(u)) {
            flush();
            nodes.push({ t: "autolink", href: /^www\./i.test(u) ? "http://" + u : u, v: u });
            i += u.length;
            continue;
          }
        }
      }
      text += c;
      i++;
    }
    flush();
    processEmphasis(nodes);
    return nodes;
  }

  function trimBareUrl(u) {
    for (;;) {
      var last = u[u.length - 1];
      if (/[?!.,:*_~'"]/.test(last)) { u = u.slice(0, -1); continue; }
      if (last === ")") {
        var o = (u.match(/\(/g) || []).length, cl = (u.match(/\)/g) || []).length;
        if (cl > o) { u = u.slice(0, -1); continue; }
      }
      if (last === ";") {
        var em = /&[A-Za-z0-9]+;$/.exec(u);
        if (em) { u = u.slice(0, em.index); continue; }
      }
      return u;
    }
  }

  function processEmphasis(nodes) {
    var ci = 0;
    while (ci < nodes.length) {
      var cl = nodes[ci];
      if (cl.t !== "delim" || !cl.close || cl.n === 0) { ci++; continue; }
      var found = -1;
      for (var oi = ci - 1; oi >= 0; oi--) {
        var o = nodes[oi];
        if (o.t !== "delim" || o.ch !== cl.ch || !o.open || o.n === 0) continue;
        if (cl.ch === "~") { if (o.n === cl.n && o.n <= 2) { found = oi; break; } continue; }
        if ((o.close || cl.open) && (o.orig + cl.orig) % 3 === 0 && !(o.orig % 3 === 0 && cl.orig % 3 === 0)) continue;
        found = oi;
        break;
      }
      if (found < 0) { ci++; continue; }
      var op = nodes[found], use, tag;
      if (cl.ch === "~") { use = op.n; tag = "del"; }
      else { use = op.n >= 2 && cl.n >= 2 ? 2 : 1; tag = use === 2 ? "strong" : "em"; }
      op.n -= use;
      cl.n -= use;
      var el = { t: "el", tag: tag, kids: nodes.slice(found + 1, ci) };
      nodes.splice(found + 1, ci - found - 1, el);
      ci = found + 2;
      if (op.n === 0) { nodes.splice(found, 1); ci--; }
      if (cl.n === 0) nodes.splice(ci, 1);
    }
  }

  // ---- URLs and local paths ------------------------------------------------------------------

  function safeUrl(u, image) {
    var s = String(u).replace(/[\x00-\x20\x7f]/g, "").toLowerCase();
    var sm = /^([a-z][a-z0-9+.\-]*):/.exec(s);
    if (!sm) return u;
    var scheme = sm[1];
    if (/^(https?|mailto|ftp|file|tel|sftp|ssh|vscode|irc)$/.test(scheme)) return u;
    if (image && scheme === "data" && /^data:image\/(png|gif|jpe?g|webp);/.test(s)) return u;
    return null;
  }

  function splitLine(p) {
    var m = /^(.*?)(?::(\d+)(?::\d+)?|#L(\d+)(?:-L?\d+)?)$/.exec(p);
    if (m && m[1]) return { path: m[1], line: parseInt(m[2] || m[3], 10) };
    return { path: p, line: null };
  }
  function safeDecode(s) { try { return decodeURIComponent(s); } catch (e) { return s; } }

  // Link hrefs that point at local files: absolute paths, file://, or scheme-less relative paths.
  function localFromHref(href) {
    var p;
    if (/^file:\/\//i.test(href)) p = safeDecode(href.replace(/^file:\/\/[^\/]*/i, ""));
    else if (/^[A-Za-z][A-Za-z0-9+.\-]*:/.test(href) || /^(?:#|\/\/|\?)/.test(href) || !href) return null;
    else p = safeDecode(href);
    var r = splitLine(p);
    return r.path ? r : null;
  }

  var COMMON_EXT = /\.(py|js|mjs|cjs|ts|tsx|jsx|md|json|html?|css|go|rs|rb|java|kt|c|h|cc|cpp|hpp|sh|bash|zsh|ya?ml|toml|ini|cfg|conf|txt|sql|lua|php|swift|scala|ex|exs|erl|hs|ml|vue|svelte|lock|xml|csv|tsv|log|env|jsonnet|libsonnet|tf|proto|gradle|mk)$/i;

  // Inline code that looks like a file path: /x/y.py, src/a.js:12, ./foo, ~/notes.md, main.go:3
  function localFromCode(s) {
    s = s.trim();
    if (s.length < 2 || s.length > 400 || /\s/.test(s)) return null;
    if (/^file:\/\//i.test(s)) return localFromHref(s);
    if (s.indexOf("://") >= 0 || s.slice(0, 2) === "//") return null;
    var r = splitLine(s), p = r.path;
    if (!/^[\w.@+~\-\/]+$/.test(p) || p === "/" || /^\.+$/.test(p)) return null;
    var ok = false;
    if (/^(?:\/|\.\.?\/|~\/)./.test(p)) ok = true;
    else if (p.indexOf("/") > 0 && (/\.[A-Za-z0-9]{1,10}$/.test(p) || /\/$/.test(p))) ok = true;
    else if (r.line && /\.[A-Za-z0-9]{1,10}$/.test(p)) ok = true;
    else if (COMMON_EXT.test(p) && /^[\w@+\-][\w.@+\-]*$/.test(p) && !/^\d/.test(p)) ok = true;
    if (/^\/(?:api|ws|pty)\b/.test(p) && !/\.[A-Za-z0-9]{1,10}$/.test(p)) ok = false;
    return ok ? r : null;
  }

  // Loose variant used when the server resolved the message's links (src/py/sfh/links.py from_code):
  // whether the file existed when the message was written decides, so `Makefile` works too.
  function looseFromCode(s) {
    s = s.trim();
    if (s.length < 2 || s.length > 400 || /\s/.test(s)) return null;
    if (/^file:\/\//i.test(s)) return localFromHref(s);
    if (s.indexOf("://") >= 0 || s.slice(0, 2) === "//") return null;
    var r = splitLine(s);
    if (!/^[\w.@+~\-\/]+$/.test(r.path) || r.path === "/" || /^\.+$/.test(r.path)) return null;
    return r;
  }
  // ctx.links (a message's {path-as-written: {path, dir}} map) -> the resolved entry, or null
  function lookupLink(ctx, info) {
    if (!info || !ctx.links) return null;
    var hit = Object.prototype.hasOwnProperty.call(ctx.links, info.path) ? ctx.links[info.path] : null;
    return hit && hit.path ? hit : null;
  }

  function localAttrs(info, hit) {
    if (!info) return "";
    return ' data-local-path="' + esc(info.path) + '"' + (info.line ? ' data-line="' + info.line + '"' : "") +
      (hit ? ' data-abs="' + esc(hit.path) + '"' + (hit.dir ? ' data-dir="1"' : "") : "");
  }

  // ---- raw HTML sanitizer --------------------------------------------------------------------
  // Agents may write HTML (diagrams, inline SVG, <details>, …). It is re-serialized from tokens: text is
  // escaped, tags/attributes are allowlisted, URLs checked, styles filtered. Output is always balanced
  // against `st` (the tags this container opened), so a stray or missing end tag can't leak into the app.

  var NAMED_ENT = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: "\u00a0", colon: ":", Tab: "\t", NewLine: "\n" };
  // decodes what browsers would decode in an attribute value (unknown named entities stay literal and
  // are re-escaped, so the browser shows them as text: `java&colon;script:` can't sneak through)
  function decodeEnt(s) {
    return s.replace(/&(?:#(\d{1,7})|#[xX]([0-9a-fA-F]{1,6})|([A-Za-z]+));?/g, function (all, d, x, nm) {
      if (nm) return Object.prototype.hasOwnProperty.call(NAMED_ENT, nm) && all[all.length - 1] === ";" ? NAMED_ENT[nm] : all;
      var cp = d ? parseInt(d, 10) : parseInt(x, 16);
      return cp > 0 && cp <= 0x10ffff && !(cp >= 0xd800 && cp <= 0xdfff) ? String.fromCodePoint(cp) : "\uFFFD";
    });
  }
  function escText(s) {
    return s.replace(/&(?!#[0-9]{1,7};|#[xX][0-9a-fA-F]{1,6};|[A-Za-z][A-Za-z0-9]{1,31};)/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function prefixIds(v) { return v.replace(/url\(\s*(['"]?)\s*#/gi, "url($1#" + ID_PREFIX); }

  // style="…": declarations are kept one by one; no url() except url(#id), no fixed/absolute/sticky
  // positioning (could cover the app), no CSS escapes/expressions.
  function safeStyle(v) {
    if (/\\|expression\s*\(|javascript:|vbscript:|@import|behavior\s*:|-moz-binding|<\//i.test(v)) return "";
    var out = [];
    v.split(";").forEach(function (decl) {
      var m = /^\s*([-A-Za-z]+)\s*:\s*([\s\S]*?)\s*$/.exec(decl);
      if (!m || !m[2]) return;
      var prop = m[1].toLowerCase(), val = m[2];
      if (prop === "position" && !/^(static|relative)(\s*!important)?$/i.test(val)) return;
      if (/url\(\s*(?!['"]?\s*#)/i.test(val)) return;
      out.push(prop + ": " + prefixIds(val));
    });
    return out.join("; ");
  }

  function closeTags(st) { var s = ""; while (st.length) s += "<" + "/" + st.pop() + ">"; return s; }

  var RE_HTML_TOKEN = new RegExp(CMT + "[\\s\\S]*?(?:-->|$)|<![^>]*>?|<\\?[^>]*>?|" + H_OPEN + "|" + H_CLOSE, "g");
  var RE_TAG_PARTS = /^<(\/?)([A-Za-z][A-Za-z0-9-]*)([\s\S]*?)(\/?)\s*>$/;
  var RE_ATTR = /([^\s"'>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;

  // One tag -> sanitized markup ("" when dropped). st: open-tag stack, updated.
  function sanitizeTag(tok, ctx, st, inLink) {
    var m = RE_TAG_PARTS.exec(tok);
    if (!m) return "";
    var close = !!m[1], name = m[2], lname = name.toLowerCase(), selfClose = !!m[4];
    if (!SAFE_TAGS[lname] || (lname === "a" && inLink)) return "";
    var inSvg = st.indexOf("svg") >= 0 || lname === "svg";
    if (close) {
      var at = st.lastIndexOf(lname);
      if (at < 0 || VOID_TAGS[lname]) return "";
      var s = "";
      while (st.length > at) s += "<" + "/" + st.pop() + ">";
      return s;
    }
    var attrs = [], href = null, title = null, am;
    RE_ATTR.lastIndex = 0;
    while ((am = RE_ATTR.exec(m[3]))) {
      var an = am[1], lan = an.toLowerCase();
      var val = decodeEnt(am[2] !== undefined ? am[2] : am[3] !== undefined ? am[3] : am[4] !== undefined ? am[4] : "");
      if (!SAFE_ATTRS[lan] && !/^aria-[a-z-]+$/.test(lan)) continue;
      if (lan === "style") { val = safeStyle(val); if (!val) continue; }
      else if (lan === "id") { if (!/^[\w\-:.]+$/.test(val)) continue; val = ID_PREFIX + val; }
      else if (lan === "href" || lan === "xlink:href") {
        if (lname === "a") { href = val; continue; } // links go through linkOpen below
        if (val[0] === "#") val = "#" + ID_PREFIX + val.slice(1);
        else if (lname !== "image" || safeUrl(val, true) === null) continue; // <use>, gradients: same-document only
      } else if (lan === "src") { if (lname !== "img" || safeUrl(val, true) === null) continue; }
      else if (lname === "a" && lan === "title") { title = val; continue; }
      else val = prefixIds(val);
      attrs.push(an + '="' + esc(val) + '"');
    }
    if (/^animate/.test(lname)) {
      // SVG animation can set attributes: never href/event handlers
      var target = /\battributename\s*=\s*["']?([^"'\s>]*)/i.exec(m[3]);
      if (target && !/^[A-Za-z-]+$/.test(target[1]) || target && /href|^on/i.test(target[1])) return "";
    }
    var a = attrs.length ? " " + attrs.join(" ") : "";
    var open;
    if (lname === "a") {
      if (href !== null && href[0] === "#") href = "#" + ID_PREFIX + href.slice(1);
      var safe = href === null ? null : safeUrl(href);
      open = safe !== null ? linkOpen(safe, title, ctx).slice(0, -1) + a + ">" : "<a" + a + ">";
    } else open = "<" + name + a + (lname === "img" ? ' loading="lazy"' : "") + ">";
    if (VOID_TAGS[lname] && !inSvg) return open;
    if (selfClose) return inSvg ? open.slice(0, -1) + "/>" : open + "<" + "/" + name + ">";
    st.push(lname);
    return open;
  }

  // A fragment of raw HTML -> sanitized markup. Tags left open stay on `st` (closed by the caller).
  function sanitizeHtml(src, ctx, st, inLink) {
    var out = "", last = 0, m;
    RE_HTML_TOKEN.lastIndex = 0;
    while ((m = RE_HTML_TOKEN.exec(src))) {
      out += escText(src.slice(last, m.index));
      last = RE_HTML_TOKEN.lastIndex;
      var tok = m[0];
      if (tok[1] === "!" || tok[1] === "?") continue; // comments, doctype, processing instructions
      var nm = /^<([A-Za-z][A-Za-z0-9-]*)/.exec(tok);
      if (nm && DROP_TAGS[nm[1].toLowerCase()]) {
        // dropped with its content: skip to the matching end tag (or the end)
        var re = new RegExp("<\\/" + nm[1] + "\\s*>", "ig");
        re.lastIndex = last;
        var e = re.exec(src);
        last = e ? re.lastIndex : src.length;
        RE_HTML_TOKEN.lastIndex = last;
        continue;
      }
      out += sanitizeTag(tok, ctx, st, inLink);
    }
    return out + escText(src.slice(last));
  }

  // ---- artifacts -----------------------------------------------------------------------------
  // ```artifact title="…"  … a self-contained HTML document …  ```  is shown by the web client in a
  // sandboxed iframe (scripts allowed, no same-origin), see artifactDoc and app.js mountArtifacts.

  function artifactTitle(info) {
    var rest = String(info || "").replace(/^\s*artifact\b/i, "");
    var m = /\btitle\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))/i.exec(rest);
    var t = m ? (m[1] !== undefined ? m[1] : m[2] !== undefined ? m[2] : m[3]) : rest.trim().replace(/^["']|["']$/g, "");
    return t.trim() || "Artifact";
  }
  function isArtifactInfo(info) { return /^artifact$/i.test(String(info || "").split(/\s+/)[0]); }

  var ARTIFACT_CSP = "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' https: blob: data:; " +
    "style-src 'unsafe-inline' https:; img-src https: data: blob:; font-src https: data:; media-src https: data: blob:; " +
    "connect-src https: data: blob:; worker-src blob:; frame-src https: data: blob:";
  var SC = "scr" + "ipt";
  // The srcdoc for an artifact's iframe: a CSP that keeps it off the local harness server (http://127.0.0.1:…
  // is not https:) plus, unless opts.autoHeight === false, a tiny script that reports the content height.
  function artifactDoc(src, opts) {
    src = String(src == null ? "" : src);
    var meta = '<meta http-equiv="Content-Security-Policy" content="' + ARTIFACT_CSP + '">';
    var dt = /^\s*<!doctype[^>]*>/i.exec(src);
    var doc = dt ? dt[0] + meta + src.slice(dt[0].length) : meta + src;
    if (!opts || opts.autoHeight !== false) {
      doc += "\n<" + SC + ">(function(){var l=0;function s(){var d=document.documentElement,b=document.body;" +
        "var h=Math.ceil(Math.max(d.getBoundingClientRect().height,b?b.scrollHeight:0));" +
        "if(h!==l){l=h;parent.postMessage({sfhArtifactHeight:h},\"*\")}}" +
        "addEventListener(\"load\",s);if(window.ResizeObserver)new ResizeObserver(s).observe(document.documentElement);" +
        "setTimeout(s,0)})()<" + "/" + SC + ">";
    }
    return doc;
  }

  function renderArtifact(b, ctx) {
    var title = artifactTitle(b.info);
    if (b.open && ctx.streaming) {
      var nl = b.text ? b.text.split("\n").length : 0;
      return '<div class="artifact pending"><div class="artifact-head"><span class="artifact-title">' + esc(title) +
        '<' + '/span><span class="artifact-status">building artifact… ' + nl + " line" + (nl === 1 ? "" : "s") +
        "<" + "/span><" + "/div><" + "/div>\n";
    }
    var btn = function (act, label, tip) {
      return '<button type="button" class="artifact-btn" data-artifact-act="' + act + '" title="' + tip + '">' + label + "<" + "/button>";
    };
    return '<div class="artifact" data-title="' + esc(title) + '"><div class="artifact-head"><span class="artifact-title">' +
      esc(title) + "<" + "/span><span class=\"artifact-actions\">" +
      btn("source", "Source", "Show the HTML source") + btn("full", "Fullscreen", "Show fullscreen") +
      btn("open", "Open", "Open in a new tab (still sandboxed)") + btn("download", "Download", "Download as .html") +
      "<" + "/span><" + "/div>" +
      '<pre class="artifact-src" hidden><code class="language-html" data-lang="html">' + esc(b.text) + (b.text ? "\n" : "") +
      "<" + "/code><" + "/pre>" + '<div class="artifact-frame"><' + "/div><" + "/div>\n";
  }

  // ---- rendering -----------------------------------------------------------------------------

  function plainText(nodes) {
    var s = "";
    for (var i = 0; i < nodes.length; i++) {
      var nd = nodes[i];
      if (nd.t === "text" || nd.t === "code" || nd.t === "autolink") s += nd.v;
      else if (nd.t === "raw") s += nd.v;
      else if (nd.t === "delim") s += new Array(nd.n + 1).join(nd.ch);
      else if (nd.kids) s += plainText(nd.kids);
    }
    return s;
  }

  function linkOpen(href, title, ctx) {
    var local = ctx.localPaths ? localFromHref(href) : null;
    var hit = null;
    if (local && ctx.links) {
      hit = lookupLink(ctx, local);
      // a local link whose target did not exist when the message was written: not clickable
      if (!hit) return '<a class="local-missing" title="' + esc((title ? title + " — " : "") + local.path + " did not exist when this was written") + '">';
    }
    var a = '<a href="' + esc(href) + '"';
    if (title) a += ' title="' + esc(title) + '"';
    if (local) a += ' class="local-path"' + localAttrs(local, hit);
    else if (/^(https?|ftp):/i.test(href)) a += ' target="_blank" rel="noopener noreferrer"';
    if (ctx.linkAttrs) a += ctx.linkAttrs(href, local) || "";
    return a + ">";
  }

  function renderInline(nodes, ctx, inLink) {
    var out = "", st = []; // st: raw HTML tags opened at this level (closed at its end)
    for (var i = 0; i < nodes.length; i++) {
      var nd = nodes[i];
      switch (nd.t) {
        case "text": out += esc(nd.v); break;
        case "html": out += sanitizeHtml(nd.v, ctx, st, inLink); break;
        case "raw": out += nd.v; break;
        case "br": out += "<br>\n"; break;
        case "delim": out += esc(new Array(nd.n + 1).join(nd.ch)); break;
        case "code": {
          var loc = null, hit = null;
          if (ctx.localPaths && !inLink) {
            if (ctx.links) { loc = looseFromCode(nd.v); hit = lookupLink(ctx, loc); if (!hit) loc = null; }
            else loc = localFromCode(nd.v);
          }
          out += "<code" + (loc ? ' class="local-path"' + localAttrs(loc, hit) : "") + ">" + esc(nd.v) + "<" + "/code>";
          break;
        }
        case "el": out += "<" + nd.tag + ">" + renderInline(nd.kids, ctx, inLink) + "<" + "/" + nd.tag + ">"; break;
        case "autolink":
          if (inLink || safeUrl(nd.href) === null) out += esc(nd.v);
          else out += linkOpen(nd.href, null, ctx) + esc(nd.v) + "<" + "/a>";
          break;
        case "link": {
          var href = safeUrl(nd.dest);
          if (inLink || href === null) out += renderInline(nd.kids, ctx, inLink);
          else out += linkOpen(href, nd.title, ctx) + renderInline(nd.kids, ctx, true) + "<" + "/a>";
          break;
        }
        case "img": {
          var src = safeUrl(nd.dest, true);
          if (src === null) out += esc(plainText(nd.kids));
          else out += '<img src="' + esc(src) + '" alt="' + esc(plainText(nd.kids)) + '"' +
            (nd.title ? ' title="' + esc(nd.title) + '"' : "") + ' loading="lazy">';
          break;
        }
      }
    }
    return out + closeTags(st);
  }

  function inline(text, ctx) { return renderInline(parseInline(text, ctx), ctx, false); }

  function renderBlocks(blocks, ctx, tight) {
    var out = "", st = []; // st: tags opened by raw HTML blocks of this container (e.g. <details> … </details>)
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      ctx.holdTail = !!(ctx.streaming && b.tail);
      switch (b.t) {
        case "p":
          out += tight ? inline(b.text, ctx) + (i < blocks.length - 1 ? "\n" : "") : "<p>" + inline(b.text, ctx) + "<" + "/p>\n";
          break;
        case "h": out += "<h" + b.level + ">" + inline(b.text, ctx) + "<" + "/h" + b.level + ">\n"; break;
        case "hr": out += "<hr>\n"; break;
        case "html": {
          // streaming: an unfinished <svg>/<pre> waits for its end tag; elsewhere a half-received tag is cut
          if (b.open && ctx.streaming && b.raw) {
            if (b.tag === "svg" || b.tag === "pre") out += '<div class="md-pending">rendering ' + b.tag + "…<" + "/div>\n";
            break;
          }
          var hsrc = b.open && ctx.streaming ? b.text.replace(RE_PARTIAL_TAG_END, "") : b.text;
          out += sanitizeHtml(hsrc, ctx, st, false) + "\n";
          break;
        }
        case "code": {
          if (isArtifactInfo(b.info)) { out += renderArtifact(b, ctx); break; }
          var lang = (b.info.split(/\s+/)[0] || "");
          out += "<pre" + (b.open ? ' class="streaming"' : "") + "><code" +
            (lang ? ' class="language-' + esc(lang) + '" data-lang="' + esc(lang) + '"' : "") + ">" +
            esc(b.text) + (b.text ? "\n" : "") + "<" + "/code><" + "/pre>\n";
          break;
        }
        case "quote": out += "<blockquote>\n" + renderBlocks(b.kids, ctx, false) + "<" + "/blockquote>\n"; break;
        case "list": {
          var tag = b.ordered ? "ol" : "ul";
          out += "<" + tag + (b.ordered && b.start !== 1 ? ' start="' + b.start + '"' : "") +
            (b.items.some(function (it) { return it.task !== null; }) ? ' class="contains-task-list"' : "") + ">\n";
          for (var k = 0; k < b.items.length; k++) {
            var it = b.items[k];
            out += it.task !== null
              ? '<li class="task-list-item"><input type="checkbox" disabled' + (it.task ? " checked" : "") + "> "
              : "<li>";
            out += renderBlocks(it.kids, ctx, !b.loose) + "<" + "/li>\n";
          }
          out += "<" + "/" + tag + ">\n";
          break;
        }
        case "table": {
          var al = function (j) { return b.align[j] ? ' style="text-align:' + b.align[j] + '"' : ""; };
          out += "<table>\n<thead>\n<tr>";
          for (var h = 0; h < b.head.length; h++) out += "<th" + al(h) + ">" + inline(b.head[h], ctx) + "<" + "/th>";
          out += "<" + "/tr>\n<" + "/thead>\n";
          if (b.rows.length) {
            out += "<tbody>\n";
            for (var r = 0; r < b.rows.length; r++) {
              out += "<tr>";
              for (var c = 0; c < b.rows[r].length; c++) out += "<td" + al(c) + ">" + inline(b.rows[r][c], ctx) + "<" + "/td>";
              out += "<" + "/tr>\n";
            }
            out += "<" + "/tbody>\n";
          }
          out += "<" + "/table>\n";
          break;
        }
      }
    }
    ctx.holdTail = false;
    return out + (st.length ? closeTags(st) + "\n" : "");
  }

  // marks the block holding the end of the text (only it can contain a tag that is still arriving)
  function markTail(blocks) {
    var b = blocks[blocks.length - 1];
    if (!b) return;
    if (b.t === "quote") markTail(b.kids);
    else if (b.t === "list") markTail(b.items[b.items.length - 1].kids);
    else b.tail = true;
  }

  function render(text, opts) {
    opts = opts || {};
    // opts.links: {path-as-written: {path, dir}} resolved by the server when the message was stored;
    // when given, only those local paths become links (others stay plain text / inert anchors)
    // opts.html === false: raw HTML is escaped instead of (sanitized and) passed through
    // opts.streaming: the text is a growing draft: hold back half-received tags/blocks/artifacts
    var ctx = { refs: {}, localPaths: opts.localPaths !== false, linkAttrs: opts.linkAttrs || null,
      links: opts.links && typeof opts.links === "object" ? opts.links : null, streaming: !!opts.streaming, holdTail: false };
    var lines = String(text == null ? "" : text).replace(/\r\n?/g, "\n").replace(/\u0000/g, "\uFFFD").split("\n").map(expandTabs);
    htmlOn = opts.html !== false;
    try {
      var blocks = parseBlocks(lines, ctx);
      if (ctx.streaming) markTail(blocks);
      return renderBlocks(blocks, ctx, false);
    } finally { htmlOn = true; }
  }

  var api = { render: render, escape: esc, sanitizeHtml: function (s, opts) {
      var ctx = { refs: {}, localPaths: !!(opts && opts.localPaths), linkAttrs: null, links: null }, st = [];
      return sanitizeHtml(String(s), ctx, st, false) + closeTags(st);
    }, artifactDoc: artifactDoc, artifactTitle: artifactTitle, localFromCode: localFromCode, looseFromCode: looseFromCode, localFromHref: localFromHref, safeUrl: safeUrl };
  root.SFHMarkdown = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
