# -*- coding: utf-8 -*-
"""
Gallery4ComfyUI 独立桌面版 —— 本地服务

把 gallery_core 的全部能力用 aiohttp 暴露出来，跑在**独立的后台线程**里。
窗口(pywebview)通过 http://127.0.0.1:<port> 访问 —— 与 ComfyUI 完全无关：
它卡、它吃内存，都影响不到出图。
"""
import asyncio
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading

from aiohttp import web
from types import SimpleNamespace

import config as CFG

APP_DIR = CFG.APP_DIR
_cfg = CFG.load()

# gallery_core 的目录约定（必须在 import 之前设好）
os.environ["GALLERY4_USERDATA"] = os.path.join(APP_DIR, "userdata")
if _cfg.get("data_dir"):
    os.environ["GALLERY4_DATA_DIR"] = _cfg["data_dir"]

sys.path.insert(0, os.path.join(APP_DIR, "src"))
sys.path.insert(0, APP_DIR)

import gallery_core as G  # noqa: E402

P = "/gallery4comfyui"


def _web_dir():
    """前端页面目录。要能同时适应「源码运行」和「打包后的发布版」。

    ⚠️ 这里踩过一个坑：原来直接写 APP_DIR/src/web，而 APP_DIR 是 exe 所在目录。
    开发机上根目录**碰巧有 src/**（源码就在旁边），所以一直没暴露；
    但发布版只有 exe + _internal，用户打开就是
    「setup.html 缺失」——连主界面都进不去。

    查找顺序：
      1. exe 旁边的 src/web           （源码运行 / 开发时）
      2. _internal/src/web            （PyInstaller 打包后，实际在这儿）
      3. sys._MEIPASS/src/web         （onefile 模式的解包目录）
    """
    cands = []
    cands.append(os.path.join(APP_DIR, "src", "web"))
    cands.append(os.path.join(APP_DIR, "_internal", "src", "web"))
    mp = getattr(sys, "_MEIPASS", "")
    if mp:
        cands.append(os.path.join(mp, "src", "web"))
        cands.append(os.path.join(mp, "web"))
    # server.py 自己所在目录（最可靠：它就在 src/ 里）
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
    for c in cands:
        if os.path.isfile(os.path.join(c, "index.html")):
            return c
    return cands[0]


WEB_DIR = _web_dir()


# ---------------------------------------------------------------------------
# folder_paths 替身
# ---------------------------------------------------------------------------
def _models_dir():
    out = (_cfg.get("comfy_output") or "").rstrip("\\/")
    if os.path.basename(out).lower() == "output":
        cand = os.path.join(os.path.dirname(out), "models")
        if os.path.isdir(cand):
            return cand
    return ""


def install_folder_paths():
    G.folder_paths = SimpleNamespace(
        get_output_directory=lambda: (_cfg.get("comfy_output") or "").strip(),
        models_dir=_models_dir(),
    )


def log(msg):
    """写到软件目录的 gallery.log。定义必须在使用之前。"""
    import time as _t
    try:
        with open(os.path.join(APP_DIR, "gallery.log"), "a", encoding="utf-8") as f:
            f.write(f"[{_t.strftime('%Y-%m-%d %H:%M:%S')}] [server] {msg}\n")
    except Exception:
        pass


def sync_settings_to_core():
    """把 config.json 的 webui_root 同步进 gallery_core 的 userdata/settings.json。

    为什么需要：gallery_core 用 webui_outputs_dir() 找 WebUI 图片，它读的是
    userdata/settings.json；而独立版的目录配置在自己的 config.json。
    两边不通 → WebUI 源永远扫不到（实测：start_scan('webui') 返回"来源目录不可用"）。
    """
    try:
        wr = (_cfg.get("webui_root") or "").strip()
        cur = G.load_settings()
        if cur.get("webui_root") == wr:
            return
        if wr and os.path.isdir(wr):
            G.save_settings(wr)
            log(f"webui_root 已同步给 gallery_core: {wr!r}")
        elif not wr and cur.get("webui_root"):
            G.save_settings("")
            log("webui_root 已清空")
    except Exception as e:
        log(f"sync_settings_to_core 失败: {e!r}")


install_folder_paths()
sync_settings_to_core()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _int(v, d=None):
    try:
        return int(v) if v not in (None, "") else d
    except Exception:
        return d


def _float(v, d=None):
    try:
        return float(v) if v not in (None, "") else d
    except Exception:
        return d


def _json(o, status=200):
    return web.Response(text=json.dumps(o, ensure_ascii=False),
                        content_type="application/json", status=status)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _roots():
    out = [(_cfg.get("comfy_output") or "").strip(), (_cfg.get("webui_root") or "").strip()]
    r = []
    for p in out:
        if p and os.path.isdir(p):
            try:
                r.append(os.path.realpath(p))
            except Exception:
                pass
    return r


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
async def page(request):
    fp = os.path.join(WEB_DIR, "index.html")
    if not os.path.isfile(fp):
        return web.Response(text="index.html 缺失", status=500)
    return web.FileResponse(fp, headers={"Cache-Control": "no-store"})


async def setup_page(request):
    fp = os.path.join(WEB_DIR, "setup.html")
    if not os.path.isfile(fp):
        return web.Response(text="setup.html 缺失", status=500)
    return web.FileResponse(fp, headers={"Cache-Control": "no-store"})


async def slide_page(request):
    fp = os.path.join(WEB_DIR, "slide.html")
    if not os.path.isfile(fp):
        return web.Response(text="slide.html 缺失", status=500)
    return web.FileResponse(fp, headers={"Cache-Control": "no-store"})


