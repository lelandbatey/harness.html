// SFHIdenticon: semantic identicons that stay legible at 16x16.
//
// Layout: a 5x5 cell grid on a plain tinted square.
//   - tile color (hue)      <- subjects[0], the broad domain: same domain => same color family.
//                              Hue is the circular mean of per-word hues, so overlapping wording
//                              ("python", "python packaging") lands on nearby hues.
//   - "wings" (columns 0-1 mirrored onto 3-4, 10 cells) <- subjects[1], the narrower topic, via a
//                              SimHash over its words: topics sharing words share most cells.
//   - "spine" (center column, 5 cells) shape <- the session id: two sessions with identical subjects
//                              still differ. Its color <- subjects[2], the specific thing.
//   - light tile / dark tile tone <- first word of subjects[0] (doubles distinguishable domains).
//   - a small lightness shift of the tile <- session id as a tie-breaker.
//   - opts.spin (0..1, optional): a "working" frame: a bright segment that travels around the tile's
//     edge (callers cycle the phase for an animated favicon); the identicon itself is unchanged.
// Subjects are normalized (lowercase, punctuation stripped, stopwords dropped, trivially
// singularized) before hashing. Missing subjects fall back to title words, then the id.
(function (root) {
  "use strict";

  function hash32(str) {
    var h = 0x811c9dc5;
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    h ^= h >>> 16; h = Math.imul(h, 0x85ebca6b);
    h ^= h >>> 13; h = Math.imul(h, 0xc2b2ae35);
    h ^= h >>> 16;
    return h >>> 0;
  }

  var STOP = {};
  ("a an the and or of for to in on at by with from into about via vs is are be how what why when " +
   "my our your this that these those using use new fix fixing add adding make making help some " +
   "it its as do does can not no").split(" ").forEach(function (w) { STOP[w] = 1; });

  function singular(w) {
    if (w.length > 4 && /ies$/.test(w)) return w.slice(0, -3) + "y";
    if (w.length > 4 && /(sses|ches|shes|xes)$/.test(w)) return w.slice(0, -2);
    if (w.length > 3 && /s$/.test(w) && !/(ss|us|is|os)$/.test(w)) return w.slice(0, -1);
    return w;
  }
  function normalize(s) { return String(s || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim(); }
  function words(s) {
    var ws = normalize(s).split(" ").filter(function (w) { return w && !STOP[w]; }).map(singular);
    return ws.length ? ws : normalize(s).split(" ").filter(Boolean).map(singular);
  }

  // Circular mean of word hues => related wording gives related hues.
  function hueOf(ws, salt) {
    var x = 0, y = 0;
    for (var i = 0; i < ws.length; i++) {
      var a = (hash32(salt + ws[i]) % 360) * Math.PI / 180, wgt = i === 0 ? 1.5 : 1;
      x += Math.cos(a) * wgt; y += Math.sin(a) * wgt;
    }
    if (!ws.length || (Math.abs(x) < 1e-9 && Math.abs(y) < 1e-9)) return hash32(salt + ws.join(" ")) % 360;
    return (Math.round(Math.atan2(y, x) * 180 / Math.PI) + 360) % 360;
  }

  // SimHash of words into `bits` bits.
  function simhash(ws, bits, salt) {
    var acc = new Array(bits).fill(0);
    for (var i = 0; i < ws.length; i++) {
      var h = hash32(salt + ws[i]);
      for (var b = 0; b < bits; b++) acc[b] += (h >>> b) & 1 ? 1 : -1;
    }
    var tie = hash32(salt + ws.join(" ")), out = 0;
    for (var k = 0; k < bits; k++) if (acc[k] > 0 || (acc[k] === 0 && (tie >>> k) & 1)) out |= 1 << k;
    return out;
  }
  function popcount(v) { var c = 0; while (v) { c += v & 1; v >>>= 1; } return c; }

  function subjectsOf(session) {
    var id = String(session.id || "");
    var subs = (session.subjects || []).map(normalize).filter(Boolean);
    if (!subs.length) {
      var tw = words(session.title || "");
      if (tw.length) subs = [tw[0], tw.join(" "), tw[tw.length - 1]];
      else subs = [id];
    }
    while (subs.length < 3) subs.push(subs[subs.length - 1] + " " + id);
    return subs;
  }

  function features(session) {
    var id = String(session.id || "") || "?";
    var s = subjectsOf(session);
    var w0 = words(s[0]), w1 = words(s[1]), w2 = words(s[2]);
    var hue = hueOf(w0, "h:");
    var hid = hash32("id:" + id);

    var wings = simhash(w1, 10, "w:");
    var pc = popcount(wings), fix = hash32("wfix:" + w1.join(" "));
    if (pc < 3) wings |= fix & 0x3ff;
    else if (pc > 7) wings &= fix | 0x155;
    if (popcount(wings) < 3) wings |= 0x0a5;

    var spine = hid & 31;
    if (spine === 0) spine = 4;
    if (spine === 31) spine = 27;

    var accent = hueOf(w2, "a:");
    var d = ((accent - hue + 540) % 360) - 180;
    if (Math.abs(d) < 50) accent = (hue + (d < 0 ? -1 : 1) * (50 + Math.abs(d)) + 360) % 360;

    var fgHue = (hue + (hash32("f:" + w1.join(" ")) % 31) - 15 + 360) % 360;
    var lightJitter = ((hid >>> 8) % 7) - 3;
    // tone (light tile / dark tile) keyed on the domain's first word doubles the domain buckets
    var dark = (hash32("t:" + (w0[0] || "")) & 1) === 1;
    return {
      subjects: s, hue: hue, wings: wings, spine: spine, accent: accent, dark: dark,
      bg: dark ? "hsl(" + hue + ",42%," + (27 + lightJitter) + "%)" : "hsl(" + hue + ",62%," + (87 + lightJitter) + "%)",
      fg: dark ? "hsl(" + fgHue + ",75%,78%)" : "hsl(" + fgHue + ",58%,30%)",
      ac: dark ? "hsl(" + accent + ",95%,58%)" : "hsl(" + accent + ",85%,47%)"
    };
  }

  function grid(f) {
    // returns 5x5 array of 0 (empty) / 1 (wing) / 2 (spine)
    var g = [];
    for (var r = 0; r < 5; r++) {
      var row = [0, 0, 0, 0, 0];
      for (var c = 0; c < 2; c++) if ((f.wings >>> (r * 2 + c)) & 1) { row[c] = 1; row[4 - c] = 1; }
      if ((f.spine >>> r) & 1) row[2] = 2;
      g.push(row);
    }
    return g;
  }

  function svg(session, size, opts) {
    size = size || 16;
    opts = opts || {};
    var f = features(session), g = grid(f);
    var out = '<svg xmlns="http://www.w3.org/2000/svg" width="' + size + '" height="' + size +
      '" viewBox="0 0 16 16"><rect width="16" height="16" fill="' + f.bg + '"/><g shape-rendering="crispEdges">';
    for (var r = 0; r < 5; r++) for (var c = 0; c < 5; c++) {
      if (!g[r][c]) continue;
      out += '<rect x="' + (0.5 + c * 3) + '" y="' + (0.5 + r * 3) + '" width="3" height="3" fill="' +
        (g[r][c] === 2 ? f.ac : f.fg) + '"/>';
    }
    out += "</g>";
    if (typeof opts.spin === "number") {
      // perimeter of the 15x15 ring is 60; a 16-long segment moves around it clockwise
      var off = -((opts.spin % 1) * 60);
      out += '<rect x="0.6" y="0.6" width="14.8" height="14.8" fill="none" stroke="' + (f.dark ? "#ffffff" : "#000000") +
        '" stroke-opacity=".9" stroke-width="1.2" stroke-dasharray="16 44" stroke-dashoffset="' + off.toFixed(2) + '"/>';
    }
    return out + "</svg>";
  }

  function dataUrl(session, size, opts) { return "data:image/svg+xml," + encodeURIComponent(svg(session, size, opts)); }

  var api = { svg: svg, dataUrl: dataUrl, features: features, grid: grid, normalize: normalize, words: words };
  root.SFHIdenticon = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
