/* Gallery4ComfyUI —— 统一 SVG 图标集
 *
 * 原来全站混用 emoji（🔄🎬🎨）和 Unicode 符号（★⇄⛶），
 * 风格不统一、字号不一致、有的还有彩色底。
 * 这里统一成一套 24×24 线性图标，用 currentColor 着色，随主题走。
 *
 * 用法：
 *   <span class="ic" data-ic="refresh"></span>        静态占位
 *   ICON.svg("refresh")                               取字符串（需要动态插入时）
 *   ICON.paint(root)                                  把整棵树的 data-ic 填上
 */
(function (root) {
  "use strict";

  // 全部 24×24 viewBox，stroke 用 currentColor，线宽 1.8
  var P = {
    // —— 数据源 / 标签 ——
    comfy: "M4 7h16M4 12h16M4 17h10",
    webui: "M3 5h18v14H3zM3 9h18",
    video: "M4 5h16v14H4zM10 9l5 3-5 3z",
    artist: "M12 3a9 9 0 100 18 3 3 0 003-3 3 3 0 013-3h1a3 3 0 003-3 9 9 0 00-10-9zM7.5 10.5h.01M11 7.5h.01M15.5 9h.01",
    artlib: "M4 5h7v14H4zM13 5h7v14h-7zM6.5 9h2M15.5 9h2",
    star: "M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1.1 5.9-5.3-2.9-5.3 2.9 1.1-5.9L3.5 9.7l5.9-.8z",
    starFill: "M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1.1 5.9-5.3-2.9-5.3 2.9 1.1-5.9L3.5 9.7l5.9-.8z",

    // —— 操作 ——
    search: "M11 4a7 7 0 100 14 7 7 0 000-14zM16.2 16.2L21 21",
    filter: "M3 5h18l-7 8v6l-4 2v-8z",
    refresh: "M20 11a8 8 0 10-2.3 5.7M20 5v6h-6",
    plus: "M12 5v14M5 12h14",
    close: "M6 6l12 12M18 6L6 18",
    check: "M5 12.5l4.5 4.5L19 7",
    more: "M4 6h16M4 12h16M4 18h16",
    puzzle: "M9 4h6v3a2 2 0 104 0h1v6h-3a2 2 0 100 4v1H5v-6h3a2 2 0 100-4H5V4z",
    settings: "M12 9a3 3 0 100 6 3 3 0 000-6zM19.4 13a7.6 7.6 0 000-2l2-1.5-2-3.4-2.4 1a7.6 7.6 0 00-1.7-1L15 3.5H9L8.7 6a7.6 7.6 0 00-1.7 1l-2.4-1-2 3.4L4.6 11a7.6 7.6 0 000 2l-2 1.5 2 3.4 2.4-1a7.6 7.6 0 001.7 1l.3 2.5h6l.3-2.5a7.6 7.6 0 001.7-1l2.4 1 2-3.4z",
    folder: "M3 6h5l2 2h11v11H3z",
    folderOpen: "M3 6h5l2 2h11v3H7l-2 9H3z",
    stats: "M4 20V10M10 20V4M16 20v-7M22 20H2",
    theme: "M12 3a9 9 0 100 18 4.5 4.5 0 010-9 4.5 4.5 0 000-9z",
    lang: "M3 5h10M8 5v14M5 12h6M14 9h7l-3.5 8h3.5",
    download: "M12 4v10M7.5 9.5L12 14l4.5-4.5M4 18h16",
    copy: "M9 9h10v10H9zM5 15V5h10",
    image: "M3 5h18v14H3zM3 15l5-5 4 4 3-3 6 6",
    info: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 8h.01M12 11v6",
    external: "M14 4h6v6M20 4l-8 8M18 14v6H4V6h6",
    trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
    dice: "M4 4h16v16H4zM8.5 8.5h.01M15.5 8.5h.01M12 12h.01M8.5 15.5h.01M15.5 15.5h.01",
    package: "M12 3l9 5v8l-9 5-9-5V8zM3 8l9 5 9-5M12 13v8",

    // —— 播放器 ——
    play: "M7 4.5l12 7.5-12 7.5z",
    pause: "M8 5v14M16 5v14",
    prev: "M18 6v12L8 12zM6 5v14",
    next: "M6 6v12l10-6zM18 5v14",
    fullscreen: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
    exitFull: "M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5",
    swap: "M7 8h13l-3-3M17 16H4l3 3",
    music: "M9 18V6l10-2v12M9 18a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0zM19 16a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0z",
    grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
    fit: "M4 4h16v16H4zM9 9h6v6H9z",

    // —— 方向 ——
    up: "M12 19V5M6 11l6-6 6 6",
    down: "M12 5v14M18 13l-6 6-6-6",
    left: "M19 12H5M11 6l-6 6 6 6",
    right: "M5 12h14M13 6l6 6-6 6",
    first: "M18 6v12L8 12zM6 5v14",
    last: "M6 6v12l10-6zM18 5v14",
    updown: "M8 8l4-4 4 4M8 16l4 4 4-4",
  };

  var NS = "http://www.w3.org/2000/svg";

  function svg(name, opts) {
    var d = P[name];
    if (!d) return "";
    opts = opts || {};
    var cls = opts.cls ? ' class="' + opts.cls + '"' : "";
    var fill = opts.fill ? "currentColor" : "none";
    return '<svg' + cls + ' viewBox="0 0 24 24" width="' + (opts.size || 18) +
      '" height="' + (opts.size || 18) + '" fill="' + fill +
      '" stroke="currentColor" stroke-width="' + (opts.fill ? 0 : 1.8) +
      '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="' + d + '"/></svg>';
  }

  /** 把一棵子树里所有 [data-ic] 元素填上图标（幂等，可重复调用）。 */
  function paint(rootEl, opts) {
    rootEl = rootEl || document;
    var list = rootEl.querySelectorAll("[data-ic]");
    for (var i = 0; i < list.length; i++) {
      var el = list[i];
      var name = el.getAttribute("data-ic");
      var key = name + "|" + (opts && opts.size);
      if (el.getAttribute("data-ic-done") === key) continue;   // 已画过，跳过
      el.innerHTML = svg(name, opts);
      el.setAttribute("data-ic-done", key);
    }
    return list.length;
  }

  /** 把元素里的旧文字/emoji 换成图标 */
  function set(el, name, opts) {
    if (!el) return;
    el.innerHTML = svg(name, opts);
  }

  root.ICON = { svg: svg, paint: paint, set: set, paths: P };
  if (typeof module !== "undefined" && module.exports) module.exports = root.ICON;
})(typeof window !== "undefined" ? window : globalThis);
