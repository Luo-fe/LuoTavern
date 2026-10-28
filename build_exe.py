# -*- coding: utf-8 -*-
"""一键构建免安装 EXE（PyInstaller onefile + pywebview）。

用法：python build_exe.py
构建依赖会自动安装：pyinstaller、pywebview、pillow
产物：dist\\LuoTavern管理台.exe（单文件，绿色免安装）
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))


def run(cmd):
    print("> %s" % " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=ROOT)


def ensure_build_deps():
    for pkg in ("pyinstaller", "pywebview", "pillow"):
        try:
            __import__({"pyinstaller": "PyInstaller"}.get(pkg, pkg))
        except ImportError:
            print("安装构建依赖：%s ..." % pkg, flush=True)
            run([sys.executable, "-m", "pip", "install", pkg])


def make_icon():
    from PIL import Image
    png = os.path.join(ROOT, "图标.png")
    ico = os.path.join(ROOT, "build", "图标.ico")
    os.makedirs(os.path.dirname(ico), exist_ok=True)
    Image.open(png).convert("RGBA").save(
        ico, format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ico


def clean_dist(keep=("LuoTavern管理台.exe",)):
    """把 dist/ 里的历史产物**移到** dist/_old/（不删除，避免误删密钥库）。

    dist/ 曾长期被当工作目录：里面会留下旧构建、_new 试验品、
    data/memory.db 与 *vault.json。留着它们会让用户双击到旧 exe
    （旧 exe 与当前源码不兼容：写操作 403、缺整个 memstore 包）。
    密钥库文件不移动，只提示位置（可能含用户唯一一份凭据）。
    """
    dist = os.path.join(ROOT, "dist")
    if not os.path.isdir(dist):
        return []
    old = os.path.join(dist, "_old")
    moved, warned = [], []
    for fn in os.listdir(dist):
        if fn in keep or fn == "_old" or fn == "BUILD_INFO.txt":
            continue
        if "vault" in fn.lower():
            warned.append(fn)
            continue
        src = os.path.join(dist, fn)
        try:
            os.makedirs(old, exist_ok=True)
            os.replace(src, os.path.join(old, fn)) if os.path.isfile(src) else \
                shutil.move(src, os.path.join(old, fn))
            moved.append(fn)
        except OSError:
            pass
    if moved:
        print("已把 dist/ 历史产物移到 dist/_old/：%s" % "、".join(moved))
    for fn in warned:
        print("注意：dist/%s 疑似密钥库，未移动（建议统一用项目根的一份密钥库）" % fn)
    return moved


def write_build_info(exe):
    """写入构建信息（源码版本 + 构建时间 + 源码最新修改时间），便于核对发布物新旧。"""
    rev = ""
    try:
        rev = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        rev = "(无 git)"
    newest, newest_file = 0.0, ""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "build", "dist", "data", "character",
                                    "book", "__pycache__", "character_backup")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            try:
                m = os.path.getmtime(os.path.join(dirpath, fn))
            except OSError:
                continue
            if m > newest:
                newest, newest_file = m, os.path.relpath(
                    os.path.join(dirpath, fn), ROOT)
    lines = [
        "LuoTavern 桌面管理台构建信息",
        "构建时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "git 版本: %s" % (rev or "(无)"),
        "源码最新修改: %s (%s)" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest)) if newest else "-",
            newest_file or "-"),
        "产物: %s (%.1f MB)" % (os.path.basename(exe), os.path.getsize(exe) / 1024 / 1024),
    ]
    out = os.path.join(ROOT, "dist", "BUILD_INFO.txt")
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("构建信息：%s" % out)
    except OSError:
        pass
    return lines


# EXE 只需要：桌面壳（pywebview）+ 管理台（qqbot.admin/admin_ui）+ 记忆页（memory_ui）
# + 项目定位/密钥库。向量检索（fastembed/onnxruntime）与数据分析栈只有机器人本体
# （用系统 Python 从源码跑）才需要，排除后体积从 ~147MB 回到 ~40MB。
# 排除是安全的：qqbot/embeddings.py 的 fastembed 导入在 try/except 内，
# 缺失时向量路径降级为关键词路径（管理台记忆页本就不做向量推理）。
EXCLUDES = [
    "fastembed", "onnxruntime", "tokenizers", "hf_xet", "huggingface_hub",
    "transformers", "datasets", "pyarrow", "pandas", "scipy", "sklearn",
    "matplotlib", "lxml", "IPython", "jupyter", "pytest", "setuptools",
    "tkinter", "PIL.ImageQt",
]


def work_paths():
    """PyInstaller 的 workpath/specpath 放到项目外（%LOCALAPPDATA%\\LuoTavern）。

    否则每次构建都会在项目根留下 60MB 左右的 build/ 中间产物与一个 .spec，
    既污染仓库又容易被误当成源码。
    """
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    work = os.path.join(base, "LuoTavern", "pyi-build")
    spec = os.path.join(base, "LuoTavern", "pyi-spec")
    os.makedirs(work, exist_ok=True)
    os.makedirs(spec, exist_ok=True)
    return work, spec


def main():
    ensure_build_deps()
    ico = make_icon()
    work, spec = work_paths()
    clean_dist()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "LuoTavern管理台",
        "--icon", ico,
        "--workpath", work,
        "--specpath", spec,
        "--add-data", os.path.join(ROOT, "build", "图标.ico") + os.pathsep + ".",
        "--hidden-import", "memory_ui",
        "--hidden-import", "qqbot.admin_ui",
        "--hidden-import", "desktop_vault",
        "--hidden-import", "desktop_service",
        "--hidden-import", "clr_loader",
        "--hidden-import", "pythonnet",
        "--hidden-import", "webview.platforms.winforms",
        "--hidden-import", "webview.platforms.edgechromium",
        "--collect-all", "webview",
        *[arg for mod in EXCLUDES for arg in ("--exclude-module", mod)],
        # 绝对路径：specpath 指向项目外时，spec 里的相对路径会相对 spec 目录解析
        os.path.join(ROOT, "desktop_app.py"),
    ]
    run(cmd)
    exe = os.path.join(ROOT, "dist", "LuoTavern管理台.exe")
    if os.path.isfile(exe):
        print("\n构建完成：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1024 / 1024))
        write_build_info(exe)
        print("绿色免安装：把 exe 放到项目根目录（与 bot.py 同目录）即可使用。")
    else:
        print("\n构建失败，请检查上方日志。")
        sys.exit(1)


if __name__ == "__main__":
    main()
