/*
 * Gallery4ComfyUI —— 幻灯片节奏引擎（纯逻辑，无 DOM 依赖）

设计目标：**极快**。所有运算都在微秒级，不能卡住 UI 线程。

核心 API：
  Rhythm.feed(t)        —— 记录一次按键（t 为 performance.now() 毫秒）
  Rhythm.plan()         —— 算出播放用的间隔序列（毫秒）
  Rhythm.mode           —— mean | real | approx
  Rhythm.threshold      —— 需要按几次才开始播
  Rhythm.gapReset       —— 超过这个间隔就重新记录（默认 2000ms）

三种模式：
  mean   取平均间隔，每拍一样（抹平手抖）
  real   按记录序列循环（保留轻重拍）
  approx 把没打完的周期补成整周期再循环（只有这个做周期识别）

 * 周期识别用扫描找最小重复单元，支持"末尾不完整"的周期。
 */
(function (root) {
  "use strict";

  function q(v) { return Math.round(v); }          // 量化到 1ms，避免浮点误差

  /**
   * 找最小重复单元。
   *
   * 难点在于：用户按的序列**两头都可能不完整**（开头可能是在上个小节中间按的，
   * 末尾可能没按完一个周期）。所以不能只看数组开头，也不能只看末尾。
   *
   * 做法：对每个候选周期长度 p，滑动所有起点 s，用"这个窗口当周期"去解释**整段**
   * 数组，取解释得最好的那个 (p, s)。评价标准 = 全数组按周期展开后的匹配率，
   * 并轻微偏好更长的周期（长的保留重音，短的会退化成"每拍一样"）。
   *
   * @param {number[]} arr 间隔数组（毫秒）
   * @param {number} tol   容差（相对值，如 0.05 = ±5%）
   * @returns {number[]|null} 周期数组，找不到返回 null
   */
  function findPeriod(arr, tol) {
    const n = arr.length;
    if (n < 4) return null;

    // 每拍都一样 → 周期就是单拍（不用走滑窗）
    let allSame = true;
    const mean0 = arr.reduce((a, b) => a + b, 0) / n;
    for (let i = 0; i < n; i++) {
      if (Math.abs(arr[i] - mean0) > Math.max(2, mean0 * tol)) { allSame = false; break; }
    }
    if (allSame) return [mean0];

    let best = null, bestScore = -1, bestScore0 = 0, bestP = 0;
    for (let p = 2; p <= (n >> 1); p++) {
      for (let s = 0; s + p <= n; s++) {
        let ok = 0, total = 0;
        for (let i = 0; i < n; i++) {
          if (i >= s && i < s + p) continue;        // 自己跟自己不比
          const a = arr[i], b = arr[s + (((i - s) % p) + p) % p];
          const lim = Math.max(1, Math.max(a, b) * tol);
          total++;
          if (Math.abs(a - b) <= lim) ok++;
        }
        if (!total) continue;
        const score = ok / total;
        // 覆盖度惩罚：周期 p 要有"足够多的对照点"才可信。
        // n=7 时 p=4 只有 3 个对照点，容易蒙对；p=3 有 4 个更可信。
        // 用 total/n 当权重，避免短数组里长周期靠运气胜出。
        const cover = total / n;
        const adj = score * (0.75 + 0.25 * cover);
        // 偏好更长的周期（保留重音）；分数接近（0.8% 内）时选长的
        if (adj > bestScore + 0.008 || (adj > bestScore - 0.008 && p > bestP)) {
          bestScore = adj; bestScore0 = score; best = arr.slice(s, s + p); bestP = p;
        }
      }
    }
    // 至少要解释得了 80% 才算"认出了周期"
    return (best && bestScore0 >= 0.8) ? best : null;
  }

  /**
   * 近似补全：在"能验证出周期"时给出精简周期，否则保留原始序列。
   *
   * 重要：只有当记录长度**至少覆盖一个完整周期再加几拍**时，周期才可被验证。
   * 例如 [500,500,500,1000,500]（5 个间隔）—— 任何 5 拍周期都能完美解释它，
   * 硬凑出 [500,500,500,1000] 是猜的，不如老实按原序列放（等同于"真实值"）。
   * 所以这里要求 n >= p + 2（至少多 2 拍对照点），否则不认周期。
   */
  function findPeriodApprox(arr) {
    const n = arr.length;
    const tols = [0.03, 0.06, 0.10, 0.18, 0.30, 0.45];
    for (let i = 0; i < tols.length; i++) {
      const p = findPeriod(arr, tols[i]);
      if (!p || !p.length) continue;
      // 校验周期是否可信：
      //   ① 有多余对照点（n >= p + 2）—— 否则是猜的，不如原样放；
      //   ② 不是"退化的 1 拍"——除非真的每拍都一样（那种情况 findPeriod 已提前返回）。
      if (n >= p.length + 2) return p;
    }
    // 认不出周期 → 原样循环（等同真实值，至少不会放错）
    return arr.slice();
  }

  function meanOf(arr) {
    if (!arr.length) return 0;
    let s = 0;
    for (let i = 0; i < arr.length; i++) s += arr[i];
    return s / arr.length;
  }

  function Rhythm(opts) {
    opts = opts || {};
    this.mode = opts.mode || "mean";          // mean | real | approx
    this.threshold = opts.threshold || 3;     // 按几次开始播
    this.gapReset = opts.gapReset || 2000;    // 超过这个间隔就重记
    this.intervals = [];                      // 记录到的间隔（逆序存储便于取最近）
    this.lastT = 0;
    this.started = false;
    this.cycle = null;                        // 当前使用的周期（数组）
    this.cachedPlan = null;                   // 缓存：算好的播放间隔序列
    this.dirty = true;
  }

  // 记录一次按键；返回 {ok, count, ready, took}
  Rhythm.prototype.feed = function (t) {
    const t0 = (typeof performance !== "undefined" ? performance.now() : Date.now());
    const last = this.lastT;
    this.lastT = t;
    if (!last) return { count: this.intervals.length, ready: false, took: 0 };

    const d = t - last;
    if (d > this.gapReset) {
      // 间隔太久：重新记录
      this.intervals.length = 0;
      this.cachedPlan = null;
      this.dirty = true;
      return { count: 0, ready: false, reset: true, took: 0 };
    }
    if (d <= 0) return { count: this.intervals.length, ready: false, took: 0 };

    this.intervals.push(d);
    // 只保留最近 64 个，防止无限增长
    if (this.intervals.length > 64) this.intervals.shift();
    this.dirty = true;

    const took = (typeof performance !== "undefined" ? performance.now() : Date.now()) - t0;
    return { count: this.intervals.length, ready: this.intervals.length >= this.threshold, took: took };
  };

  // 重新开始记录（不影响已算好的 plan）
  Rhythm.prototype.reset = function () {
    this.intervals.length = 0;
    this.lastT = 0;
    this.dirty = true;
  };

  /**
   * 算出播放间隔序列（毫秒数组）。结果会缓存，只有记录变化时才重算。
   * @returns {number[]} 间隔数组，长度 >= 1
   */
  Rhythm.prototype.plan = function () {
    if (!this.dirty && this.cachedPlan) return this.cachedPlan;
    const iv = this.intervals;
    let out;
    if (!iv.length) {
      out = [1000];
    } else if (this.mode === "mean") {
      const m = meanOf(iv);
      out = [Math.max(30, m)];
    } else if (this.mode === "real") {
      out = iv.slice();
    } else {                                  // approx：近似补全
      out = findPeriodApprox(iv);
      if (!out || !out.length) out = iv.slice();
    }
    // 保底：不能小到 0，也不能大到离谱
    for (let i = 0; i < out.length; i++) {
      out[i] = Math.min(60000, Math.max(16, out[i]));
    }
    this.cachedPlan = out;
    this.dirty = false;
    return out;
  };

  // 已经攒够次数了吗
  Rhythm.prototype.ready = function () {
    return this.intervals.length >= this.threshold;
  };

  // 当前统计（给 UI 显示）
  Rhythm.prototype.stats = function () {
    const iv = this.intervals;
    return {
      count: iv.length,
      threshold: this.threshold,
      mode: this.mode,
      avg: iv.length ? Math.round(meanOf(iv)) : 0,
      cycleLen: (this.plan() || []).length,
      cycle: (this.plan() || []).slice(0, 16).map(q),
    };
  };

  /**
   * 把播放中用户新给的节奏"无缝并入"。
   * 做法：把新记录直接拼到当前记录尾部（保留重音位置），
   * 然后让 plan 重算 —— 下一拍起就用新节奏，不做任何等待。
   */
  Rhythm.prototype.mergeLive = function (t) {
    return this.feed(t);
  };

  const api = {
    Rhythm: Rhythm,
    findPeriod: findPeriod,
    findPeriodApprox: findPeriodApprox,
    meanOf: meanOf,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.SlideRhythm = api;
})(typeof window !== "undefined" ? window : globalThis);