async def static_file(request):
    """给幻灯片页提供 slide.js / slide_rhythm.js。只允许白名单文件名。"""
    name = os.path.basename(request.match_info.get("name", ""))
    if name not in ("slide.js", "slide_rhythm.js", "slide_audio.js", "icons.js"):
        return web.Response(status=404)
    fp = os.path.join(WEB_DIR, name)
    if not os.path.isfile(fp):
        return web.Response(status=404)
    ctype = "application/javascript" if name.endswith(".js") else "text/plain"
    return web.FileResponse(fp, headers={"Content-Type": ctype,
                                         "Cache-Control": "no-store"})


async def api_config(request):
    """给设置页读当前配置。"""
    c = CFG.load()
    return _json({"comfy_output": c.get("comfy_output", ""),
                  "webui_root": c.get("webui_root", ""),
                  "data_dir": c.get("data_dir", "")})


async def api_check_dir(request):
    """检查目录是否存在（设置页实时校验用）。"""
    try:
        data = await request.json()
        path = (data.get("path") or (data.get("args") or [""])[0] or "").strip()
    except Exception:
        path = ""
    if not path:
        return _json({"ok": False, "error": "空路径"})
    exists = os.path.isdir(path)
    kind = ""
    if exists:
        low = path.replace("/", "\\").lower()
        if low.rstrip("\\").endswith("\\output"):
            kind = "看起来是 ComfyUI 输出目录"
        elif os.path.isdir(os.path.join(path, "outputs")):
            kind = "看起来是 SD WebUI 根目录"
        else:
            kind = "目录存在"
        # 顺便数一下图片数量，确认选对了
        try:
            n = 0
            for dp, _, fns in os.walk(path):
                for f in fns:
                    if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm")):
                        n += 1
                if n > 5000:
                    break
            if n:
                kind += f"（约 {n} 个图片文件）"
        except Exception:
            pass
    return _json({"ok": exists, "exists": exists, "kind": kind})


async def api_save_dirs(request):
    """设置页保存目录（HTTP 回退路径，供浏览器调试用）。"""
    try:
        data = await request.json()
        if "args" in data and isinstance(data["args"], list):
            a = data["args"] + ["", "", ""]
            data = {"comfy_output": a[0], "webui_root": a[1], "data_dir": a[2]}
    except Exception:
        data = {}
    c = CFG.load()
    for k in ("comfy_output", "webui_root", "data_dir"):
        if k in data:
            c[k] = str(data[k] or "").strip()
    c["first_run"] = False
    CFG.save(c)
    globals()["_cfg"] = c
    install_folder_paths()
    sync_settings_to_core()          # ← webui_root 要同步给 gallery_core，否则扫不到 WebUI 图
    return _json({"ok": True})


async def api_boot(request):
    """页面打开时调一次：**增量**刷新（保留缓存、立刻可看图），后台补新增。"""
    try:
        started = []
        # refresh=True：内存/缓存已就绪时也起后台增量扫描，扫完自动替换。
        # 关键：gallery_core 现在会在扫描期间先把磁盘缓存装进内存（ready=True），
        # 所以用户打开就能看到原来的图，不用等扫描结束。
        for src in ("comfyui", "webui"):
            try:
                r = G.start_scan(src, refresh=True)
                if r and r.get("ok"):
                    started.append(src)
            except Exception as e:
                log(f"  start_scan({src}) 失败: {e!r}")
        # 视频源单独处理（目录是 output/video）
        out = (_cfg.get("comfy_output") or "").strip()
        vdir = os.path.join(out, "video") if out else ""
        if vdir and os.path.isdir(vdir):
            try:
                G.start_scan("video")
                started.append("video")
            except Exception as e:
                log(f"  start_scan(video) 失败: {e!r}")
        return _json({"ok": True, "started": started,
                      "comfyui": out, "webui": (_cfg.get("webui_root") or "").strip()})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


async def api_status(request):
    try:
        return _json({s: G.scan_status(s) for s in ("comfyui", "webui", "video")})
    except Exception as e:
        return _json({"error": str(e)})


async def api_scan(request):
    """强制重建 / 增量刷新某个源。

    注意 gallery_core.start_scan 的签名是 (source, force=False, refresh=False)，
    root/parser/exts 由它自己按 source 解析 —— 不要自己传目录（传了会错位成 force）。
    """
    q = request.query
    src = q.get("source", "comfyui")
    force = q.get("force", "0") == "1"
    refresh = q.get("refresh", "0") == "1"
    try:
        G.start_scan(src, force=force, refresh=refresh or not force)
        return _json({"ok": True})
    except Exception as e:
        return _json({"ok": False, "error": str(e)})


async def api_gallery(request):
    q = request.query
    try:
        r = G.query(
            q.get("source", "comfyui"),
            q=q.get("q", ""), model=q.get("model", ""), sampler=q.get("sampler", ""),
            lora=q.get("lora", ""),
            steps_min=_int(q.get("steps_min")), steps_max=_int(q.get("steps_max")),
            cfg_min=_float(q.get("cfg_min")), cfg_max=_float(q.get("cfg_max")),
            min_w=_int(q.get("min_w")), min_h=_int(q.get("min_h")),
            fav_only=q.get("fav", "0") == "1",
            sort=q.get("sort", "newest"),
            seed=q.get("seed", ""),
            page=_int(q.get("page"), 1),
            # 上限放宽到 50000：幻灯片要一次拿全部（否则只能看前 1000 张）。
            # 条目只是元数据，几 MB JSON 对本地服务无压力；真正吃内存的图片由前端预载窗口控制。
            page_size=min(_int(q.get("page_size"), 60) or 60, 50000),
            tags=q.get("tags", ""),
            artist=q.get("artist", ""),
            artist_min=_int(q.get("artist_min")),
        )
        return _json(r)
    except Exception as e:
        return _json({"error": str(e), "items": [], "total": 0, "ready": False})


