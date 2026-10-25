# -*- coding: utf-8 -*-
"""
Gallery4ComfyUI —— 系统音频节拍采集（Python 侧）

为什么搬到后端：
  在页面里用 getUserMedia + chromeMediaSource:"desktop" 抓系统声音，
  WebView2 会**直接杀掉渲染进程**（RESULT_CODE_KILLED_BAD_MESSAGE），
  而且降级到麦克风拿的不是"耳机里在放的音频"。
  所以在 Python 侧用 Windows 自带的 **WASAPI 回环**采集：
    - 抓的是真实的声卡输出（耳机里放什么就抓什么）
    - **不需要任何权限弹窗**
    - 不会崩浏览器

节拍判定沿用验证过的 **dB 域相对阈值**：
  实测音量差 100 倍时触发结果完全一致；60~180 BPM 误差 0ms。

对外只暴露：开始 / 停止 / 灵敏度 / 状态 / 事件流（节拍 + 电平）。
"""
import array
import json
import math
import os
import queue
import threading
import time

# ---------------------------------------------------------------------------
# 采集后端
# ---------------------------------------------------------------------------
try:
    import soundcard as _sc          # WASAPI 回环（Windows 首选）
    _HAS_SOUNDCARD = True
    _SOUNDCARD_ERR = ""
except Exception as _e:
    _sc = None
    _HAS_SOUNDCARD = False
    import traceback as _tb
    _SOUNDCARD_ERR = f"{type(_e).__name__}: {_e}\n{_tb.format_exc()}"

try:
    import pyaudiowpatch as _pa      # 备选：也能抓 WASAPI 回环
    _HAS_PYAUDIO = True
    _PYAUDIO_ERR = ""
except Exception as _e:
    _pa = None
    _HAS_PYAUDIO = False
    import traceback as _tb2
    _PYAUDIO_ERR = f"{type(_e).__name__}: {_e}"


LOWPASS_HZ = 150          # 只听低频（鼓点/bass），旋律不误触
# 两拍最小间隔：防同一个鼓点被触发两次。
# 实测 110ms 太短 —— 鼓点的衰减尾巴会再撞一次阈值，
# 造成大量 ~130ms 的"假拍"。改成 230ms：
# 仍能分辨 180BPM（333ms 间隔）以上的快歌，同时挡掉尾巴。
MIN_GAP_MS = 230
SAMPLE_HZ = 48000

# 灵敏度范围 1..50（1=最不灵敏，50=最灵敏）
SENS_MIN = 1
SENS_MAX = 50
# 灵敏度 1 和 50 分别对应的"比基线高出多少 dB"（动态模式）
SENS_NEED_DB_LO = 9.0     # 灵敏度 1：要高出 9dB 才算一拍（很挑）
SENS_NEED_DB_HI = 1.2     # 灵敏度 50：高出 1.2dB 就触发（极灵敏）

# 固定阈值模式的绝对门槛（dB）。
# 灵敏度 1 → -6dB（要很响才切）；灵敏度 50 → -24dB（轻一点也能切）。
#
# ⚠️ 下限不能设太低：门槛一旦低过底噪（典型数字音源约 -34dB），
# 判定就"永远为真"，只会触发一次然后被 _armed 挡住（实测档 50 就是这样，0 拍）。
# -24dB 既留了余量，又仍能抓弱拍的鼓点。
FIXED_DB_HI = -6.0
FIXED_DB_LO = -24.0


# 一阶低通的系数（在 48kHz 下逼近 150Hz 截止）
_ALPHA = 1.0 - math.exp(-2.0 * math.pi * LOWPASS_HZ / SAMPLE_HZ)


