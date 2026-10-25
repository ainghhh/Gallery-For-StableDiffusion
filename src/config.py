# -*- coding: utf-8 -*-
"""
Gallery4ComfyUI 独立桌面版 —— 配置管理

所有路径都存在 config.json 里（跟软件放一起），不再依赖环境变量或 ComfyUI。
"""
import json
import os
import sys

def _app_dir():
    """软件根目录。

    打包后 = exe 所在目录；源码运行时 = src/ 的上一级（即项目根）。
    数据（config.json / userdata / data）都放这里，不跟着 .py 跑。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))      # .../gallery/src
    parent = os.path.dirname(here)                          # .../gallery
    # 只有在 src 里才回退到上一级；否则（被拷到别处）就用当前目录
    return parent if os.path.basename(here).lower() == "src" else here


APP_DIR = _app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "comfy_output": "",      # ComfyUI 输出目录
    "webui_root": "",        # SD WebUI 根目录（可选）
    "data_dir": "",          # 画师库数据目录（可选，留空则用内置/自下')
    "port": 0,               # 0 = 自动挑一个空闲端口
    "first_run": True,       # 首次启动要弹目录选择
    "window": {"w": 1500, "h": 950, "x": None, "y": None, "maximized": False},
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    cfg = dict(DEFAULTS)
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = _deep_merge(DEFAULTS, json.load(f))
    except Exception:
        pass
    return cfg


def save(cfg):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        return False


def guess_dir(kind):
    """首次运行时猜一个默认目录，省得用户翻半天。kind: comfy | webui"""
    import glob
    drives = ["C:", "D:", "E:", "F:"]
    pats = {
        "comfy": ["**/ComfyUI/output", "**/ComfyUI-*/ComfyUI/output"],
        "webui": ["**/sd-webui-*/outputs", "**/stable-diffusion-webui/outputs"],
    }.get(kind, [])
    hits = []
    for d in drives:
        if not os.path.isdir(d + "\\"):
            continue
        for pat in pats:
            try:
                for p in glob.iglob(os.path.join(d + "\\", pat), recursive=True):
                    if os.path.isdir(p) and len(p) < 200:
                        hits.append(p)
            except Exception:
                pass
        if hits:
            break
    return hits[0] if hits else ""