async def api_image(request):
    src = request.query.get("source", "comfyui")
    rel = request.query.get("file", "")
    try:
        fp = G.image_path(src, rel)
    except Exception:
        fp = None
    if not fp or not os.path.isfile(fp):
        return web.Response(status=404)
    # 防目录穿越
    try:
        real = os.path.realpath(fp)
        roots = _roots()
        if roots and not any(real == r or real.startswith(r + os.sep) for r in roots):
            return web.Response(status=403)
    except Exception:
        return web.Response(status=403)
    ctype, _ = mimetypes.guess_type(fp)
    low = fp.lower()
    if low.endswith(".webm"):
        ctype = "video/webm"
    elif low.endswith(".mp4"):
        ctype = "video/mp4"
    return web.FileResponse(fp, headers={
        "Content-Type": ctype or "application/octet-stream",
        "Cache-Control": "max-age=86400",
    })


# ---------------------------------------------------------------------------
# 缩略图（幻灯片高速播放用）
# ---------------------------------------------------------------------------
# 60 张/秒时如果每次都解原图（2~3 MB），一秒要读 150 MB —— 必爆。
# 这里按需生成缩略图并缓存到磁盘，第二次直接命中。
_THUMB_LOCK = {}
_THUMB_DIR = None


def _thumb_dir():
    global _THUMB_DIR
    if _THUMB_DIR is None:
        d = os.path.join(APP_DIR, "userdata", "thumbs")
        os.makedirs(d, exist_ok=True)
        _THUMB_DIR = d
    return _THUMB_DIR


async def api_thumb(request):
    """按尺寸给缩略图。size=256/384/512。带磁盘缓存。"""
    import hashlib
    src = request.query.get("source", "comfyui")
    rel = request.query.get("file", "")
    try:
        size = int(request.query.get("size", "384"))
    except Exception:
        size = 384
    size = max(96, min(1024, size))

    try:
        fp = G.image_path(src, rel)
    except Exception:
        fp = None
    if not fp or not os.path.isfile(fp):
        return web.Response(status=404)
    # 防穿越
    try:
        real = os.path.realpath(fp)
        roots = _roots()
        if roots and not any(real == r or real.startswith(r + os.sep) for r in roots):
            return web.Response(status=403)
    except Exception:
        return web.Response(status=403)

    # 视频 / GIF 不走缩略图（要保留动态），直接给原文件
    if fp.lower().endswith((".webm", ".mp4", ".mov", ".mkv", ".avi", ".gif")):
        return await api_image(request)

    key = hashlib.sha1(f"{real}|{size}|{os.path.getmtime(fp)}|{os.path.getsize(fp)}"
                       .encode("utf-8")).hexdigest()
    out = os.path.join(_thumb_dir(), key[:2], key + ".jpg")
    if os.path.isfile(out):
        return web.FileResponse(out, headers={"Content-Type": "image/jpeg",
                                              "Cache-Control": "max-age=604800"})

    import asyncio
    loop = asyncio.get_event_loop()

    def make():
        try:
            from PIL import Image
            os.makedirs(os.path.dirname(out), exist_ok=True)
            im = Image.open(fp)
            im.draft("RGB", (size, size))          # JPEG 快速降采样（只对 JPEG 有效，但不亏）
            im = im.convert("RGB")
            im.thumbnail((size, size), Image.LANCZOS)
            tmp = out + ".tmp"
            im.save(tmp, "JPEG", quality=84, optimize=False)
            os.replace(tmp, out)
            return True
        except Exception as e:
            log(f"缩略图失败 {rel}: {e!r}")
            return False

    ok = await loop.run_in_executor(None, make)
    if not ok:
        # 生成失败就退回原图，别让播放器卡住
        return await api_image(request)
    return web.FileResponse(out, headers={"Content-Type": "image/jpeg",
                                          "Cache-Control": "max-age=604800"})


async def api_slide_meta(request):
    """幻灯片需要的轻量元信息：总数 + 当前源就绪状态。"""
    src = request.query.get("source", "comfyui")
    try:
        st = G.scan_status(src)
        return _json({"source": src, "total": st.get("total", 0),
                      "ready": st.get("ready", False), "busy": st.get("busy", False)})
    except Exception as e:
        return _json({"error": str(e)})


def _wraps(fn, kw=(), post=False):
    """把 query 参数按名字转成 fn 的实参。

    用**关键字**调用而非位置：函数签名顺序变了也不会错位。
    空字符串一律不传（否则覆盖函数默认值，比如 page="" 会炸）。
    数值型参数自动转 int（看函数默认值或参数名推断）。

    自保护：只传函数**签名里真的收**的参数。
    （曾经因为多传了 source 给 stats()，整个统计接口直接 500。）
    """
    import inspect as _ins
    try:
        sig = _ins.signature(fn)
        accepts_kwargs = any(p.kind == _ins.Parameter.VAR_KEYWORD
                             for p in sig.parameters.values())
        allowed = set(sig.parameters.keys())
        defaults = {n: p.default for n, p in sig.parameters.items()
                    if p.default is not _ins.Parameter.empty}
    except Exception:
        sig = None
        accepts_kwargs = True
        allowed = set()
        defaults = {}

    def _cast(name, raw):
        if raw == "" or raw is None:
            return None
        d = defaults.get(name)
        if isinstance(d, int) and not isinstance(d, bool):
            try:
                return int(raw)
            except Exception:
                return d
        if isinstance(d, float):
            try:
                return float(raw)
            except Exception:
                return d
        if isinstance(d, bool):
            return str(raw).lower() in ("1", "true", "yes", "on")
        # 默认值不是基本类型时，按参数名猜
        if name in ("page", "page_size", "min_posts", "limit", "artist_min"):
            try:
                return int(raw)
            except Exception:
                return None
        return raw

    async def h(request):
        try:
            if post:
                try:
                    src = await request.json()
                except Exception:
                    src = {}
            else:
                src = request.query
            kwargs = {}
            for k in kw:
                if not accepts_kwargs and k not in allowed:
                    continue                     # 函数不收这个参数，跳过
                v = _cast(k, src.get(k))
                if v is not None:
                    kwargs[k] = v
            return _json(fn(**kwargs))
        except Exception as e:
            # 写进日志，别让错误只藏在 JSON 里没人看得见
            try:
                log(f"接口出错 {getattr(fn, '__name__', fn)}: {e!r}")
            except Exception:
                pass
            return _json({"error": str(e)})
    return h