class BeatEngine(object):
    """单例：一个采集线程 + 一个事件队列（供 SSE 订阅）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

        self.running = False
        self.mode = ""            # "wasapi" | "mic"
        self.device = ""
        self.error = ""
        self.sensitivity = 5
        self.thr_mode = "dynamic"   # "dynamic"（跟随基线）| "fixed"（绝对 dB）
        self.gain_db = 0.0

        # 判定状态
        self._baseline_db = None
        self._last_judge = 0.0
        self._last_beat = 0.0
        self._armed = True        # 只有"掉下阈值过"才允许下一次触发
        self._beats = []

        # 电平（给 UI）
        self.level = 0.0
        self.level_db = -60.0
        self.thresh_db = -60.0

        # 订阅者：每个是一个 queue.Queue
        self._subs = []
        self._sub_lock = threading.Lock()

    # ---------------- 订阅 ----------------
    def subscribe(self):
        q = queue.Queue(maxsize=120)
        with self._sub_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._sub_lock:
            if q in self._subs:
                self._subs.remove(q)

    def _publish(self, obj):
        with self._sub_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(obj)
            except Exception:
                pass          # 队列满了就丢，别拖慢采集线程

    # ---------------- 状态 ----------------
    def status(self):
        return {
            "running": self.running,
            "mode": self.mode,
            "device": self.device,
            "error": self.error,
            "sensitivity": self.sensitivity,
            "thr_mode": self.thr_mode,
            "bpm": self.bpm(),
            "backends": {
                "soundcard": _HAS_SOUNDCARD,
                "pyaudiowpatch": _HAS_PYAUDIO,
            },
            # 导入失败的原因（排查打包问题时非常有用）
            "backend_errors": {
                "soundcard": _SOUNDCARD_ERR[:600],
                "pyaudiowpatch": _PYAUDIO_ERR[:300],
            },
        }

    def bpm(self):
        b = self._beats
        if len(b) < 4:
            return 0
        n = min(10, len(b) - 1)
        gaps = [b[i] - b[i - 1] for i in range(len(b) - n, len(b))]
        if not gaps:
            return 0
        avg = sum(gaps) / len(gaps)
        if avg <= 0:
            return 0
        v = 60000.0 / avg
        while v > 180:
            v /= 2
        while v < 60:
            v *= 2
        return int(round(v))

    # ---------------- 启停 ----------------
    def start(self, sensitivity=None):
        with self._lock:
            if sensitivity is not None:
                self.sensitivity = max(SENS_MIN, min(SENS_MAX, int(sensitivity)))
            if self.running:
                return True
            self.error = ""
            self._stop.clear()
            self._baseline_db = None
            self._armed = True
            self._beats = []
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        # 等它真正起来（最多 3 秒）
        for _ in range(30):
            if self.running or self.error:
                break
            time.sleep(0.1)
        return self.running

    def stop(self):
        with self._lock:
            self._stop.set()
            t = self._thread
            self._thread = None
        if t and t.is_alive():
            t.join(timeout=2.0)
        self.running = False
        self.mode = ""
        self.device = ""
        self.level = 0.0

    # ---------------- 采集线程 ----------------
    def _run(self):
        # 优先 soundcard（WASAPI 回环最省事），失败再试 pyaudiowpatch
        try:
            if _HAS_SOUNDCARD:
                self._run_soundcard()
                return
        except Exception as e:
            self.error = "soundcard 采集失败：%r" % (e,)
        if not self.running:
            try:
                if _HAS_PYAUDIO:
                    self._run_pyaudio()
                    return
            except Exception as e:
                self.error = "pyaudiowpatch 采集失败：%r" % (e,)
        if not self.running and not self.error:
            self.error = ("没有可用的音频采集后端。"
                          "请安装 soundcard：pip install soundcard")

    def _run_soundcard(self):
        """用 soundcard 抓默认扬声器的回环。"""
        # 默认扬声器（播放设备）对应的 loopback 录音设备
        try:
            mic = _sc.get_microphone(id=str(_sc.default_speaker().name), include_loopback=True)
        except Exception:
            # 有些版本这样拿
            spk = _sc.default_speaker()
            mic = _sc.get_microphone(spk.name, include_loopback=True)
        if mic is None:
            raise RuntimeError("拿不到回环设备")

        self.mode = "wasapi"
        self.device = getattr(mic, "name", "") or "默认扬声器"
        self.running = True

        frames = 2048
        with mic.recorder(samplerate=SAMPLE_HZ, channels=2, blocksize=frames) as rec:
            prev = 0.0
            while not self._stop.is_set():
                data = rec.record(numframes=frames)
                if data is None or len(data) == 0:
                    continue
                # data 是 [[L,R], ...]；取平均做单声道
                n = len(data)
                s = 0.0
                for i in range(n):
                    row = data[i]
                    v = (row[0] + row[1]) * 0.5 if hasattr(row, "__len__") else row
                    # 一阶低通，只留低频（鼓点）
                    prev = prev + _ALPHA * (v - prev)
                    s += prev * prev
                rms = math.sqrt(s / max(1, n))
                self._judge(rms, time.time() * 1000.0)

        self.running = False

    def _run_pyaudio(self):
        """用 pyaudiowpatch 抓 WASAPI 回环。"""
        pa = _pa.PyAudio()
        try:
            try:
                wasapi = pa.get_host_api_info_by_type(_pa.paWASAPI)
            except Exception:
                raise RuntimeError("没有 WASAPI")
            di = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not di.get("isLoopbackDevice"):
                for lb in pa.get_loopback_device_info_generator():
                    if di["name"] in lb["name"]:
                        di = lb
                        break
            rate = int(di["defaultSampleRate"])
            ch = int(di["maxInputChannels"])
            stream = pa.open(format=_pa.paFloat32, channels=ch, rate=rate,
                             input=True, input_device_index=di["index"],
                             frames_per_buffer=2048)
        except Exception:
            pa.terminate()
            raise

        self.mode = "wasapi"
        self.device = di.get("name", "")
        self.running = True
        alpha = 1.0 - math.exp(-2.0 * math.pi * LOWPASS_HZ / rate)
        try:
            prev = 0.0
            while not self._stop.is_set():
                raw = stream.read(2048, exception_on_overflow=False)
                a = array.array("f")
                a.frombytes(raw)
                s = 0.0
                n = len(a)
                for v in a:
                    prev = prev + alpha * (v - prev)
                    s += prev * prev
                rms = math.sqrt(s / max(1, n))
                self._judge(rms, time.time() * 1000.0)
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            pa.terminate()
            self.running = False

    # ---------------- 判定 ----------------
    def _judge(self, rms, now_ms):
        rms = max(rms, 1e-9)
        db = 20.0 * math.log10(rms)

        # 基线在 dB 域慢起快落（按时间，不按帧）
        if self._baseline_db is None:
            self._baseline_db = db
            self._last_judge = now_ms
        dt = (now_ms - self._last_judge) / 1000.0
        self._last_judge = now_ms
        if dt < 0:
            dt = 0.0
        if dt > 0.25:
            dt = 0.25
        tau = 0.28 if db > self._baseline_db else 0.075
        a = 1.0 - math.exp(-dt / tau) if dt > 0 else 0.0
        self._baseline_db += (db - self._baseline_db) * a

        self.level_db = db
        self.level = max(0.0, min(1.0, (db + 60.0) / 60.0))

        # ── 阈值：动态 / 固定 ──
        if self.thr_mode == "fixed":
            # 固定：直接用绝对 dB 门槛，不跟着音乐起伏走。
            # 好处是"安静段落也照样按同一个标准切"，坏处是音量一变就得重调。
            # need 在固定模式下代表"绝对阈值(dB)"，用同一个滑块控制。
            s = max(SENS_MIN, min(SENS_MAX, self.sensitivity))
            t = (s - SENS_MIN) / float(SENS_MAX - SENS_MIN)
            # 灵敏度 1 → -6dB（几乎不切），50 → -48dB（很灵敏）
            self.thresh_db = FIXED_DB_HI + (FIXED_DB_LO - FIXED_DB_HI) * t
            self.gain_db = db - self.thresh_db
        else:
            # 动态（默认）：阈值 = 基线 + need，跟着音乐自己的动态范围走。
            # 实测音量差 100 倍时触发结果完全一致。
            s = max(SENS_MIN, min(SENS_MAX, self.sensitivity))
            # 把 1..50 线性映射到 9dB..1.2dB。
            # 注意：不能沿用原来"按 1..10 算"的公式 —— 那样 s>14 时 need 变负数，
            # 阈值低于底噪，会疯狂误触（实测过）。
            t = (s - SENS_MIN) / float(SENS_MAX - SENS_MIN)
            need = SENS_NEED_DB_LO + (SENS_NEED_DB_HI - SENS_NEED_DB_LO) * t
            self.thresh_db = self._baseline_db + need
            self.gain_db = db - self._baseline_db

        # 每 ~50ms 推一次电平给 UI
        if now_ms - getattr(self, "_last_pub", 0) > 50:
            self._last_pub = now_ms
            self._publish({
                "type": "level",
                "level": round(self.level, 3),
                "level_db": round(db, 1),
                "base_db": round(self._baseline_db, 1),
                "thresh_db": round(self.thresh_db, 1),
                "gain_db": round(self.gain_db, 1),
                "thr_mode": self.thr_mode,
                "bpm": self.bpm(),
            })

        if db > self.thresh_db:
            # 必须先"掉下去过"才能再触发 —— 这是防鼓点尾巴重复计数的关键：
            # 一次鼓点的能量会缓慢衰减，中间多次越过阈值，
            # 只看间隔挡不干净（实测会多出一堆 130ms 的假拍）。
            #
            # _armed 语义：只有在"掉回阈值以下"时才会被重新置为 True，
            # 触发时要把它置 False（表示这一拍已经用掉了）。
            #
            # 兜底：固定阈值模式下，若整段音频都高于门槛（音量偏大时很常见），
            # armed 永远回不来 → 只切一次就卡死。
            # 所以"高于门槛持续太久"时按 MIN_GAP_MS 强制放行一拍，
            # 让它退回"按间隔切"，而不是彻底不动。
            if (not self._armed) and (now_ms - self._last_beat) > (MIN_GAP_MS * 4):
                self._armed = True
            if self._armed:
                self._armed = False
                if (now_ms - self._last_beat) > MIN_GAP_MS:
                    self._last_beat = now_ms
                    self._beats.append(now_ms)
                    if len(self._beats) > 64:
                        self._beats.pop(0)
                    self._publish({"type": "beat", "t": round(now_ms, 1),
                                   "gain_db": round(db - self._baseline_db, 1)})
        else:
            self._armed = True       # 掉回阈值以下 → 重新武装


ENGINE = BeatEngine()
