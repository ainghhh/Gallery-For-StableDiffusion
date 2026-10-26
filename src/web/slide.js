/*
 * Gallery4ComfyUI —— 幻灯片播放器
 *
 * 性能要点（用户要求"运算一定要快！极快！"）：
 *   1. 用 requestAnimationFrame 驱动播放，不用 setTimeout
 *      —— setTimeout 有 16ms 抖动，节奏会飘；rAF 对齐屏幕刷新，误差 1-2ms。
 *   2. 转场只用 transform / opacity（GPU 合成），不碰 layout 属性。
 *   3. 按速度动态预加载 + 高速度用缩略图，内存恒定。
 *   4. 自适应节奏识别在 100 微秒内（见 slide_rhythm.js）。
 */
(function () {
  "use strict";

  const API = (window.__API_PREFIX__ || "/gallery4comfyui") + "/api";
  const $ = id => document.getElementById(id);

  // ---------- 状态 ----------
  const S = {
    src: localStorage.getItem("slide.src") || "comfyui",
    sort: localStorage.getItem("slide.sort") || "newest",
    fill: localStorage.getItem("slide.fill") === "1",
    sMode: localStorage.getItem("slide.sMode") || "fps",      // fps | spf | adaptive | audio
    fps: parseFloat(localStorage.getItem("slide.fps") || "8"),
    spf: parseFloat(localStorage.getItem("slide.spf") || "1"),
    aSens: parseInt(localStorage.getItem("slide.aSens") || "5", 10),
    aFixedDb: parseFloat(localStorage.getItem("slide.aFixedDb") || "-30"),
    thrMode: localStorage.getItem("slide.thrMode") || "dynamic",   // dynamic | fixed
    rMode: localStorage.getItem("slide.rMode") || "mean",     // mean | real | approx
    threshold: parseInt(localStorage.getItem("slide.thr") || "3", 10),
    trans: localStorage.getItem("slide.trans") || "fade",
    forward: localStorage.getItem("slide.fwd") !== "0",
    items: [], total: 0, idx: 0, page: 1,
    playing: false, cur: "A",                     // 当前是 A 层还是 B 层
    speed: 8,                                     // 当前实际张/秒
  };
  const save = (k, v) => { try { localStorage.setItem("slide." + k, v); } catch (e) {} };

  const rhythm = new SlideRhythm.Rhythm({ mode: S.rMode, threshold: S.threshold });

  // 音频跟拍引擎：听电脑正在放的声音，每个"高峰"切一张
  const audio = new SlideAudio.AudioBeat({
    sensitivity: S.aSens,
    thrMode: S.thrMode,
    minGapMs: 110,          // 两拍最小间隔，防同一鼓点连触
    lowpassHz: 150,         // 只听低频（鼓点/bass），旋律不误触
  });
  let audioReady = false;
  let cache = new Map();          // file -> 已解码的 HTMLImageElement
  let urls = new Map();           // file -> objectURL / 直链
  let pending = new Set();
  let loadErrors = 0;
  let waiting = 0;                // 因为下一张没就绪而"等一拍"的次数

  // ---------- 取图 URL ----------
  // 固定用原图。实测本地磁盘下原图只要 12ms（10 张/秒的预算是每张 100ms），
  // 缩略图只快 2ms —— 为了 2ms 牺牲画质毫无意义。
  function urlOf(file) {
    const q = new URLSearchParams({ source: S.src, file });
    return `${API}/image?${q.toString()}`;
  }

  // 缓存的键就是文件名（只用一个画质，不需要区分）
  function ckey(file) { return file; }

  function preload(file) {
    if (!file) return;
    const k = ckey(file);
    if (cache.has(k) || pending.has(k)) return;
    pending.add(k);
    const im = new Image();
    // decode() 让浏览器**提前完成解码**，这样克隆节点时能立刻上屏。
    // 不做这一步的话，即使图已下载完，克隆后仍要现场解码，高速下就来不及。
    im.onload = () => {
      const finish = () => { pending.delete(k); cache.set(k, im); trim(); };
      if (im.decode) {
        im.decode().then(finish, finish);
      } else {
        finish();
      }
    };
    im.onerror = () => { pending.delete(k); loadErrors++; };
    im.src = urlOf(file);
  }
  // 内存上限：必须**大于** warm() 的预载窗口，否则会在用之前就被 trim 掉。
  // 音频跟拍时拍子可能很密（快歌），窗口给宽一点。
  function cacheLimit() {
    const sp = S.sMode === "adaptive" ? 12 : (S.sMode === "audio" ? 16 : S.speed);
    return Math.max(18, Math.min(26, Math.ceil(sp * 1.8) + 12));
  }
  function trim() {
    const lim = cacheLimit();
    if (cache.size <= lim) return;
    // 保留范围要 **≥ warm() 的预载窗口**，否则刚预载好就被删掉，
    // 播放时又得现场加载 → 卡顿。
    const keep = new Set();
    for (let i = -6; i <= Math.ceil(lim * 0.8); i++) {
      const it = S.items[S.idx + i * (S.forward ? 1 : -1)];
      if (it) keep.add(ckey(it.file));
    }
    for (const k of cache.keys()) {
      if (!keep.has(k) && cache.size > lim) { cache.delete(k); urls.delete(k); }
    }
  }
  function warm() {
    // 预加载张数按速度算。音频模式按"可能的最高拍速"（180BPM≈3张/秒）算，够用。
    const sp = S.sMode === "adaptive" ? 8 : (S.sMode === "audio" ? 10 : S.speed);
    const n = Math.max(4, Math.min(14, Math.ceil(sp * 1.6)));
    const dir = S.forward ? 1 : -1;
    for (let i = -3; i <= n; i++) {
      const it = S.items[S.idx + i * dir];
      if (it) preload(it.file);
    }
  }

  // ---------- 数据（一次性取全部：不限 1000，也不分批）----------
  // 用户要的是"能看所有的"，所以第一次就把整个结果集拉下来。
  // 5 万张的索引条目也就几 MB JSON，对本地服务毫无压力；
  // 真正吃内存的图片另有预载窗口控制，不会因为条目多就爆。
  const BATCH = 20000;
  let loadingMore = false;

  async function fetchBatch(page) {
    const q = new URLSearchParams({
      source: S.src, sort: S.sort, page, page_size: BATCH,
    });
    const r = await (await fetch(`${API}/gallery?${q}`)).json();
    return r;
  }

  async function loadList(keepIdx) {
    const old = S.items[S.idx] ? S.items[S.idx].file : null;
    let all = [], page = 1, total = 0;
    // 循环取完为止（后端单页有上限时也能兜住）
    for (let guard = 0; guard < 50; guard++) {
      const r = await fetchBatch(page);
      const items = r.items || [];
      total = r.total || 0;
      if (!items.length) break;
      all = all.concat(items);
      if (all.length >= total) break;
      page++;
    }
    S.items = all;
    S.total = total || all.length;
    S.page = 1;
    if (!S.items.length) { render(); return; }
    if (keepIdx && old) {
      const i = S.items.findIndex(x => x.file === old);
      S.idx = i >= 0 ? i : 0;
    } else {
      S.idx = 0;
    }
    cache.clear(); pending.clear(); urls.clear();
    $("counter").textContent = S.items.length
      ? `${S.idx + 1} / ${S.items.length}` : "—";
    render(); warm();
  }

  // 快播到批次尾部时，后台悄悄接上下一批，做到**无缝续播**。
  // （之前写死 page_size=1000，播到第 1000 张就到头不动了 —— 这就是"到 1000 就不动"的原因。）
  async function maybeExtend() {
    if (loadingMore) return;
    if (S.items.length >= S.total) return;                    // 全拿完了
    const left = S.items.length - S.idx;
    if (left > Math.max(30, BATCH * 0.25)) return;            // 离尾部还早
    loadingMore = true;
    try {
      const nextPage = Math.ceil(S.items.length / BATCH) + 1;
      const r = await fetchBatch(nextPage);
      const add = r.items || [];
      if (add.length) {
        // 去重后追加（排序固定，理论上不会重复，防一手）
        const seen = new Set(S.items.map(x => x.file));
        for (const it of add) if (!seen.has(it.file)) S.items.push(it);
      } else {
        S.total = S.items.length;                             // 没有更多了
      }
    } catch (e) {
      // 取失败就标记到底，避免反复请求
      S.total = Math.min(S.total, S.items.length);
    } finally {
      loadingMore = false;
    }
  }

  function curItem() { return S.items[S.idx]; }

  // ---------- 渲染 ----------
  const layers = { A: $("layerA"), B: $("layerB") };

  function buildNode(it) {
    if (!it) return document.createTextNode("");
    const isVideo = it.type === "video" || /\.(mp4|webm|mov|mkv|avi|gif)$/i.test(it.file || "");
    if (isVideo) {
      const v = document.createElement("video");
      v.src = `${API}/image?source=${S.src}&file=${encodeURIComponent(it.file)}`;
      v.muted = true; v.playsInline = true; v.autoplay = true; v.loop = true;
      return v;
    }
    const c = cache.get(ckey(it.file));
    if (c) {
      // 关键：缓存里是**已解码好的 <img>**，直接克隆它 ——
      // 克隆已解码的节点会复用位图，不再走网络、不再解码，所以能瞬间显示。
      // 之前只复制 `.src`，等于每次新建元素重新下载解码，
      // 高速播放时 IO 跟不上 → 后面的图加载不出来。
      const clone = c.cloneNode(false);
      clone.decoding = "sync";       // 已解码，同步上屏避免闪一下空白
      return clone;
    }
    // 没缓存：仍然建新元素，但标出来，方便上层决定"等还是跳"
    const img = document.createElement("img");
    img.decoding = "async";
    img.dataset.miss = "1";
    img.src = urlOf(it.file);
    return img;
  }

  function render() {
    const lay = layers[S.cur];
    lay.innerHTML = "";
    lay.style.opacity = "1";
    lay.style.transform = "";
    lay.style.clipPath = "";
    layers[S.cur === "A" ? "B" : "A"].innerHTML = "";
    const it = curItem();
    if (it) lay.appendChild(buildNode(it));
    updateCounter();
  }

  function updateCounter() {
    $("counter").textContent = S.items.length
      ? `${S.idx + 1} / ${S.items.length}`
      : "—";
  }

  // ---------- 转场 ----------
  const TRANS = ["none","fade","slide-lr","slide-ud","zoom","flip","clock","blinds","pixel","pulse"];
  function pickTrans() {
    if (S.trans !== "random") return S.trans;
    return TRANS[1 + Math.floor(Math.random() * (TRANS.length - 1))];
  }

  /**
   * 切到下一张并做转场。
   * durMs 由调用方按节奏给出 —— 快速播放时动画要相应变短，否则会拖坏节奏。
   */
  function show(durMs, dirHint) {
    const it = curItem();
    if (!it) return;
    const from = layers[S.cur];
    const toKey = S.cur === "A" ? "B" : "A";
    const to = layers[toKey];
    to.innerHTML = "";
    to.appendChild(buildNode(it));

    const kind = pickTrans();
    // 动画时长：不超过间隔的 70%，且夹在 60~600ms 之间
    const d = Math.max(60, Math.min(600, Math.round(durMs * 0.7))) || 240;
    const dir = dirHint || (S.forward ? 1 : -1);

    to.style.transition = "none";
    to.style.opacity = "0";
    to.style.transform = "";
    to.style.clipPath = "";
    to.style.filter = "";
    // 强制回流，确保起始状态生效（只此一处，必要）
    void to.offsetWidth;

    switch (kind) {
      case "none":
        to.style.opacity = "1";
        from.style.opacity = "0";
        break;
      case "fade":
        to.style.transition = `opacity ${d}ms ease`;
        to.style.opacity = "1";
        from.style.transition = `opacity ${d}ms ease`;
        from.style.opacity = "0";
        break;
      case "slide-lr":
        to.style.transform = `translate3d(${100 * dir}%,0,0)`;
        from.style.transform = "translate3d(0,0,0)";
        to.style.transition = from.style.transition = `transform ${d}ms cubic-bezier(.22,.61,.36,1), opacity ${d}ms linear`;
        to.style.opacity = "1";
        void to.offsetWidth;
        to.style.transform = "translate3d(0,0,0)";
        from.style.transform = `translate3d(${-100 * dir}%,0,0)`;
        break;
      case "slide-ud":
        to.style.transform = `translate3d(0,${100 * dir}%,0)`;
        to.style.transition = from.style.transition = `transform ${d}ms cubic-bezier(.22,.61,.36,1), opacity ${d}ms linear`;
        to.style.opacity = "1";
        void to.offsetWidth;
        to.style.transform = "translate3d(0,0,0)";
        from.style.transform = `translate3d(0,${-100 * dir}%,0)`;
        break;
      case "zoom":
        to.style.transform = "scale(1.14)";
        to.style.transition = from.style.transition = `transform ${d}ms ease-out, opacity ${d}ms ease`;
        to.style.opacity = "1";
        void to.offsetWidth;
        to.style.transform = "scale(1)";
        from.style.transform = "scale(.92)";
        from.style.opacity = "0";
        break;
      case "flip":
        to.style.transform = `perspective(1400px) rotateY(${-90 * dir}deg)`;
        to.style.transition = from.style.transition = `transform ${d}ms cubic-bezier(.4,0,.2,1), opacity ${d}ms linear`;
        to.style.opacity = "1";
        void to.offsetWidth;
        to.style.transform = "perspective(1400px) rotateY(0deg)";
        from.style.transform = `perspective(1400px) rotateY(${90 * dir}deg)`;
        from.style.opacity = "0";
        break;
      case "clock":
        to.style.clipPath = `circle(0% at 50% 50%)`;
        to.style.transition = `clip-path ${d}ms cubic-bezier(.4,0,.2,1)`;
        to.style.opacity = "1";
        void to.offsetWidth;
        to.style.clipPath = "circle(75% at 50% 50%)";
        break;
      case "blinds": {
        // 用遮罩条做百叶窗：铺 8 条，逐条展开
        to.style.opacity = "1";
        to.style.transition = "none";
        const n = 8;
        to.style.clipPath = "none";
        to.style.webkitMaskImage =
          `repeating-linear-gradient(to bottom, #000 0, #000 12.5%, transparent 12.5%, transparent 25%)`;
        to.style.maskImage = to.style.webkitMaskImage;
        to.style.transition = `mask-size ${d}ms ease, -webkit-mask-size ${d}ms ease`;
        to.style.webkitMaskSize = "100% 0%";
        to.style.maskSize = "100% 0%";
        void to.offsetWidth;
        to.style.webkitMaskSize = `100% ${n * 100}%`;
        to.style.maskSize = `100% ${n * 100}%`;
        break;
      }
      case "pixel":
        to.style.opacity = "1";
        to.style.filter = "blur(14px)";
        to.style.transition = `filter ${d}ms ease-out`;
        void to.offsetWidth;
        to.style.filter = "none";
        from.style.transition = `opacity ${d}ms ease`;
        from.style.opacity = "0";
        break;
      case "pulse": {
        // 节奏型：猛地冲进来再急停（慢→冲→停）
        const fast = Math.max(50, Math.round(d * 0.35));
        to.style.transform = "scale(1.35)";
        to.style.opacity = "0";
        to.style.transition = `transform ${fast}ms cubic-bezier(.16,1,.3,1), opacity ${fast}ms linear`;
        from.style.transition = `opacity ${d}ms ease`;
        void to.offsetWidth;
        to.style.transform = "scale(1)";
        to.style.opacity = "1";
        from.style.opacity = "0";
        break;
      }
    }

    S.cur = toKey;
    setTimeout(() => {
      if (S.cur !== toKey) return;
      from.innerHTML = "";
      from.style.transition = "none";
      from.style.opacity = "0";
      from.style.transform = "";
      from.style.clipPath = "";
      from.style.filter = "";
      from.style.webkitMaskImage = from.style.maskImage = "";
      from.style.webkitMaskSize = from.style.maskSize = "";
      to.style.transition = "none";
    }, d + 30);
    updateCounter();
  }

  // ---------- 播放引擎（rAF 驱动，精度关键） ----------
  let rafId = 0, beatIdx = 0, nextAt = 0, lastFrame = 0;

  // "正在等下一张加载完"的提示：让用户知道是在等，不是卡了
  function setWaiting(on) {
    const el = $("loadingHint");
    if (!el) return;
    if (on === setWaiting._last) return;
    setWaiting._last = on;
    el.classList.toggle("show", !!on);
  }
  setWaiting._last = false;

  function plan() { return rhythm.plan(); }

  // ---------- 音频跟拍 ----------
  // 每检测到一个"高峰"就切一张。间隔由音频自己决定，
  // 转场时长按"上一拍到现在"的间隔动态给，快拍时动画自动变短。
  let lastBeatAt = 0;
  audio.onBeat = (t) => {
    pulseBeat();
    if (!S.playing) return;
    const gap = lastBeatAt ? (t - lastBeatAt) : 420;
    lastBeatAt = t;
    // 自适应式"取下一张"：推进索引 + 转场
    const dir = S.forward ? 1 : -1;
    let next = S.idx + dir;
    if (next < 0 || next >= S.items.length) {
      if (next >= S.items.length && S.items.length < S.total) { maybeExtend(); return; }
      next = next < 0 ? S.items.length - 1 : 0;
    }
    const nk = ckey(S.items[next].file);
    const isVid = /\.(mp4|webm|mov|mkv|avi|gif)$/i.test(S.items[next].file || "");
    if (!isVid && !cache.has(nk)) {
      preload(S.items[next].file);
      // 图没就绪就跳过这一拍，保证动画不卡（等图会把节奏拖垮）
      return;
    }
    S.idx = next;
    show(Math.max(120, Math.min(900, gap)));   // 转场时长跟拍子走
    warm();
    maybeExtend();
  };

  /** 启动/停止听音。音频采集在 Python 侧（WASAPI 回环），
   *  这里只调后端接口 —— 前端**不碰任何音频权限**，所以不会再崩。 */
  async function toggleAudio(force) {
    const btn = $("btnAudioOn");
    const on = force != null ? force : !audioReady;
    const nm = { wasapi: "系统声音 ✅（回环）", mic: "麦克风 ⚠️" };
    if (on) {
      btn.disabled = true;
      btn.textContent = "启动中…";
      $("aInfo").textContent = "正在连接音频…";
      try {
        const mode = await audio.start();
        audioReady = true;
        btn.classList.add("on");
        btn.textContent = "停止听音";
        const dev = audio.device ? ` · ${audio.device.slice(0, 22)}` : "";
        $("aInfo").textContent = (nm[mode] || mode) + dev;
      } catch (e) {
        audioReady = false;
        try { audio.stop(); } catch (e2) {}
        btn.classList.remove("on");
        btn.textContent = "重试";
        $("aInfo").textContent = "无法启动：" + String(e && (e.message || e)).slice(0, 30);
      } finally {
        btn.disabled = false;
      }
    } else {
      audio.stop();
      audioReady = false;
      btn.classList.remove("on");
      btn.textContent = "开始听";
      $("aInfo").textContent = "已停止";
      $("aBpm").textContent = "";
    }
    return audioReady;
  }

  // 电平表 + BPM 实时刷新
  setInterval(() => {
    if (S.sMode !== "audio") return;
    const lvl = $("aLevel"), th = $("aThresh");
    if (!audioReady) { lvl.style.width = "0%"; th.style.left = "0%"; return; }
    // 后端给的是 dB（-60..0 归一到 0..100%）
    const ldb = audio.levelDb != null ? audio.levelDb : -60;
    const td = audio.threshDb != null ? audio.threshDb : -60;
    const pct = Math.max(0, Math.min(100, ((ldb + 60) / 60) * 100));
    const tpc = Math.max(0, Math.min(100, ((td + 60) / 60) * 100));
    lvl.style.width = pct + "%";
    th.style.left = tpc + "%";
    // 电平撞过阈值线时高亮一下，一眼看出"这一下会切图"
    lvl.classList.toggle("hot", ldb > td);
    const bpm = audio.getBpm();
    $("aBpm").textContent = bpm ? `≈${bpm} BPM` : "";
  }, 60);

  function loop(ts) {
    if (!S.playing) return;
    if (!lastFrame) lastFrame = ts;

    // 音频模式不走定时器 —— 由 audio.onBeat 在检测到鼓点时直接切图。
    // 这样每一个鼓点都能对上，不会有"轮询间隔"造成的漂移。
    if (S.sMode === "audio") {
      rafId = requestAnimationFrame(loop);
      return;
    }

    // 自适应模式：按节奏计划的间隔走；其它模式：按固定间隔
    if (ts >= nextAt) {
      const gap = (S.sMode === "adaptive")
        ? (plan()[beatIdx % plan().length] || 500)
        : (1000 / Math.max(0.5, S.speed));

      // 先推进索引，再交 show() 渲染 —— show() 画的是 curItem()，
      // 不先推进就会一直重画同一张。
      const dir = S.forward ? 1 : -1;
      let next = S.idx + dir;
      if (next < 0 || next >= S.items.length) {
        // 到头：若还有未加载的批次，先取；否则循环回另一端
        if (next >= S.items.length && S.items.length < S.total) {
          maybeExtend();
          nextAt = ts + 120;
          rafId = requestAnimationFrame(loop);
          return;
        }
        next = next < 0 ? S.items.length - 1 : 0;
      }
      // 下一张没解码好就等一小会儿（最多 3 次），避免冲过去只剩空白。
      // 但不死等 —— 死等会让节奏被拖垮，反而不好看。
      const nk = ckey(S.items[next].file);
      const isVid = /\.(mp4|webm|mov|mkv|avi|gif)$/i.test(S.items[next].file || "");
      if (!isVid && !cache.has(nk) && waiting < 3) {
        waiting++;
        preload(S.items[next].file);
        nextAt = ts + Math.min(120, gap * 0.5);
        rafId = requestAnimationFrame(loop);
        return;
      }
      waiting = 0;
      S.idx = next;
      show(gap);
      warm();
      maybeExtend();          // 快播到尾部时后台续上下一批
      beatIdx++;
      nextAt = ts + gap;
    }
    rafId = requestAnimationFrame(loop);
  }

  function play() {
    if (!S.items.length) return;
    S.playing = true;
    $("btnPlay").textContent = "⏸";
    // 立即就绪：nextAt 设为当前时刻，第一帧就切图（不等一个间隔）
    nextAt = performance.now();
    lastFrame = 0;
    if (S.sMode === "adaptive") beatIdx = 0;
    cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(loop);
    showUI();
  }
  function pause() {
    S.playing = false;
    $("btnPlay").textContent = "▶";
    cancelAnimationFrame(rafId);
  }
  const toggle = () => (S.playing ? pause() : play());

  // ---------- 翻页 ----------
  function step(delta, viaKey) {
    if (!S.items.length) return;
    // 自适应记录：无论播放与否都记（用户要求播放中也记）
    if (S.sMode === "adaptive" && viaKey) {
      const r = rhythm.feed(performance.now());
      flashBeats();
      updateAdvInfo();
      if (!S.playing && r.ready) {
        // 攒够次数 → 自动开始播，且第一拍就接上（不等整拍）
        play();
        return;
      }
      if (!S.playing) {
        // 还没到阈值：只翻一张，让用户看到反馈
      }
    }
    const dir = S.forward ? 1 : -1;
    const next = S.idx + delta * dir;
    if (next < 0 || next >= S.items.length) {
      // 手动翻到尾部：若还有没取的批次就先取，下次按键就能继续
      if (next >= S.items.length && S.items.length < S.total) {
        maybeExtend().then(() => {
          if (S.idx + delta * dir < S.items.length) step(delta, false);
        });
      }
      return;
    }
    S.idx = next;
    const gap = S.sMode === "adaptive"
      ? (plan()[beatIdx % plan().length] || 500)
      : (1000 / Math.max(0.5, S.speed));
    show(gap, delta);
    warm();
    maybeExtend();
  }

  // ---------- 自适应 UI ----------
  function flashBeats() {
    const box = $("beats");
    if (box.children.length !== 16) {
      box.innerHTML = "";
      for (let i = 0; i < 16; i++) {
        const d = document.createElement("div"); d.className = "beat"; box.appendChild(d);
      }
    }
    const n = rhythm.intervals.length;
    [...box.children].forEach((el, i) => {
      el.classList.toggle("hit", i < n);
      el.classList.toggle("past", i >= n && i < rhythm.threshold);
    });
  }
  function updateAdvInfo() {
    const st = rhythm.stats();
    $("advInfo").textContent = st.count
      ? `${st.count} 次 · 均值 ${st.avg}ms · 周期 ${st.cycleLen} 拍`
      : "等待打节奏…";
  }

  // ---------- 设置联动 ----------
  function applySpeedUI() {
    const isAdaptive = S.sMode === "adaptive";
    const isAudio = S.sMode === "audio";
    $("adaptive").classList.toggle("on", isAdaptive);
    $("audiobar").style.display = isAudio ? "" : "none";
    document.querySelectorAll("#sMode button").forEach(b =>
      b.classList.toggle("on", b.dataset.mode === S.sMode));
    // 音频栏里的阈值模式按钮也要同步高亮
    applyThrModeUI();

    const isFps = S.sMode === "fps";
    const isSpf = S.sMode === "spf";
    $("numFps").style.display = (isFps || isSpf) ? "" : "none";
    $("rngFps").style.display = (isFps || isSpf) ? "" : "none";
    $("uFps").style.display = (isFps || isSpf) ? "" : "none";

    if (isFps) {
      $("numFps").value = S.fps; $("rngFps").value = S.fps;
      $("numFps").max = 60; $("rngFps").max = 60;
      $("uFps").textContent = "张/秒";
      S.speed = S.fps;
    } else if (isSpf) {
      $("numFps").value = S.spf; $("rngFps").value = S.spf;
      $("numFps").max = 60; $("rngFps").max = 60;
      $("numFps").min = 0.05; $("rngFps").min = 0.05;
      $("uFps").textContent = "秒/张";
      S.speed = 1 / Math.max(0.05, S.spf);
    } else {
      S.speed = 8;
    }
    updateAdvInfo();
    flashBeats();
    warm();
  }

  function applyListUI() {
    $("src").value = S.src;
    $("sort").value = S.sort;
    $("trans").value = S.trans;
  }

  // ---------- 事件绑定 ----------
  $("src").onchange = e => { S.src = e.target.value; save("src", S.src); loadList(false); };
  $("sort").onchange = e => { S.sort = e.target.value; save("sort", S.sort); loadList(true); };
  $("trans").onchange = e => { S.trans = e.target.value; save("trans", S.trans); };
  $("btnOrder").onclick = () => {
    S.forward = !S.forward; save("fwd", S.forward ? "1" : "0");
    $("btnOrder").style.transform = S.forward ? "" : "rotate(180deg)";
  };

  $("btnPlay").onclick = toggle;
  $("btnPrev").onclick = () => step(-1);
  $("btnNext").onclick = () => step(1);

  document.querySelectorAll("#sMode button").forEach(b => {
    b.onclick = () => {
      S.sMode = b.dataset.mode; save("sMode", S.sMode);
      rhythm.reset(); beatIdx = 0;
      if (S.playing) pause();
      applySpeedUI();
      // ⚠️ 不要在这里自动调 getUserMedia：
      //    非用户手势下请求音频权限，可能弹出用户看不见的模态框，
      //    把整个渲染进程卡死。音频由用户显式点「开始听」启动。
      if (S.sMode !== "audio" && audioReady) toggleAudio(false);
    };
  });

  $("btnAudioOn").onclick = () => toggleAudio();

  // 阈值模式：动态（跟随基线）/ 固定（绝对 dB）
  document.querySelectorAll("#thrMode button").forEach(b => {
    b.onclick = async () => {
      const m = b.dataset.mode;
      S.thrMode = m; save("thrMode", m);
      // 不管有没有在听音都把设置同步给后端 —— 原来只在 audioReady 时同步，
      // 导致"先选固定、后点开始听"的情况下后端还是 dynamic。
      try {
        await audio.setThrMode(m);
      } catch (e) {}
      applyThrModeUI();
    };
  });

  function applyThrModeUI() {
    const m = S.thrMode || "dynamic";
    document.querySelectorAll("#thrMode button").forEach(b =>
      b.classList.toggle("on", b.dataset.mode === m));
    const fixed = m === "fixed";
    const sl = $("aSens");

    // 两种模式的滑块含义完全不同，所以连量程一起换：
    //   动态：1..50 灵敏度（往右更灵敏）
    //   固定：-60..-3 dB 绝对门槛（往右门槛更高 = 更严）
    if (fixed) {
      $("aSensLabel").textContent = "门槛";
      sl.min = "-60"; sl.max = "-3"; sl.step = "0.5";
      sl.value = String(S.aFixedDb);
      $("aSensVal").textContent = fmtDb(S.aFixedDb);
      sl.title = "固定阈值：绝对 dB 门槛。往右 = 门槛更高 = 只有更响的拍子才切";
    } else {
      $("aSensLabel").textContent = "灵敏度";
      sl.min = "1"; sl.max = "50"; sl.step = "1";
      sl.value = String(S.aSens);
      $("aSensVal").textContent = String(S.aSens);
      sl.title = "动态阈值：比当前基线高多少 dB 才算一拍。往右 = 更灵敏";
    }
    const mt = document.querySelector(".meter");
    if (mt) mt.classList.toggle("fixed", fixed);
    syncSliderFill();
  }

  function fmtDb(v) { return (v > 0 ? "+" : "") + Number(v).toFixed(1); }

  /** 滑块已填充部分的比例（给 CSS 的 --fill 用，做出"进度条"手感）。 */
  function syncSliderFill() {
    const sl = $("aSens");
    if (!sl) return;
    const mn = parseFloat(sl.min), mx = parseFloat(sl.max);
    const v = parseFloat(sl.value);
    const pct = (mx > mn) ? ((v - mn) / (mx - mn)) * 100 : 0;
    sl.style.setProperty("--fill", Math.max(0, Math.min(100, pct)).toFixed(1) + "%");
  }

  /** 一拍点亮：整条播放条轻微呼吸 + 电平表发光。 */
  let _beatT1 = 0, _beatT2 = 0, _barEl = null;
  function pulseBeat() {
    if (!_barEl) _barEl = $("bar");
    const mt = document.querySelector(".meter");
    const now = performance.now();
    // 太密时不重复触发（44ms ≈ 22拍/秒以上的极快节奏就只保一次）
    if (now - _beatT1 < 44) return;
    _beatT1 = now;
    if (_barEl) {
      _barEl.classList.remove("beat");
      void _barEl.offsetWidth;        // 强制重排，让动画能连续重放
      _barEl.classList.add("beat");
    }
    if (mt) {
      mt.classList.remove("beat");
      void mt.offsetWidth;
      mt.classList.add("beat");
      clearTimeout(_beatT2);
      _beatT2 = setTimeout(() => mt.classList.remove("beat"), 160);
    }
  }

  // 启动时把上次保存的阈值模式同步给后端（否则后端一直用默认 dynamic）
  (async () => {
    try { await audio.setThrMode(S.thrMode || "dynamic"); } catch (e) {}
    try { await audio.setFixedDb(S.aFixedDb); } catch (e) {}
    try { await audio.setSensitivity(S.aSens); } catch (e) {}
  })();

  // ⚠️ 这里原来有个「① 授权音频」按钮，会在页面里调 getUserMedia。
  //    实测 WebView2 会因此**杀掉渲染进程**（RESULT_CODE_KILLED_BAD_MESSAGE），
  //    所以整个权限流程已删除 —— 采集搬到 Python 侧（WASAPI 回环），
  //    前端一个音频 API 都不碰，也就不会再崩、不会弹麦克风权限。
  $("aSens").oninput = () => {
    if ((S.thrMode || "dynamic") === "fixed") {
      const v = Math.max(-60, Math.min(-3, parseFloat($("aSens").value)));
      if (!isFinite(v)) return;
      S.aFixedDb = v; save("aFixedDb", v);
      audio.setFixedDb(v);
      $("aSensVal").textContent = fmtDb(v);
      syncSliderFill();
      return;
    }
    const v = Math.max(1, Math.min(50, parseInt($("aSens").value || "5", 10)));
    S.aSens = v; save("aSens", v);
    audio.setSensitivity(v);          // 通知后端调整判定阈值
    $("aSensVal").textContent = v;
    syncSliderFill();
  };
  document.querySelectorAll("#rMode button").forEach(b => {
    b.onclick = () => {
      S.rMode = b.dataset.mode; save("rMode", S.rMode);
      rhythm.mode = S.rMode; rhythm.dirty = true;
      document.querySelectorAll("#rMode button").forEach(x =>
        x.classList.toggle("on", x.dataset.mode === S.rMode));
      updateAdvInfo();
    };
  });

  function bindSpeedInput(el) {
    el.addEventListener("input", () => {
      const v = parseFloat(el.value);
      if (!isFinite(v) || v <= 0) return;
      if (S.sMode === "fps") { S.fps = Math.min(60, v); save("fps", S.fps); }
      else if (S.sMode === "spf") { S.spf = Math.min(60, v); save("spf", S.spf); }
      applySpeedUI();
    });
  }
  bindSpeedInput($("numFps"));
  bindSpeedInput($("rngFps"));

  $("advThr").oninput = () => {
    const v = Math.max(2, Math.min(16, parseInt($("advThr").value || "3", 10)));
    S.threshold = v; save("thr", v);
    rhythm.threshold = v; rhythm.reset();
    $("advNeed").textContent = v;
    updateAdvInfo(); flashBeats();
  };

  // 全屏
  $("btnFull").onclick = toggleFullscreen;
  function toggleFullscreen() {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen().catch(() => {});
    else document.exitFullscreen().catch(() => {});
  }
  // 关闭：嵌入在软件里时通知父窗口收起覆盖层；独立打开才关窗口
  function closeSlide() {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage("slide-close", "*");
        return;
      }
    } catch (e) {}
    try { window.close(); } catch (e) {}
  }
  window.__closeSlide = closeSlide;
  $("btnClose").onclick = closeSlide;

  // ---------- 防止方向键被顶部控件吃掉 ----------
  // 浏览器原生行为：焦点落在 <select> 上时，上下/左右键会**切换选项**。
  // 幻灯片里方向键是"上一张/下一张"，如果焦点跑到数据源下拉上，
  // 就成了"按键换数据源" —— 必须禁掉。
  // 做法：顶栏控件一律 tabindex="-1"（不可键盘聚焦），
  //      鼠标点击它们之后再主动把焦点还给 body。
  ["src", "sort", "trans", "btnOrder", "btnFull", "btnClose",
   "btnPlay", "btnPrev", "btnNext", "numFps", "rngFps", "advThr"].forEach(id => {
    const el = $(id);
    if (el) el.setAttribute("tabindex", "-1");
  });
  document.querySelectorAll(".seg button").forEach(b => b.setAttribute("tabindex", "-1"));

  // 任何控件被点/聚焦后，立刻把焦点丢掉（不影响它的点击功能）
  function blurSelf(e) {
    const t = e.target;
    if (!t || !t.blur) return;
    // 数值/区间输入要给用户留出连续调整的时间，不立即 blur
    if (t.id === "numFps" || t.id === "rngFps" || t.id === "advThr") {
      setTimeout(() => { if (document.activeElement === t) t.blur(); }, 1200);
      return;
    }
    setTimeout(() => t.blur(), 0);
  }
  document.addEventListener("focusin", blurSelf, true);

  // 兜底：方向键/空格按下时，若焦点还在某个控件上，先夺回焦点再执行
  document.addEventListener("keydown", e => {
    const t = e.target;
    const tag = (t.tagName || "").toLowerCase();
    const isEditing = tag === "input" &&
      (t.id === "numFps" || t.id === "rngFps" || t.id === "advThr");
    if (isEditing) return;                  // 正在编辑数值，放行
    if (["ArrowUp","ArrowDown","ArrowLeft","ArrowRight"," ","Spacebar"].includes(e.key)) {
      if (document.activeElement && document.activeElement !== document.body) {
        if (document.activeElement.blur) document.activeElement.blur();
      }
    }
  }, true);

  // 再兜一层：直接拦住 select 的方向键换项（Win 上某些情况下 tabindex 挡不住）
  ["src", "sort", "trans"].forEach(id => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("keydown", ev => {
      if (ev.key.indexOf("Arrow") === 0 || ev.key === " " || ev.key === "Home" ||
          ev.key === "End" || ev.key === "PageUp" || ev.key === "PageDown") {
        ev.preventDefault();
        ev.stopPropagation();
        el.blur();
      }
    }, true);
    // 鼠标滚轮也别改值（容易误触）
    el.addEventListener("wheel", ev => { ev.preventDefault(); el.blur(); }, { passive: false });
  });

  // 键盘
  document.addEventListener("keydown", e => {
    const t = e.target;
    const tag = (t.tagName || "").toLowerCase();
    // 只在"正在输入数值"时让步，其余一律由幻灯片接管。
    // （原来是 input/select 一律 return —— 于是焦点落在下拉上时，
    //   方向键变成了切换数据源。）
    if (tag === "input" &&
        (t.id === "numFps" || t.id === "rngFps" || t.id === "advThr")) return;
    switch (e.key) {
      case " ":
        e.preventDefault();
        // 自适应：空格在没播放时也算"打拍"
        if (S.sMode === "adaptive" && !S.playing) { step(0, true); }
        else toggle();
        break;
      case "ArrowLeft":  e.preventDefault(); step(-1, true); break;
      case "ArrowRight": e.preventDefault(); step(1, true); break;
      case "ArrowUp":    e.preventDefault(); step(-1, true); break;
      case "ArrowDown":  e.preventDefault(); step(1, true); break;
      case "f": case "F": toggleFullscreen(); break;
      case "Escape":
        if (document.fullscreenElement) document.exitFullscreen();
        else closeSlide();
        break;
      case "Home": S.idx = 0; render(); warm(); break;
      case "End": S.idx = Math.max(0, S.items.length - 1); render(); warm(); break;
    }
    showHint();
  });

  // 鼠标静止 2 秒隐藏 UI
  let hideTimer = 0;
  function showHint() {
    const h = $("fsHint");
    h.classList.add("show");
    clearTimeout(showHint._t);
    showHint._t = setTimeout(() => h.classList.remove("show"), 1600);
    showUI();
  }
  function showUI() {
    // ⚠️ 隐藏态要挂在 <body> 上：#stage 和 #bar 是**兄弟节点**，
    //    原来用 `#stage.hideui #bar` 这个后代选择器永远匹配不到，
    //    所以"自动隐藏"一直没生效（实测 bar 的 opacity 恒为 1）。
    document.body.classList.remove("hideui");
    $("stage").classList.remove("hideui");
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (S.playing) {
        document.body.classList.add("hideui");
        $("stage").classList.add("hideui");   // 同时保留：用来隐藏鼠标指针
      }
    }, 2000);
  }
  document.addEventListener("mousemove", showUI);
  document.addEventListener("mousedown", showUI);
  // 键盘操作也算"有活动"，别一边按键一边黑着
  document.addEventListener("keydown", showUI);
  // 鼠标移出窗口 / 触摸屏：直接进隐藏态，不用等 2 秒
  document.addEventListener("mouseleave", () => {
    if (S.playing) { document.body.classList.add("hideui"); $("stage").classList.add("hideui"); }
  });

  // 每秒刷新一次实时速度显示
  setInterval(() => {
    const sp = S.sMode === "adaptive" ? (1000 / (plan()[beatIdx % plan().length] || 500)) : S.speed;
    $("showFps").textContent = S.playing ? `${sp.toFixed(1)} 张/秒` : "";
  }, 500);

  // ---------- 启动 ----------
  async function boot() {
    // URL 参数优先（从图库「其他功能」跳过来时带的数据源/排序）
    try {
      const u = new URLSearchParams(location.search);
      if (u.get("src")) { S.src = u.get("src"); save("src", S.src); }
      if (u.get("sort")) { S.sort = u.get("sort"); save("sort", S.sort); }
      S.embed = u.get("embed") === "1";
    } catch (e) {}
    applyListUI();
    applySpeedUI();
    document.querySelectorAll("#rMode button").forEach(x =>
      x.classList.toggle("on", x.dataset.mode === S.rMode));
    $("advThr").value = S.threshold;
    $("advNeed").textContent = S.threshold;
    $("btnOrder").style.transform = S.forward ? "" : "rotate(180deg)";
    flashBeats();
    await loadList(false);
    // 焦点**不要**落在控件上（否则方向键会被下拉吃掉）。
    // 焦点给 body，键盘事件才能被幻灯片接管。
    try { document.activeElement && document.activeElement.blur(); document.body.focus(); } catch (e) {}
    showUI();
  }
  boot();
})();
