# -*- coding: utf-8 -*-
"""LuoTavern 桌面管理台（免安装单文件 EXE 入口）。

打包：python build_exe.py
运行：双击 dist\\LuoTavern管理台.exe；或开发模式 python desktop_app.py

EXE 只负责管理：项目中的机器人和依赖安装都调用系统 Python 完成。
首次启动会自动把项目 .env / config*.json 里的现有密钥导入 EXE 密钥库。
"""
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser

import desktop_vault

PORT = 8123
_MUTEX_HANDLE = None


def _single_instance():
    """Windows 命名互斥体：已有一个管理台运行时，第二次启动直接退出并打开现有实例。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    _MUTEX_HANDLE = k32.CreateMutexW(
        None, False, "LuoTavernManager_SingleInstance")
    if not _MUTEX_HANDLE:
        return True  # 创建失败不阻塞启动
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def _read_local_token():
    """读取已运行实例的管理台 token（data/admin_token）。

    管理台 GET 接口现在也要求鉴权，单实例探测必须带 token 才能看到已运行实例。
    只读取、不创建：避免在错误的工作目录下生成一个新的 token 文件。
    """
    roots = []
    env = os.environ.get("LUOTAVERN_PROJECT", "")
    if env:
        roots.append(env)
    d = desktop_vault.exe_dir()
    roots += [d, os.path.dirname(d), os.getcwd()]
    for root in roots:
        try:
            with open(os.path.join(root, "data", "admin_token"),
                      "r", encoding="utf-8") as f:
                token = f.read().strip()
            if token:
                return token
        except OSError:
            continue
    return ""


def _existing_port():
    """探测已运行实例的管理台端口（8123-8132）；未找到返回 None。"""
    token = _read_local_token()
    for port in range(PORT, PORT + 10):
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/desktop/status" % port,
                headers={"X-Admin-Token": token})
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    return port
        except Exception:
            continue
    return None


def _start_server():
    """在 8123 起尝试启动本地管理台，端口被占用则顺延。"""
    from qqbot.admin import start_admin
    for port in range(PORT, PORT + 10):
        try:
            server = start_admin(bot=None, port=port, block=False)
            return server, port
        except OSError:
            continue
    return None, None


def icon_path():
    """窗口图标：优先打包内嵌资源，其次项目 build 目录。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for name in ("图标.ico", "icon.ico"):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build", "图标.ico")
    return p if os.path.isfile(p) else None


def wait_server(url, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.HTTPError as exc:
            # 页面本身是公开的（token 由页面注入）：403 说明服务已起来但 Host/来源被拒
            if exc.code == 403:
                print("管理台拒绝本机页面请求（HTTP 403），请检查代理设置", flush=True)
                return False
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _message_box(text, title="LuoTavern 管理台"):
    """无控制台的 windowed EXE 里，用系统弹窗告知用户（print 看不见）。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x40)
        return True
    except Exception:
        print(text, flush=True)
        return False


def main():
    if not _single_instance():
        # 已有实例在运行：打开浏览器指向它，本进程直接退出，避免双监督
        existing = None
        for _ in range(20):          # 先实例可能刚拿到互斥体、HTTP 还没就绪
            existing = _existing_port()
            if existing is not None:
                break
            time.sleep(0.5)
        if existing is not None:
            webbrowser.open("http://127.0.0.1:%d" % existing)
        else:
            _message_box("检测到管理台已在运行，但暂时找不到它的端口（8123-8132）。\n"
                         "请稍等片刻再双击，或检查是否被防火墙拦截。")
        return
    # 延迟导入：SERVICE 构造会探测系统 Python（可能耗时数秒），
    # 放在单实例检查之后，让第二个实例能立即退出
    from desktop_service import SERVICE
    os.environ.setdefault("LUOTAVERN_ROOT", SERVICE.project_root)
    try:
        os.chdir(SERVICE.project_root)
    except OSError:
        pass
    imported = desktop_vault.import_from_project(
        SERVICE.project_root, desktop_vault.load_vault())
    if imported:
        print("[密钥库] 已从项目导入现有密钥：%s" % "、".join(imported))
    else:
        print("[密钥库] 文件：%s" % desktop_vault.vault_path())

    server, port = _start_server()
    if server is None:
        print("管理台端口 8123-8132 均被占用，请关闭占用程序后重试", flush=True)
        return
    url = "http://127.0.0.1:%d" % port
    if not wait_server(url):
        print("管理台服务未就绪，已退出", flush=True)
        server.shutdown()
        server.server_close()
        return

    try:
        import webview

        class Api:
            def pick_folder(self):
                try:
                    result = window.create_file_dialog(webview.FOLDER_DIALOG)
                    return result[0] if result else None
                except Exception:
                    return None

            def pick_save_file(self, default_name=""):
                try:
                    result = window.create_file_dialog(
                        webview.SAVE_DIALOG, save_filename=str(default_name or ""))
                    if not result:
                        return None
                    return result if isinstance(result, str) else result[0]
                except Exception:
                    return None

            def open_browser(self, url=""):
                """用系统默认浏览器打开外部地址（如 NapCat WebUI）。

                只放行 http/https：拒绝 file:/javascript: 等协议劫持。
                """
                if url and str(url).lower().startswith(("http://", "https://")):
                    webbrowser.open(str(url))
                return True

        window = webview.create_window(
            "LuoTavern 管理台",
            url,
            width=1320,
            height=860,
            min_size=(1100, 720),
            background_color="#0b0f14",
            text_select=True,
            js_api=Api(),
        )
        webview.start(icon=icon_path())
    except Exception as exc:
        print("WebView 启动失败，已退回浏览器模式：%s" % exc)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
    finally:
        try:
            SERVICE.stop_bot()
        except Exception:
            pass
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
