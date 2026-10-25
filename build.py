# -*- coding: utf-8 -*-
"""
Gallery4ComfyUI 独立桌面版 —— 打包脚本
用法：python build.py
产物：dist\Gallery\Gallery4ComfyUI.exe  （绿色文件夹版，双击即用）
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SEP = ";" if os.name == "nt" else ":"

# gallery_core / server / config 都是独立模块，用 hidden-import 保证被收进来
HIDDEN = ["gallery_core", "server", "config", "aiohttp", "webview",
          # 音频采集：audio_beat 用动态 import 拉 soundcard，
          # PyInstaller 静态扫不到，必须显式声明
          "audio_beat", "soundcard", "cffi",
          # soundcard 的 Windows 后端与 cffi 运行时编译链路
          "soundcard.mediafoundation", "soundcard.coreaudio",
          "_cffi_backend", "cffi.backend_ctypes"]

# pywebview 会拖进整个 PyQt5（≈80 MB），但我们用的是系统自带的 Edge WebView2 后端，
# 根本用不到 Qt —— 排掉它，产物能从 120 MB 瘦到 ~35 MB。
EXCLUDES = [
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy",
    "tkinter", "unittest", "pydoc_data", "test",
    # ⚠️ 不能排 numpy！soundcard 的 mediafoundation 后端 import numpy，
    #    排掉之后「跟音乐」模式直接不可用（实测 ModuleNotFoundError）。
    #    多出来的体积约 15MB，换一个能用的功能，值得。
    "pandas", "scipy", "matplotlib",
    "cryptography", "pip",
    # ⚠️ 不能排 setuptools：soundcard 靠 CFFI 运行时编译，编译链路要用它
]

# 必须一起打包的资源（源码目录 + 前端页面）
DATAS = [
    # 只放真正要用的前端文件；用 ignore 排掉备份 / 缓存
    (os.path.join(ROOT, "src", "web"), "src/web"),
    (os.path.join(ROOT, "src", "gallery_core.py"), "src"),
    (os.path.join(ROOT, "src", "server.py"), "src"),
    (os.path.join(ROOT, "src", "config.py"), "src"),
    # 系统音频节拍采集（WASAPI 回环）—— 不带上「跟音乐」模式就没法用
    (os.path.join(ROOT, "src", "audio_beat.py"), "src"),
    # 窗口图标也要打进产物（运行时读）
    (os.path.join(ROOT, "app.ico"), "."),
]

# 单文件模式？默认绿色文件夹（启动快、好排查）
ONEFILE = "--onefile" in sys.argv

ARGS = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--name", "Gallery4ComfyUI",
    "--windowed",                 # 不要控制台
    # 应用图标：多尺寸 ico，Windows 会按场合自动挑
    "--icon", os.path.join(ROOT, "app.ico"),
    "--distpath", ROOT,           # 直接出到根目录（不再套一层 dist\）
    "--workpath", os.path.join(ROOT, "_build"),
    "--specpath", os.path.join(ROOT, "_build"),
    "--paths", os.path.join(ROOT, "src"),
    "--paths", ROOT,
    "--collect-submodules", "webview",
    "--collect-data", "webview",
    # soundcard 用 CFFI **运行时编译**：它带 .py.h 头文件，还要 .py 源文件在场。
    # PyInstaller 默认只收 .pyc 和 .h，源码一丢，import 就失败
    # （实测产物里 backends={"soundcard": false}）。
    # 这是它自己的钩子干的事，必须显式收全。
    "--collect-data", "soundcard",
    "--collect-submodules", "soundcard",
    # cffi 的后端模块也是动态导入的
    "--collect-submodules", "cffi",
]

for m in HIDDEN:
    ARGS += ["--hidden-import", m]

for m in EXCLUDES:
    ARGS += ["--exclude-module", m]

for src, dst in DATAS:
    ARGS += ["--add-data", f"{src}{SEP}{dst}"]

# soundcard 的 CFFI 是**运行时**编译的：它在 import 时读自己的 .py 源文件
# 和 .py.h 头文件，现场编译 C 扩展。
# PyInstaller 的 collect-data 只收 .py.h，把 .py 全丢了 →
# 产物里 import soundcard 直接失败（实测 backends 全是 false）。
# 所以这里手工把整个 soundcard 包原样塞进去。
try:
    import soundcard as _sc_pkg
    _sc_dir = os.path.dirname(_sc_pkg.__file__)
    ARGS += ["--add-data", f"{_sc_dir}{SEP}soundcard"]
    print(f"  [build] 已附加 soundcard 源码：{_sc_dir}")
except Exception as _e:
    print(f"  [build] 警告：拿不到 soundcard 目录（{_e}），「跟音乐」模式将不可用")

if ONEFILE:
    ARGS.append("--onefile")

ARGS.append(os.path.join(ROOT, "app.py"))


def main():
    print("[1/2] 清理旧产物…")
    for d in ("dist", "_build", "build"):
        p = os.path.join(ROOT, d)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    # 旧 exe / _internal 会被 PyInstaller 覆盖，这里先删干净避免残留旧文件
    for p in (os.path.join(ROOT, "Gallery4ComfyUI.exe"),
              os.path.join(ROOT, "_internal")):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            try:
                os.remove(p)
            except Exception:
                pass

    print("[2/2] 开始打包（首次较慢，约 1~3 分钟）…")
    print("       " + " ".join(ARGS[:6]) + " …")
    r = subprocess.run(ARGS, cwd=ROOT)
    if r.returncode != 0:
        print("\n!! 打包失败")
        return r.returncode

    exe = os.path.join(ROOT, "Gallery4ComfyUI.exe") if ONEFILE \
        else os.path.join(ROOT, "Gallery4ComfyUI", "Gallery4ComfyUI.exe")
    if not os.path.isfile(exe):
        print("\n!! 没找到产物:", exe)
        return 1

    print(f"\n✅ 打包完成：{exe}")
    base = os.path.dirname(exe)
    tot = 0
    for dp, _, fns in os.walk(base):
        for f in fns:
            tot += os.path.getsize(os.path.join(dp, f))
    print(f"   总体积 {tot/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