# ---------------------------------------------------------------------------
# 系统音频节拍（WASAPI 回环，Python 侧采集）
# ---------------------------------------------------------------------------
# 为什么不在页面里采：WebView2 遇到 getUserMedia + desktop 回环会
# **杀掉渲染进程**（RESULT_CODE_KILLED_BAD_MESSAGE），且降级麦克风拿的不是
# "耳机里在放的音频"。所以改到 Python 侧抓，前端只订阅节拍事件。
try:
    import audio_beat as _AB
except Exception as _e:                       # noqa: N816
    _AB = None
    log(f"音频节拍模块加载失败：{_e!r}")


async def api_audio_start(request):
    if _AB is None:
        return _json({"ok": False, "error": "音频模块不可用"}, status=500)
    try:
        sens = int(request.query.get("sensitivity") or 5)
    except Exception:
        sens = 5
    loop = asyncio.get_event_loop()

    def go():
        return _AB.ENGINE.start(sensitivity=sens)

    try:
        # run_in_executor 里的异常默认会被 aiohttp 吞成 500，必须自己抓
        ok = await loop.run_in_executor(None, go)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log(f"音频启动失败: {e!r}\n{tb}")
        return _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)
    st = _AB.ENGINE.status()
    if not ok:
        return _json({"ok": False, "error": st.get("error") or "无法开始采集",
                      **st}, status=500)
    return _json({"ok": True, **st})


async def api_audio_stop(request):
    if _AB is None:
        return _json({"ok": False})
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _AB.ENGINE.stop)
    return _json({"ok": True})


async def api_audio_status(request):
    if _AB is None:
        return _json({"running": False, "error": "音频模块不可用"})
    return _json(_AB.ENGINE.status())


async def api_audio_sensitivity(request):
    if _AB is None:
        return _json({"ok": False})
    try:
        v = int(request.query.get("value") or 5)
    except Exception:
        v = 5
    _AB.ENGINE.sensitivity = max(1, min(50, v))
    # 顺便支持在同一个接口上切换阈值模式
    m = (request.query.get("mode") or "").strip().lower()
    if m in ("dynamic", "fixed"):
        _AB.ENGINE.thr_mode = m
    return _json({"ok": True, "sensitivity": _AB.ENGINE.sensitivity,
                  "thr_mode": _AB.ENGINE.thr_mode})


async def api_audio_thr_mode(request):
    """切换阈值模式：dynamic（跟随基线）/ fixed（绝对 dB）。"""
    if _AB is None:
        return _json({"ok": False})
    m = (request.query.get("mode") or "dynamic").strip().lower()
    if m not in ("dynamic", "fixed"):
        m = "dynamic"
    _AB.ENGINE.thr_mode = m
    # 切换后清掉基线，让判定从新状态起步，避免拿旧基线算新阈值
    _AB.ENGINE._baseline_db = None
    return _json({"ok": True, "thr_mode": m})


async def api_audio_stream(request):
    """SSE：把节拍与电平推给前端。"""
    if _AB is None:
        return web.Response(status=503, text="audio unavailable")
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    q = _AB.ENGINE.subscribe()

    async def sender():
        # 先给一帧状态，让 UI 立刻有东西显示
        first = _AB.ENGINE.status()
        await resp.write(
            f"data: {json.dumps({'type': 'state', **first}, ensure_ascii=False)}\n\n".encode())
        while True:
            try:
                item = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: q.get(timeout=0.5))
            except Exception:
                item = None
            if item is not None:
                try:
                    await resp.write(
                        f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode())
                except Exception:
                    break
            else:
                # 心跳，防代理断流
                try:
                    await resp.write(b": ping\n\n")
                except Exception:
                    break
    try:
        await sender()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    finally:
        _AB.ENGINE.unsubscribe(q)
    return resp


