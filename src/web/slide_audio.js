/*
 * Gallery4ComfyUI —— 音频节拍引擎（客户端）
 *
 * ⚠️ 重要变更（2026-10-25）
 * 之前想用 getUserMedia + chromeMediaSource:"desktop" 在**页面里**抓系统声音。
 * 实测：WebView2 直接**杀掉渲染进程**（报 RESULT_CODE_KILLED_BAD_MESSAGE），
 * 而且降级到麦克风也不是用户要的"耳机里在放的音频"。
 * 所以改成：**音频采集全部搬到 Python 侧**（WASAPI 回环，无权限弹窗），
 * 前端只订阅后端推来的"节拍"事件，自己一个音频 API 都不碰。
 *
 * 这个模块负责：
 *   1. 连接后端的节拍流（SSE）
 *   2. 把节拍/电平/状态通过回调交给播放器
 *   3. 节拍到达时由播放器切图；若流断了自动重连
 */
(function (root) {
  "use strict";

  function AudioBeat(o) {
    o = o || {};
    this.sensitivity = o.sensitivity != null ? o.sensitivity : 5;
    this.fixedDb = o.fixedDb != null ? o.fixedDb : -30;
    this.thrMode = o.thrMode || "dynamic";       // dynamic | fixed
    this.url = o.url || "/gallery4comfyui/api/audio/stream";

    this.es = null;              // EventSource
    this.running = false;
    this.mode = "";              // "wasapi" | "mic" | ""
    this.device = "";
    this.level = 0;              // 0..1，给电平表
    this.levelDb = -60;          // 当前响度（dB，后端给）
    this.baseline = 0;           // 线性基线（后端给的 dB 换算）
    this.baselineDb = null;
    this.threshDb = null;
    this.bpm = 0;
    this.beats = [];             // 本地留最近的时间戳（算平均间隔用）
    this.lastBeat = 0;
    this.err = "";
    this.onBeat = null;          // (tMs) => void
    this.onState = null;         // (obj) => void
    this._retry = 0;
  }

  /**
   * 让**后端**开始采集（后端会自己选 WASAPI 回环，不行再退麦克风）。
   * 前端不请求任何权限，所以不会再崩。
   */
  AudioBeat.prototype.start = async function () {
    if (this.running) return this.mode;
    this.err = "";

    // 先让后端开采集
    let r;
    try {
      const res = await fetch(this.url.replace("/stream", "/start") +
        "?sensitivity=" + this.sensitivity, { method: "POST" });
      r = await res.json();
    } catch (e) {
      this.err = "无法连接音频服务：" + (e.message || e);
      throw new Error(this.err);
    }
    if (!r || !r.ok) {
      this.err = (r && r.error) || "后端无法开始采集";
      throw new Error(this.err);
    }
    this.mode = r.mode || "wasapi";
    this.device = r.device || "";

    this._open();
    this.running = true;
    return this.mode;
  };

  AudioBeat.prototype._open = function () {
    try { if (this.es) this.es.close(); } catch (e) {}
    const es = new EventSource(this.url);
    this.es = es;

    es.onmessage = (ev) => {
      let d;
      try { d = JSON.parse(ev.data); } catch (e) { return; }
      this._retry = 0;

      if (d.type === "level") {
        this.level = d.level != null ? d.level : 0;
        if (d.level_db != null) this.levelDb = d.level_db;
        if (d.base_db != null) {
          this.baselineDb = d.base_db;
          this.baseline = Math.pow(10, d.base_db / 20);
        }
        if (d.thresh_db != null) this.threshDb = d.thresh_db;
        if (d.bpm != null) this.bpm = d.bpm;
      } else if (d.type === "beat") {
        const t = performance.now();
        this.lastBeat = t;
        this.beats.push(t);
        if (this.beats.length > 64) this.beats.shift();
        if (this.onBeat) this.onBeat(t);
      } else if (d.type === "error") {
        this.err = d.error || "音频采集出错";
        if (this.onState) this.onState({ ok: false, error: this.err });
      }
    };

    es.onerror = () => {
      // 断了就重连（后端可能在切换设备）
      if (!this.running) return;
      this._retry++;
      if (this._retry > 8) {
        this.err = "节拍流断开，重连失败";
        if (this.onState) this.onState({ ok: false, error: this.err });
        return;
      }
      setTimeout(() => { if (this.running) this._open(); }, 800);
    };
  };

  /** 最近几次触发的平均间隔（毫秒）。0 = 数据不够。 */
  AudioBeat.prototype.avgGap = function (n) {
    n = n || 8;
    const b = this.beats;
    if (b.length < 3) return 0;
    const from = Math.max(1, b.length - n);
    let sum = 0, cnt = 0;
    for (let i = from; i < b.length; i++) { sum += b[i] - b[i - 1]; cnt++; }
    return cnt ? sum / cnt : 0;
  };

  /** BPM：优先用后端算的，没有就用本地平均间隔换算。 */
  AudioBeat.prototype.getBpm = function () {
    if (this.bpm) return this.bpm;
    const g = this.avgGap(10);
    if (!g) return 0;
    let bpm = 60000 / g;
    while (bpm > 180) bpm /= 2;
    while (bpm < 60) bpm *= 2;
    return Math.round(bpm);
  };

  AudioBeat.prototype.setSensitivity = async function (v) {
    this.sensitivity = v;
    try {
      await fetch(this.url.replace("/stream", "/sensitivity") + "?value=" + v,
        { method: "POST" });
    } catch (e) {}
  };

  /** 切换阈值模式：dynamic（跟随基线）/ fixed（绝对 dB）。 */
  AudioBeat.prototype.setThrMode = async function (m) {
    this.thrMode = m;
    try {
      await fetch(this.url.replace("/stream", "/thr-mode") + "?mode=" + m,
        { method: "POST" });
    } catch (e) {}
  };

  /** 固定模式的门槛（绝对 dB）。数值越大 = 门槛越高 = 越难触发。 */
  AudioBeat.prototype.setFixedDb = async function (v) {
    this.fixedDb = v;
    try {
      await fetch(this.url.replace("/stream", "/fixed-db") + "?value=" + v,
        { method: "POST" });
    } catch (e) {}
  };

  AudioBeat.prototype.stop = function () {
    this.running = false;
    try { if (this.es) this.es.close(); } catch (e) {}
    this.es = null;
    this.beats = [];
    this.level = 0;
    // 通知后端也可以停（释放声卡句柄）
    try {
      fetch(this.url.replace("/stream", "/stop"), { method: "POST" });
    } catch (e) {}
  };

  root.SlideAudio = { AudioBeat: AudioBeat };
  if (typeof module !== "undefined" && module.exports) module.exports = root.SlideAudio;
})(typeof window !== "undefined" ? window : globalThis);
