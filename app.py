# -*- coding: utf-8 -*-
"""
Gallery4ComfyUI 独立桌面版 —— 启动器（单窗口）

双击出**一个原生窗口**，里面就是图库。与 ComfyUI 完全独立。

设计要点（都是踩坑换来的）：
  · pywebview 的 start() 只能调一次  → 全程只 start 一次
  · create_window() 必须在主线程     → 主干里只建 1 个窗口
  · 不要在两个窗口之间切换           → 一开始就决定加载「设置页」还是「图库页」
  · 配置框保存后 → 窗口内 reload 到图库，不新建窗口
"""
import os
import sys

# —— 必须先于一切：中文环境下窗口标题 / 路径会乱码 ——
os.environ.setdefault("PYTHONUTF8", "1")
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass

import json
import threading
import time
import traceback

APP_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))
sys.path.insert(0, os.path.join(APP_DIR, "src"))
sys.path.insert(0, APP_DIR)

LOG_PATH = os.path.join(APP_DIR, "gallery.log")
TITLE = "\u56fe\u5e93 Gallery"          # 图库 Gallery


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _fatal(title, detail):
    log(f"FATAL {title}: {detail}")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, f"{title}\n\n{detail}",
                                         "\u56fe\u5e93 Gallery", 0x10)
    except Exception:
        pass


def main():
    log("=" * 62)
    log(f"启动 frozen={bool(getattr(sys,'frozen',False))} python={sys.version.split()[0]}")

    try:
        import webview
    except Exception as e:
        _fatal("缺少界面组件 pywebview", str(e))
        return 1

    import config as CFG
    import server as S

    cfg = CFG.load()
    need_cfg = bool(cfg.get("first_run")) or not cfg.get("comfy_output")

    try:
        srv = S.Server(cfg.get("port") or 0)
        port = srv.start()
        log(f"本地服务已启动 {srv.url}")
    except Exception as e:
        _fatal("启动本地服务失败", f"{e}\n\n{traceback.format_exc()}")
        return 1

    start_url = (srv.url.replace("/gallery4comfyui/", "/gallery4comfyui/setup")
                 if need_cfg else srv.url)
    log(f"首屏 = {'设置页' if need_cfg else '图库'}  {start_url}")

    W = {"win": None}

    def _boot():
        time.sleep(1.0)
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}{S.P}/api/boot",
                                   timeout=15).read()
            log("已触发 /api/boot")
        except Exception as e:
            log(f"boot 失败: {e}")

    # ---------------- JS 接口 ----------------
    class Api:
        # ---- 设置页 ----
        def read_config(self):
            c = CFG.load()
            return {"comfy_output": c.get("comfy_output", ""),
                    "webui_root": c.get("webui_root", ""),
                    "data_dir": c.get("data_dir", "")}

        def check_dir(self, path):
            path = (path or "").strip()
            if not path or not os.path.isdir(path):
                return {"ok": False, "exists": False}
            low = path.replace("/", "\\").lower()
            if low.rstrip("\\").endswith("\\output"):
                kind = "看起来是 ComfyUI 输出目录"
            elif os.path.isdir(os.path.join(path, "outputs")):
                kind = "看起来是 SD WebUI 根目录"
            else:
                kind = "目录存在"
            n = 0
            try:
                for dp, _, fns in os.walk(path):
                    for f in fns:
                        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp",
                                               ".gif", ".mp4", ".webm")):
                            n += 1
                    if n > 5000:
                        break
                if n:
                    kind += f"（约 {n} 个图片文件）"
            except Exception:
                pass
            return {"ok": True, "exists": True, "kind": kind}

        def pick_dir(self, current):
            try:
                from webview import OPEN_DIALOG
                d = W["win"].create_file_dialog(OPEN_DIALOG, directory=current or None)
                if d:
                    return d if isinstance(d, str) else d[0]
            except Exception as e:
                log(f"pick_dir 失败: {e!r}")
            return ""

        def save(self, comfy, webui, data):
            try:
                c = CFG.load()
                c["comfy_output"] = (comfy or "").strip()
                c["webui_root"] = (webui or "").strip()
                c["data_dir"] = (data or "").strip()
                c["first_run"] = False
                CFG.save(c)
                log(f"配置已保存 comfy={c['comfy_output']!r} webui={c['webui_root']!r}")
                S.install_folder_paths()
                return {"ok": True}
            except Exception as e:
                log(f"保存失败: {e!r}")
                return {"ok": False, "error": str(e)}

        def open_gallery(self):
            """设置完成后在**同一个窗口**里跳到图库。"""
            try:
                W["win"].load_url(srv.url)
                log("窗口已切到图库")
            except Exception as e:
                log(f"切图库失败: {e!r}")
                try:
                    W["win"].evaluate_js(f"location.href={json.dumps(srv.url)}")
                except Exception:
                    pass
            threading.Thread(target=_boot, daemon=True).start()
            return {"ok": True}

        # ---- 图库页 ----
        def quit(self):
            log("页面请求退出")
            try:
                W["win"].destroy()
            except Exception:
                os._exit(0)

        def open_settings(self):
            try:
                W["win"].load_url(srv.url.replace("/gallery4comfyui/",
                                                  "/gallery4comfyui/setup"))
                log("窗口已切到设置页")
            except Exception as e:
                log(f"切设置页失败: {e!r}")
            return {"ok": True}

    # ---------------- 建窗口（主线程，仅一次）----------------
    wcfg = cfg.get("window") or {}
    # 窗口图标（标题栏 / 任务栏）
    # 注意：这里必须先给 _ICON 兜底 None，否则"文件存在但赋值语句没走到"时
    # 会出现未定义变量，create_window 直接报 NameError。
    _ICON = None
    try:
        _ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
        if not os.path.isfile(_ico):
            # PyInstaller 打包后资源在 _MEIPASS 里
            _ico = os.path.join(getattr(sys, "_MEIPASS", ""), "app.ico")
        if os.path.isfile(_ico):
            _ICON = _ico
    except Exception:
        _ICON = None

    win = webview.create_window(
        TITLE, start_url,
        width=int(wcfg.get("w") or 1520),
        height=int(wcfg.get("h") or 960),
        min_size=(940, 620),
        background_color="#14161a",
        js_api=Api(),
    )
    W["win"] = win

    def on_closing():
        try:
            b = getattr(win, "bounds", None)
            if b:
                c = CFG.load()
                c["window"] = {"w": int(b[2]), "h": int(b[3]),
                               "x": int(b[0]), "y": int(b[1]),
                               "maximized": bool(getattr(win, "maximized", False))}
                CFG.save(c)
        except Exception:
            pass
        log("窗口关闭，退出")

    win.events.closing += on_closing

    if not need_cfg:
        threading.Thread(target=_boot, daemon=True).start()

    try:
        # ⚠️ 图标参数属于 webview.start()，**不是** create_window()。
        #    这个版本的 pywebview 里 create_window 不收 icon，
        #    传了会直接 TypeError、进程秒退（实测踩过）。
        _start_kw = {"debug": False, "private_mode": False}
        if _ICON:
            _start_kw["icon"] = _ICON
        webview.start(**_start_kw)
    except Exception as e:
        log(f"窗口异常: {e}\n{traceback.format_exc()}")
        _fatal("窗口启动失败", str(e))
        return 1

    try:
        srv.stop()
    except Exception:
        pass
    log("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