def build_app():
    import traceback as _tb

    @web.middleware
    async def _err_mw(request, handler):
        """兜底：把任何未处理异常写进日志。
        否则 aiohttp 只回一句 "500 Internal Server Error"，
        日志里什么都没有 —— 排查会变成瞎猜（这次就吃过亏）。"""
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            log(f"[500] {request.method} {request.path} -> {type(e).__name__}: {e}\n"
                + _tb.format_exc())
            return web.Response(text="500 " + type(e).__name__ + ": " + str(e),
                                status=500)

    app = web.Application(client_max_size=64 * 1024 * 1024, middlewares=[_err_mw])
    app.router.add_get(P + "/", page)
    app.router.add_get(P + "/setup", setup_page)
    app.router.add_get(P + "/slide", slide_page)
    app.router.add_get(P + "/w/{name}", static_file)
    app.router.add_get(P + "/api/config", api_config)
    app.router.add_post(P + "/api/check_dir", api_check_dir)
    app.router.add_post(P + "/api/save_dirs", api_save_dirs)
    app.router.add_get(P + "/api/boot", api_boot)
    app.router.add_get(P + "/api/status", api_status)
    app.router.add_get(P + "/api/scan", api_scan)
    app.router.add_get(P + "/api/gallery", api_gallery)
    app.router.add_get(P + "/api/image", api_image)
    app.router.add_get(P + "/api/thumb", api_thumb)
    app.router.add_get(P + "/api/slide/meta", api_slide_meta)
    app.router.add_post(P + "/api/audio/start", api_audio_start)
    app.router.add_post(P + "/api/audio/stop", api_audio_stop)
    app.router.add_get(P + "/api/audio/status", api_audio_status)
    app.router.add_post(P + "/api/audio/sensitivity", api_audio_sensitivity)
    app.router.add_post(P + "/api/audio/thr-mode", api_audio_thr_mode)
    app.router.add_get(P + "/api/audio/stream", api_audio_stream)

    # 参数名 -> gallery_core 函数名
    GETS = {
        # 路由 -> (gallery_core 函数名, 要接收的参数名)
        # ⚠️ 参数名必须写全，否则前端传了也会被丢掉（画师翻页就是这么坏的）
        # ⚠️ 参数名也必须**和函数签名一致**，多了会 TypeError（stats 就是不收参的）
        "stats": ("stats", ()),
        "models": ("models", ("source",)),
        "samplers": ("samplers", ("source",)),
        "loras": ("loras", ("source",)),
        "artists": ("artists", ("source", "sort", "search", "page", "page_size", "fav_only")),
        # 注意：folders 不在这里 —— 它需要包一层 {folders:...}，
        # 由下面专门的 api_folders_get 处理
        "chain": ("chain_list", ()),
        "artlib/facets": ("artlib_facets", ()),
        "artlib/search": ("artlib_search", ("q", "archetype", "origin", "subject",
                                            "theme", "technique", "composition",
                                            "rating", "min_posts", "sort", "page", "page_size")),
    }
    for route, (fn, kw) in GETS.items():
        f = getattr(G, fn, None)
        if f is None:
            continue
        app.router.add_get(P + "/api/" + route, _wraps(f, kw))

    if hasattr(G, "folder_images"):
        # 注意：folder_images(name, source="") 里 name 是**必需**参数，
        # 空值不能像其它可选参数那样跳过，否则会 TypeError。
        async def api_folder_images(request):
            try:
                name = request.query.get("name") or ""
                src = request.query.get("source") or "comfyui"
                return _json(G.folder_images(name, src))
            except Exception as e:
                log(f"folder_images 出错: {e!r}")
                return _json({"items": [], "total": 0, "error": str(e)})
        app.router.add_get(P + "/api/folders/images", api_folder_images)

    # 画师库本地预览封面：names 是逗号分隔的一串画师名，要拆成列表再传。
    # （之前这里只传了 source，names 被丢掉，导致画师库卡片全是占位符 🎨）
    async def api_artlib_preview(request):
        try:
            raw = request.query.get("names", "") or ""
            names = [n.strip() for n in raw.split(",") if n.strip()]
            src = request.query.get("source", "comfyui")
            if not names:
                return _json({})
            return _json(G.artlib_preview(src, names))
        except Exception as e:
            return _json({"error": str(e)})

    if hasattr(G, "artlib_preview"):
        app.router.add_get(P + "/api/artlib/preview", api_artlib_preview)

    # ---- 之前漏掉的接口（插件版有、独立版没有，导致收藏/标签/元数据全废）----
    def _safe_name_(s):
        """去掉路径穿越，只取文件名部分（保留子目录，因为 file 里带子目录）。"""
        s = (s or "").replace("\\", "/").lstrip("/")
        parts = [p for p in s.split("/") if p and p not in (".", "..")]
        return "/".join(parts)

    async def api_folders_of(request):
        """这张图属于哪些收藏夹。前端灯箱的收藏按钮靠它拿状态 ——
        之前没这个接口，前端 .then() 直接抛错，收藏按钮就成了死的。"""
        try:
            src = request.query.get("source", "comfyui")
            file = _safe_name_(request.query.get("file", ""))
            return _json({"folders": G.folder_contains(src, file)})
        except Exception as e:
            return _json({"folders": [], "error": str(e)})

    async def api_tags(request):
        try:
            src = request.query.get("source", "comfyui")
            search = request.query.get("search", "")
            limit = min(int(request.query.get("limit") or 200), 2000)
            return _json({"items": G.tag_stats(src, search=search, limit=limit)})
        except Exception as e:
            return _json({"items": [], "error": str(e)})

    async def api_meta(request):
        try:
            src = request.query.get("source", "comfyui")
            file = _safe_name_(request.query.get("file", ""))
            return _json({"metadata": G.raw_metadata(src, file)})
        except Exception as e:
            return _json({"metadata": None, "error": str(e)})

    async def api_artists_tags(request):
        try:
            src = request.query.get("source", "comfyui")
            tag = request.query.get("tag", "")
            mx = int(request.query.get("max") or 400)
            lim = int(request.query.get("limit") or 40)
            return _json({"items": G.artist_top_tags(src, tag, max_show=mx, limit=lim)})
        except Exception as e:
            return _json({"items": [], "error": str(e)})

    async def api_open_folder(request):
        """在文件管理器里打开图片所在文件夹。"""
        try:
            src = request.query.get("source", "comfyui")
            file = _safe_name_(request.query.get("file", ""))
            fp = G.image_path(src, file)
            if not fp or not os.path.isfile(fp):
                return _json({"error": "not found"}, status=404)
            d = os.path.dirname(fp)
            if sys.platform == "win32":
                os.startfile(d)          # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", d])
            else:
                subprocess.Popen(["xdg-open", d])
            return _json({"ok": True, "dir": d})
        except Exception as e:
            return _json({"error": str(e)}, status=500)

    app.router.add_get(P + "/api/folders/of", api_folders_of)
    app.router.add_get(P + "/api/tags", api_tags)
    app.router.add_get(P + "/api/meta", api_meta)
    app.router.add_get(P + "/api/artists/tags", api_artists_tags)
    app.router.add_get(P + "/api/image/open-folder", api_open_folder)

    # 收藏夹列表：前端读的是 r.folders，所以必须包一层
    # （原来走 GETS 直传 folder_list()，返回的是裸 dict，前端拿到 undefined → 列表全空）
    async def api_folders_get(request):
        try:
            return _json({"folders": G.folder_list()})
        except Exception as e:
            return _json({"folders": {}, "error": str(e)})

    app.router.add_get(P + "/api/folders", api_folders_get)

    # 收藏夹增删改（前端 /api/folders 用 POST）
    # ⚠️ 前端删除时传的是 {action:"delete", name}，不是 folder —— 两个都收
    async def api_folders_post(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        try:
            act = (data.get("action") or "").lower()
            name = data.get("folder") or data.get("name") or ""
            to = data.get("to") or data.get("new_name") or data.get("new") or ""
            if act == "add":
                G.folder_add(name, data.get("items") or [])
            elif act == "remove":
                G.folder_remove(name, data.get("items") or [])
            elif act == "create":
                G.folder_create(name)
            elif act == "delete":
                G.folder_delete(name)
            elif act == "rename":
                G.folder_rename(name, to)
            return _json({"ok": True, "folders": G.folder_list()})
        except Exception as e:
            return _json({"ok": False, "error": str(e)}, status=500)

    app.router.add_post(P + "/api/folders", api_folders_post)

    # 设置某位画师的封面（画师库里右键「设为封面」用）
    async def api_artist_cover(request):
        try:
            data = await request.json()
            tag = (data.get("tag") or "").strip()
            file = (data.get("file") or "").strip()
            if not tag or not file:
                return _json({"ok": False, "error": "缺 tag/file"})
            G.save_artist_cover(tag, file)
            return _json({"ok": True})
        except Exception as e:
            return _json({"ok": False, "error": str(e)})

    if hasattr(G, "save_artist_cover"):
        app.router.add_post(P + "/api/artists/cover", api_artist_cover)

    # ---- 画师：收藏 / 备注 / 权重 ----
    def _post_json(fn, keys2):
        """把 JSON body 的字段转成 fn 的关键字实参。"""
        async def h(request):
            try:
                data = await request.json()
            except Exception:
                data = {}
            try:
                kw = {}
                for src_key, fn_key, caster in keys2:
                    v = data.get(src_key)
                    if caster and v is not None:
                        try:
                            v = caster(v)
                        except Exception:
                            v = None
                    if v is not None:
                        kw[fn_key] = v
                return _json(fn(**kw))
            except Exception as e:
                return _json({"ok": False, "error": str(e)})
        return h

    if hasattr(G, "save_artist_fav"):
        app.router.add_post(P + "/api/artists/fav", _post_json(
            G.save_artist_fav, [("tag", "tag", None), ("on", "on", bool)]))
    if hasattr(G, "save_artist_note"):
        app.router.add_post(P + "/api/artists/note", _post_json(
            G.save_artist_note, [("tag", "tag", None), ("note", "note", None)]))
    if hasattr(G, "save_artist_weight"):
        app.router.add_post(P + "/api/artists/weight", _post_json(
            G.save_artist_weight, [("tag", "tag", None), ("weight", "weight", float)]))

    # ---- 画师库内存释放 ----
    async def api_artlib_release(request):
        try:
            return _json(G.artlib_release())
        except Exception as e:
            return _json({"ok": False, "error": str(e)})

    if hasattr(G, "artlib_release"):
        app.router.add_get(P + "/api/artlib/release", api_artlib_release)

    # ---- 画师串（收藏的画师组合）----
    async def api_chains(request):
        try:
            return _json({"chains": G.chain_list(), "artists": G.chain_artists_flat()})
        except Exception as e:
            return _json({"chains": [], "artists": [], "error": str(e)})

    async def api_chains_save(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        try:
            return _json(G.chain_fav_save(data or {}))
        except Exception as e:
            return _json({"ok": False, "error": str(e)})

    async def api_chains_delete(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        try:
            return _json(G.chain_fav_delete((data or {}).get("id", "")))
        except Exception as e:
            return _json({"ok": False, "error": str(e)})

    if hasattr(G, "chain_list"):
        app.router.add_get(P + "/api/chains", api_chains)
    if hasattr(G, "chain_fav_save"):
        app.router.add_post(P + "/api/chains/save", api_chains_save)
    if hasattr(G, "chain_fav_delete"):
        app.router.add_post(P + "/api/chains/delete", api_chains_delete)

    # ---- 文件系统浏览（「打开文件夹」用）----
    async def api_fs_drives(request):
        out = []
        for c in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            p = c + ":\\"
            if os.path.isdir(p):
                out.append(p)
        return _json({"drives": out})

    async def api_fs_list(request):
        """列目录：给前端「选择文件夹」用。

        权限策略：
          scope=any  → 允许浏览任意路径（设置页选目录 / 打包选输出位置）
          默认       → 只允许已配置的图片根目录（图库内的「打开文件夹」）

        ⚠️ 这里踩了两个坑，都让「选目录」彻底不可用：
          1) 一律限制在图片根目录内 → 设置页选 ComfyUI 目录、打包选输出目录都被判「越权」
          2) 自己手写 dirs 返回的是**字符串数组**，而前端按对象用（d.name / d.has_outputs）
             → 即使不越权，目录列表也是空的、名字还显示 undefined
        现在统一交给 gallery_core.list_dir()，它返回的结构就是前端要的
        （带 has_outputs / has_models 标记，方便用户认出 WebUI 根目录）。
        """
        p = (request.query.get("path") or "").strip()
        scope = (request.query.get("scope") or "").strip().lower()
        if not p:
            return _json({"path": "", "parent": None, "dirs": []})
        if not os.path.isdir(p):
            return _json({"error": "目录不存在", "path": p, "parent": None, "dirs": []})
        if scope != "any":
            roots = _roots()
            try:
                real = os.path.realpath(p)
                if roots and not any(real == r or real.startswith(r + os.sep) for r in roots):
                    return _json({"error": "越权访问", "path": p, "parent": None, "dirs": []})
            except Exception:
                return _json({"error": "越权访问", "path": p, "parent": None, "dirs": []})
        return _json(G.list_dir(p))

    async def api_fs_mkdir(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        p = (data.get("path") or "").strip()
        name = (data.get("name") or "").strip()
        if not p:
            return _json({"ok": False, "error": "缺路径"})
        # 前端传的是 {path: 当前目录, name: 新文件夹名} —— 必须拼起来。
        # 原来只 makedirs(path)，把"新建文件夹"变成了对当前目录的空操作，
        # 返回还是 ok:true，用户看着成功其实什么都没建。
        if name:
            name = name.replace("\\", "/").split("/")[-1].strip()
            if not name:
                return _json({"ok": False, "error": "名称无效"})
            p = os.path.join(p, name)
        if not os.path.isdir(os.path.dirname(p) or "."):
            return _json({"ok": False, "error": "父目录不存在"})
        try:
            os.makedirs(p, exist_ok=True)
            return _json({"ok": True, "path": p})
        except Exception as e:
            return _json({"ok": False, "error": str(e)})

    async def api_fs_open_dir(request):
        """用系统资源管理器打开目录。

        同时支持两种调用方式：
          · GET  /api/fs/open-dir?path=<绝对目录>      ← 前端实际用的是这个
          · POST  {"file": "...", "source": "comfyui"}  ← 从图条目反推目录

        ⚠️ 原来只做成 POST+JSON，而前端（灯箱「所在位置」、打包完成后自动打开）
        用的是 GET+?path= —— 所以这个功能一直是坏的（404/405）。
        """
        q = request.query
        rel = (q.get("file") or "").strip()
        src = q.get("source") or "comfyui"
        abs_path = (q.get("path") or "").strip()

        if not abs_path and request.method == "POST":
            try:
                data = await request.json()
            except Exception:
                data = {}
            rel = (data.get("file") or rel).strip()
            src = data.get("source") or src
            abs_path = (data.get("path") or "").strip()

        if abs_path:
            target = abs_path
        else:
            root = {"comfyui": (_cfg.get("comfy_output") or "").strip(),
                    "webui": (_cfg.get("webui_root") or "").strip()}.get(src, "")
            target = root
            if rel and root:
                cand = os.path.join(root, os.path.dirname(rel.replace("/", os.sep)))
                if os.path.isdir(cand):
                    target = cand

        if not target or not os.path.isdir(target):
            return _json({"ok": False, "error": "目录不存在"})
        try:
            os.startfile(target)          # noqa: S606 —— 只打开用户自己选的目录
            return _json({"ok": True, "path": target})
        except Exception as e:
            return _json({"ok": False, "error": str(e)})

    # ---- 打包导出（独立版原先缺失，前端调 /api/zip 一直 404）----
    def _pack_images(data):
        """收集文件 + 写 zip。在后台线程跑，不阻塞服务。

        body: {target, items}          → 打包选中的图
              {target, query}          → 打包筛选结果全部
              by_model: true           → 按大模型分子目录
              name: "xxx"              → 指定 zip 名（默认带时间戳）
        """
        import zipfile
        import datetime as _dt

        target = (data.get("target") or "").strip()
        if not target or not os.path.isdir(target):
            return {"error": "目标目录不存在（先在弹窗里选一个文件夹）", "_code": 400}
        by_model = bool(data.get("by_model", False))
        entries = []          # (磁盘路径, 子目录)
        n_candidates = 0

        if data.get("items"):
            for it in data["items"]:
                fp = G.image_path(it.get("source", "comfyui"),
                                  _safe_name_(it.get("file", "")))
                if fp:
                    entries.append((fp, ""))
        elif data.get("query"):
            q = data["query"]
            src = q.get("source", "comfyui")
            # 索引还没建好就打包 → query 拿不到数据，会变成 500 或"没有可打包的图片"。
            # 给个明确提示，别让用户看着报错猜。
            try:
                st = G.scan_status(src)
                if st.get("total") and not st.get("ready"):
                    return {"error": "索引还在构建中（%s/%s），等它扫完再打包"
                                     % (st.get("scanned", 0), st.get("total", 0)),
                            "_code": 409}
            except Exception:
                pass
            try:
                r = G.query(
                    src,
                    q=q.get("q", ""), model=q.get("model", ""),
                    sampler=q.get("sampler", ""), lora=q.get("lora", ""),
                    steps_min=_int(q.get("steps_min")), steps_max=_int(q.get("steps_max")),
                    cfg_min=_float(q.get("cfg_min")), cfg_max=_float(q.get("cfg_max")),
                    min_w=_int(q.get("min_w")), min_h=_int(q.get("min_h")),
                    fav_only=q.get("fav", "0") == "1", tags=q.get("tags", ""),
                    sort="newest", page=1, page_size=10000,
                )
            except Exception as e:
                return {"error": "查询图片失败：%s" % e, "_code": 500}
            n_candidates = len(r.get("items", []))
            for it in r.get("items", []):
                fp = G.image_path(src, _safe_name_(it.get("file", "")))
                if fp:
                    sub = str(it.get("model") or "未分类") if by_model else ""
                    entries.append((fp, sub))
        else:
            return {"error": "没有指定要打包的图片", "_code": 400}

        if not entries:
            msg = "没有可打包的图片"
            if n_candidates:
                msg += "（候选 %d 个，但磁盘上都找不到文件）" % n_candidates
            return {"error": msg, "_code": 400}

        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        zname = (data.get("name") or "").strip()
        if zname:
            safe_n = re.sub(r'[\\/:*?"<>|]', "_", zname).strip() or "gallery_export"
            zpath = os.path.join(target, safe_n + ".zip")
            if os.path.exists(zpath):
                zpath = os.path.join(target, "%s_%s.zip" % (safe_n, ts))
        else:
            zpath = os.path.join(target, "gallery_export_%s.zip" % ts)

        try:
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                seen = set()
                for fp, sub in entries:
                    name = os.path.basename(fp)
                    if name in seen:                 # 不同目录下同名文件，加序号避免覆盖
                        name = "%d_%s" % (len(seen), name)
                    seen.add(name)
                    if sub:
                        safe = re.sub(r'[\\/:*?"<>|]', "_", sub).strip() or "未分类"
                        z.write(fp, os.path.join(safe, name))
                    else:
                        z.write(fp, name)
            return {"ok": True, "zip": zpath, "count": len(entries), "_code": 200}
        except Exception as e:
            return {"ok": False, "error": str(e), "_code": 500}

    async def api_zip(request):
        try:
            data = await request.json()
        except Exception:
            return _json({"error": "bad json"}, status=400)
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, _pack_images, data)
        code = resp.pop("_code", 200)
        return _json(resp, status=code)

    app.router.add_get(P + "/api/fs/drives", api_fs_drives)
    app.router.add_get(P + "/api/fs/list", api_fs_list)
    app.router.add_post(P + "/api/fs/mkdir", api_fs_mkdir)
    app.router.add_post(P + "/api/fs/open-dir", api_fs_open_dir)
    app.router.add_get(P + "/api/fs/open-dir", api_fs_open_dir)
    app.router.add_post(P + "/api/zip", api_zip)

    # ---- 本软件特有：设置读写（改的就是自己的 config.json）----
    async def api_settings(request):
        c = CFG.load()
        return _json({
            "comfy_output": c["comfy_output"], "webui_root": c["webui_root"],
            "data_dir": c["data_dir"],
            "configured": bool(c["comfy_output"]),
            "artist_format": "anima", "scan_mode": "incremental",
            "app": True,
        })

    async def api_settings_save(request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        c = CFG.load()
        for k in ("comfy_output", "webui_root", "data_dir"):
            if k in data:
                c[k] = str(data[k] or "").strip()
        if data.get("port"):
            c["port"] = _int(data["port"], 0)
        c["first_run"] = False
        CFG.save(c)
        globals()["_cfg"] = c
        install_folder_paths()
        sync_settings_to_core()      # ← 同上，改完目录要立刻生效
        return _json({"ok": True})

    app.router.add_get(P + "/api/settings", api_settings)
    app.router.add_post(P + "/api/settings", api_settings_save)

    # ---- 本软件特有：开机自检 / 结束自己 ----
    async def api_diag(request):
        return _json({
            "app_dir": APP_DIR,
            "comfy_output": _cfg.get("comfy_output"),
            "comfy_ok": os.path.isdir((_cfg.get("comfy_output") or "").strip() or "\0"),
            "webui_root": _cfg.get("webui_root"),
            "webui_ok": os.path.isdir((_cfg.get("webui_root") or "").strip() or "\0"),
            "data_dir": _cfg.get("data_dir"),
            "data_ok": os.path.isdir((_cfg.get("data_dir") or "").strip() or "\0"),
            "artlib_installed": bool(getattr(G, "_data_file", lambda n: "")("artists.jsonl")),
            "frozen": bool(getattr(sys, "frozen", False)),
        })

    async def api_quit(request):
        threading.Timer(0.4, lambda: os._exit(0)).start()
        return _json({"ok": True})

    app.router.add_get(P + "/api/diag", api_diag)
    app.router.add_post(P + "/api/quit", api_quit)
    return app


# ---------------------------------------------------------------------------
# 后台服务
# ---------------------------------------------------------------------------
class Server:
    def __init__(self, port=None):
        self.port = _int(port) or _int(_cfg.get("port")) or free_port()
        self.loop = None
        self.runner = None
        self._thread = None
        self._ready = threading.Event()

    def start(self):
        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            async def main():
                app = build_app()
                self.runner = web.AppRunner(app, access_log=None)
                await self.runner.setup()
                await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
                self._ready.set()
                while True:
                    await asyncio.sleep(3600)

            try:
                self.loop.run_until_complete(main())
            except Exception:
                import traceback
                log("HTTP 服务异常退出:\n" + traceback.format_exc())
                self._ready.set()

        self._thread = threading.Thread(target=run, daemon=True, name="gallery-http")
        self._thread.start()
        self._ready.wait(timeout=20)
        return self.port

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}{P}/"

    def stop(self):
        try:
            if self.loop and self.runner:
                asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(timeout=5)
        except Exception:
            pass
