# -*- coding: utf-8 -*-
"""桌面管理台服务：项目定位、机器人子进程、依赖安装、日志环形缓冲。

注意：EXE 是 PyInstaller 打包的独立解释器，不能在里面跑 pip/启动 bot，
所以机器人与依赖安装都调用系统 Python（py -3 / python），避免"打包环境"假象。
"""
import base64
import hashlib
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlparse

import desktop_vault
from qqbot.textutil import redact

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

QRCODE_MAX_AGE = 150          # 缓存二维码视为“新鲜”的最大年龄（秒）；QQ 二维码约 2 分钟失效
try:                          # psutil 可选：没有就用 PowerShell/CIM 兜底
    import psutil
except Exception:             # pragma: no cover
    psutil = None
MAX_LOGS = 2000
_NOISY_NAP_PATTERNS = (
    "LightQuic", "light_quic", "quic_transport", "crypto_config",
    "OnProofValid", "OnNewEncryptionKeyAvailable", "handshake",
    "send reset", "stream id:", "~LightQuic",
)
NAP_SILENT_RESTART_MIN = 8          # 接收静默超过该分钟数视为链路假死
NAP_ACTIVE_WINDOW_MIN = 25          # 机器人最近 N 分钟内有过回复才算“活跃”
NAP_OFFLINE_GRACE_SEC = 60          # 日志出现离线后，再无接收 N 秒才重启
NAP_AUTO_RESTART_COOLDOWN = 600     # 两次自动重启最小间隔（秒）
NAP_PORT_LOST_GRACE_SEC = 240       # framework：QQ 在线但 OneBot 端口消失多久后重启

_JOB = None


def _init_kill_job():
    """Windows Job Object：EXE 进程被结束（含强杀）时，OS 强制回收所有子进程。"""
    global _JOB
    if os.name != "nt" or _JOB is not None:
        return _JOB
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        # 64 位句柄：未声明 restype 会被截断成 32 位 int，Job 回收失效
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info))
        _JOB = job
    except Exception:
        _JOB = None
    return _JOB


def _assign_job(proc):
    """把子进程加入 Job Object；失败不阻塞（正常退出路径仍有 stop_bot 兜底）。"""
    job = _init_kill_job()
    if not job or proc is None:
        return False
    try:
        import ctypes
        ctypes.windll.kernel32.AssignProcessToJobObject(
            job, int(proc._handle))
        return True
    except Exception:
        return False


def _resolve_python():
    """返回可用于 pip / 运行 bot 的系统 Python 可执行路径；找不到返回 None。"""
    candidates = [
        ["python", "-c", "import sys; print(sys.executable)"],
        ["py", "-3", "-c", "import sys; print(sys.executable)"],
        ["python3", "-c", "import sys; print(sys.executable)"],
    ]
    for cmd in candidates:
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=8,
                creationflags=CREATE_NO_WINDOW)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip().strip('"')
        except Exception:
            continue
    return None


class DesktopService:
    def __init__(self, bot_port=None):
        # 可重入锁：start_bot/install_deps 持锁期间会调用 self.log()（同样加锁），
        # 普通 Lock 会导致死锁、整个管理台卡死。
        self.lock = threading.RLock()
        self.project_root = self._default_project()
        self.python = _resolve_python()
        self.python_version = self._probe_version()
        self.bot_port = int(bot_port or 8008)
        self.bot_proc = None
        self.bot_pid = None
        self.bot_managed = False
        self.bot_platform = None
        self.bot_started_at = None
        self.stop_event = threading.Event()
        self.install_proc = None
        self.install_started_at = None
        self.napcat_proc = None
        self.napcat_started = False
        self.napcat_ready = False
        self.napcat_log_path = None
        self.napcat_last_receive_ts = None
        # framework 模式（官方 QQ 客户端 + NapCat 注入）状态
        self.framework_pids = set()            # 本管理台启动/接管的 QQ.exe PID
        self.framework_owned = False           # True=本管理台拉起，可安全结束
        self._framework_launch_ts = 0.0
        self._framework_port_seen_ts = None
        self._napcat_offline_seen_ts = None
        self._napcat_login_seen_ts = None      # 本次启动以来"已登录"的时刻（宽限期用）
        self._napcat_kicked_ts = None          # 上次被 QQ 踢下线的时刻（管理台提示用）
        self._push_obj = None                  # 惰性构建的推送器（方案 C）
        self._napcat_health_started = False
        self._napcat_last_auto_restart = 0.0
        self._napcat_cred = None
        self._logs = []
        self._seq = 0

    def _probe_version(self):
        if not self.python:
            return None
        try:
            out = subprocess.run(
                [self.python, "-c", "import platform; print(platform.python_version())"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW)
            return out.stdout.strip() if out.returncode == 0 else None
        except Exception:
            return None

    # ---------- 项目定位 ----------
    def _default_project(self):
        d = desktop_vault.exe_dir()
        candidates = [d]
        # PyInstaller 默认把 EXE 放在项目根的 dist/ 下：向上找项目根
        if os.path.basename(d) in ("dist", "build"):
            candidates.append(os.path.dirname(d))
        candidates.append(os.getcwd())
        env = os.environ.get("LUOTAVERN_PROJECT", "")
        if env and os.path.isfile(os.path.join(env, "bot.py")):
            return env
        for c in candidates:
            if c and os.path.isfile(os.path.join(c, "bot.py")):
                return os.path.abspath(c)
        return d

    def set_project(self, path):
        path = os.path.abspath(str(path or ""))
        if not os.path.isfile(os.path.join(path, "bot.py")):
            return {"ok": False, "error": "该目录下没有 bot.py，不是 LuoTavern 项目"}
        with self.lock:
            changed = path != self.project_root
            self.project_root = path
            if changed:
                # 切换项目：旧项目的凭证/进程状态一律作废，防止串项目
                self._napcat_cred = None
                self.napcat_log_path = None
                self.napcat_last_receive_ts = None
                self._napcat_offline_seen_ts = None
                self._napcat_last_auto_restart = 0.0
                self.framework_pids = set()
                self.framework_owned = False
                self._framework_launch_ts = 0.0
                self._framework_port_seen_ts = None
        return {"ok": True, "project_root": path}

    # ---------- 日志 ----------
    def log(self, kind, text):
        with self.lock:
            self._seq += 1
            self._logs.append({
                "seq": self._seq,
                "ts": time.strftime("%H:%M:%S"),
                "kind": kind,
                "text": redact(text),
            })
            del self._logs[:-MAX_LOGS]

    def logs_since(self, after=0):
        with self.lock:
            return [dict(x) for x in self._logs if x["seq"] > int(after)]

    # ---------- NapCat（QQ 依赖） ----------
    @staticmethod
    def _port_open(port):
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
                return True
        except OSError:
            return False

    def _wait_port_closed(self, port, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._port_open(port):
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _napcat_port(root):
        """从 config.qq.json 的 onebot_ws_url 读取端口（默认 3001）。"""
        try:
            with open(os.path.join(root, "config.qq.json"),
                      "r", encoding="utf-8") as f:
                data = json.load(f)
            url = str(data.get("onebot_ws_url") or "")
            if url:
                return urlparse(url).port or 3001
        except Exception:
            pass
        return 3001

    @classmethod
    def _napcat_line_ok(cls, line):
        """NapCat 日志降噪：QUIC 握手/加解密刷屏不进入桌面日志；超长行截断。"""
        line = str(line)
        if any(t in line for t in _NOISY_NAP_PATTERNS):
            return False
        return True

    # ---------- 被动推送（方案 C：手机上先知道、先拿到二维码） ----------
    def _pusher(self):
        """惰性构建推送器；config.json 的 push 段变化时自动重建。"""
        from qqbot.push import build_pusher
        from qqbot import config as qconfig
        try:
            cfg = qconfig.load_config(
                os.path.join(self.project_root, "config.json"))
        except Exception:
            cfg = {}
        section = cfg.get("push") or {}
        if self._push_obj is not None and getattr(self._push_obj, "config", {}).get("push") == section:
            return self._push_obj
        if self._push_obj is not None:
            try:
                self._push_obj.close(timeout=0.5)   # 配置变了：旧推送线程不留在后台
            except Exception:
                pass
        self._push_obj = build_pusher(cfg, log=lambda m: self.log("push", m))
        return self._push_obj

    def _notify_kick(self):
        try:
            p = self._pusher()
            if not p.enabled() or not p.config.get("push", {}).get("on_kick", True):
                return
            p.notify("kick", "LuoTavern：QQ 被踢下线",
                     "登录态已失效，需要重新扫码；随后会把二维码推给你。")
        except Exception as exc:
            self.log("push", "被踢推送失败（忽略）：%s" % exc)

    def _notify_qr(self):
        """二维码刚生成 → 直接把图推给手机（单手机可用“相册选图”扫）。"""
        try:
            p = self._pusher()
            if not p.enabled() or not p.config.get("push", {}).get("on_qr", True):
                return
            png = None
            path = self.napcat_qrcode_cache_path(self.project_root)
            if path and os.path.isfile(path):
                with open(path, "rb") as f:
                    png = f.read()
            p.notify("qr", "LuoTavern：请扫码登录 QQ",
                     "二维码约 2 分钟内有效。单手机用法：长按图片保存到相册 → "
                     "QQ「扫一扫 → 相册」选这张图。", image=png)
        except Exception as exc:
            self.log("push", "二维码推送失败（忽略）：%s" % exc)

    def _log_napcat_line(self, line):
        line = str(line).rstrip("\n")
        if "接收 <-" in line:
            self.napcat_last_receive_ts = time.time()
        if "账号状态变更为离线" in line:
            self._napcat_offline_seen_ts = time.time()
        if "二维码已保存到" in line or "请扫描下面的二维码" in line:
            self._notify_qr()
        if "KickedOffLine" in line or "登录已失效" in line:
            # QQ 服务端作废了会话：第三方协议登录态有寿命（本机实测约 2 小时），
            # 同账号在别处登录也会用同一条通知顶号。记下来给管理台明确提示。
            self._napcat_kicked_ts = time.time()
            self._napcat_offline_seen_ts = time.time()
            self.log("napcat", "【QQ 被踢下线】登录态已失效：需要重新扫码；"
                               "若配置了“账号+密码快速登录”则可自动重登")
            self._notify_kick()
        if not self._napcat_line_ok(line):
            return
        if len(line) > 500:
            line = line[:500] + "…（截断）"
        self.log("napcat", line)

    @staticmethod
    def _napcat_dir(root):
        candidates = [
            os.path.join(root, "NapCat.Shell.Windows.Node"),
            os.path.join(os.path.dirname(root), "NapCat.Shell.Windows.Node"),
        ]
        for d in candidates:
            if os.path.isfile(os.path.join(d, "index.js")):
                return d
        return None

    # ---------- 本机 QQ 客户端（NapCat Framework 注入） ----------
    @staticmethod
    def _framework_dir(root):
        """官方 QQ + NapCat.Framework 目录（项目根或上一级）。"""
        candidates = [
            os.path.join(root, "NapCat.Framework"),
            os.path.join(os.path.dirname(root), "NapCat.Framework"),
        ]
        for d in candidates:
            if (os.path.isfile(os.path.join(d, "napimain.exe"))
                    and os.path.isfile(os.path.join(d, "napiLoader.bat"))):
                return d
        return None

    @classmethod
    def _qq_login_mode(cls, root):
        """QQ 接入方式：framework（本机官方 QQ）/ shell（NapCat 无头）。

        未配置时默认 shell（保持旧行为）；在管理台「启动设置 → QQ 登录方式」
        选择 framework 后生效。
        """
        try:
            with open(os.path.join(root, "config.qq.json"), "r",
                      encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        mode = str(cfg.get("login_mode") or "").strip().lower()
        return mode if mode in ("framework", "shell") else "shell"

    @staticmethod
    def _qq_process_pids():
        """当前 QQ.exe 的 PID 集合（psutil 缺失时用 tasklist 兜底）。"""
        if psutil is not None:
            out = set()
            try:
                for proc in psutil.process_iter(["pid", "name"]):
                    try:
                        if str(proc.info.get("name") or "").lower() == "qq.exe":
                            out.add(int(proc.info["pid"]))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                return out
            except Exception:
                pass
        try:
            res = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="gbk", errors="replace",
                timeout=10, creationflags=CREATE_NO_WINDOW)
            pids = set()
            for line in (res.stdout or "").splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[1].isdigit():
                    pids.add(int(parts[1]))
            return pids
        except Exception:
            return set()

    @classmethod
    def _framework_pidfile(cls, root):
        return os.path.join(root, "data", "qq_framework.pid")

    @classmethod
    def _record_framework_pids(cls, root, pids):
        try:
            os.makedirs(os.path.join(root, "data"), exist_ok=True)
            with open(cls._framework_pidfile(root), "w", encoding="utf-8") as f:
                f.write(",".join(str(int(p)) for p in sorted(set(pids))))
        except OSError:
            pass

    @classmethod
    def _read_framework_pids(cls, root):
        try:
            with open(cls._framework_pidfile(root), "r", encoding="utf-8") as f:
                return {int(p) for p in (f.read() or "").split(",") if p.strip().isdigit()}
        except (OSError, ValueError):
            return set()

    def _framework_client_alive(self):
        live = self._qq_process_pids()
        if self.framework_pids:
            return bool(self.framework_pids & live)
        return bool(live)

    def _ensure_framework(self, root):
        """确保官方 QQ 客户端 + Framework 注入在线（不重复拉起已有 QQ）。"""
        self._start_napcat_health(root)
        port = self._napcat_port(root)
        if self._port_open(port):
            self.napcat_started = True
            self.napcat_ready = True
            self._framework_port_seen_ts = time.time()
            self.log("napcat", "本机 QQ 已在线（OneBot 端口 %d）" % port)
            return {"ok": True, "started": False, "port": port,
                    "mode": "framework"}
        fw = self._framework_dir(root)
        if not fw:
            return {"ok": False,
                    "error": "未找到 NapCat.Framework（napimain.exe / napiLoader.bat）"}
        live = self._qq_process_pids()
        recorded = self._read_framework_pids(root)
        ours = recorded & live
        if live:
            self.framework_pids = ours or set(live)
            self.framework_owned = bool(ours)
            self.napcat_started = True
            self._framework_port_seen_ts = time.time()
            self.log("napcat", "检测到本机 QQ 客户端（PID %s）；不会重复拉起"
                     % "、".join(str(p) for p in sorted(self.framework_pids)))
            if not ours:
                self.log("napcat", "该 QQ 由用户启动；若 OneBot 3001 不出现，"
                                   "请关闭它后用本软件重新“启动 QQ”（需要 Framework 注入）")
            self._framework_watch(root, port, live, adopted=True)
            return {"ok": True, "started": False, "port": port, "pending": True,
                    "mode": "framework", "adopted": True,
                    "pids": sorted(self.framework_pids)}
        log_path = os.path.join(root, "data", "qq-framework.log")
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            log_fh = subprocess.DEVNULL
        env = os.environ.copy()
        env["NAPCAT_INJECT_PATH"] = os.path.join(fw, "napiloader.dll")
        env["NAPCAT_LAUNCHER_PATH"] = os.path.join(fw, "napimain.exe")
        env["NAPCAT_MAIN_PATH"] = os.path.join(fw, "nativeLoader.cjs").replace("\\", "/")
        try:
            proc = subprocess.Popen(
                ["cmd.exe", "/c", "napiLoader.bat"], cwd=fw, env=env,
                stdout=log_fh, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            if log_fh is not subprocess.DEVNULL:
                try:
                    log_fh.close()
                except Exception:
                    pass
            self.log("napcat", "启动官方 QQ 失败：%s" % exc)
            return {"ok": False, "error": "启动官方 QQ 失败：%s" % exc}
        if log_fh is not subprocess.DEVNULL:
            try:
                log_fh.close()
            except Exception:
                pass
        self.log("napcat", "正在启动官方 QQ 客户端（NapCat Framework 注入）…")
        self.napcat_started = True
        self.framework_owned = True
        self._framework_launch_ts = time.time()
        self._framework_port_seen_ts = time.time()
        self._framework_watch(root, port, set(), adopted=False)
        return {"ok": True, "started": True, "port": port, "pending": True,
                "mode": "framework", "launcher_pid": proc.pid}

    def _framework_watch(self, root, port, before_pids, adopted=False):
        """后台等待 QQ 出现/登录/OneBot 端口就绪，不阻塞启动流程。"""
        def _watch():
            started_at = time.time()
            deadline = started_at + 180
            login_hint = False
            while time.time() < deadline:
                live = self._qq_process_pids()
                new = live - set(before_pids or ())
                if new and not self.framework_pids:
                    self.framework_pids = set(new)
                    self.framework_owned = True
                    self._record_framework_pids(root, new)
                    self.log("napcat", "官方 QQ 已启动（PID %s）"
                             % "、".join(str(p) for p in sorted(new)))
                if self.framework_pids and not login_hint:
                    login_hint = True
                    if not adopted:
                        self.log("napcat", "请在 QQ 窗口登录；勾选“自动登录”可让下次免扫码")
                if self._port_open(port):
                    self.napcat_ready = True
                    self._framework_port_seen_ts = time.time()
                    self.log("napcat", "本机 QQ 已登录并就绪（OneBot 端口 %d）" % port)
                    return
                if adopted and time.time() - started_at > 45:
                    self.log("napcat", "OneBot 3001 仍未出现：当前 QQ 可能是普通客户端（无注入）。"
                                       "请完全退出 QQ 后，再点“启动机器人”由本软件拉起。")
                    return
                if not live and time.time() - self._framework_launch_ts > 30:
                    self.log("napcat", "QQ 客户端未启动：请确认已安装官方 QQ NT，"
                                       "且 NapCat.Framework 完整（napimain.exe/napiLoader.bat）")
                    return
                time.sleep(2)
            if not self._port_open(port):
                self.log("napcat", "等待本机 QQ / OneBot 端口超时；请完成登录或查看 "
                                   "NapCat.Framework/logs")
        threading.Thread(target=_watch, daemon=True).start()

    def _framework_health_tick(self, root):
        """framework 模式看护：QQ 退出自动拉起；在线但 3001 掉线超 4 分钟重启。"""
        if not self.napcat_started:
            return
        if not self.framework_owned:
            # 用户自己启动的 QQ 不自动结束/重启，避免误杀其他账号
            return
        now = time.time()
        port = self._napcat_port(root)
        if self._port_open(port):
            self._framework_port_seen_ts = now
            return
        if now - self._framework_launch_ts < 60:
            return                                  # 启动/登录宽限期
        if now - self._napcat_last_auto_restart < NAP_AUTO_RESTART_COOLDOWN:
            return
        last = self._framework_port_seen_ts or self._framework_launch_ts or now
        if not self._framework_client_alive():
            self.log("napcat", "【自动恢复】官方 QQ 客户端已退出，重新拉起…")
        elif now - last >= NAP_PORT_LOST_GRACE_SEC:
            self.log("napcat", "【自动恢复】QQ 在线但 OneBot 端口 %d 已掉线 %d 秒，"
                               "重启 QQ 客户端…" % (port, NAP_PORT_LOST_GRACE_SEC))
        else:
            return
        self._restart_napcat(root)

    def _stop_framework(self):
        """停止机器人时保留用户正在使用的官方 QQ 客户端（与父项目行为一致）。"""
        with self.lock:
            self.napcat_proc = None
            self.napcat_started = False
            self.napcat_ready = False
        self.log("napcat", "本机 QQ 客户端保持运行（关闭机器人不会退出 QQ）")

    def _restart_framework(self, root):
        """重启官方 QQ：只结束本管理台拉起的 PID，绝不杀用户自己的 QQ。"""
        self._napcat_last_auto_restart = time.time()
        live = self._qq_process_pids()
        kill = sorted(self.framework_pids & live) if self.framework_owned else []
        if kill:
            for pid in kill:
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, timeout=10,
                                   creationflags=CREATE_NO_WINDOW)
                except Exception:
                    pass
            self.log("napcat", "重启本机 QQ：结束进程 %s" % kill)
        elif live and self.framework_pids and not self.framework_owned:
            self.log("napcat", "本机 QQ 由用户启动，未自动结束；请手动关闭 QQ 后重试")
        self.framework_pids = set()
        self.framework_owned = False
        self.napcat_started = False
        self.napcat_ready = False
        self._wait_port_closed(self._napcat_port(root), timeout=15)
        self.log("napcat", "本机 QQ 重启中…")
        self._ensure_napcat(root)


    @classmethod
    def _napcat_webui_config(cls, root):
        """读取 NapCat WebUI 配置 → (port, token)；读取失败返回 (None, None)。"""
        dirs = []
        if cls._qq_login_mode(root) == "framework":
            fw = cls._framework_dir(root)
            if fw:
                dirs.append(fw)
        napcat_dir = cls._napcat_dir(root)
        if napcat_dir:
            dirs.append(napcat_dir)
        for base_dir in dirs:
            for cfg in ("webui.json",
                        os.path.join("napcat", "config", "webui.json"),
                        os.path.join("config", "webui.json")):
                p = os.path.join(base_dir, cfg)
                if not os.path.isfile(p):
                    continue
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    port = int(data.get("port", 6099))
                    token = str(data.get("token") or "")
                    return port, token
                except Exception:
                    return None, None
        return None, None

    @classmethod
    def _napcat_webui_url(cls, root):
        """读取 NapCat WebUI 配置 → 管理页地址（含 token）。"""
        port, token = cls._napcat_webui_config(root)
        if not port:
            return None
        url = "http://127.0.0.1:%d/webui" % port
        if token:
            url += "?token=" + token
        return url

    # ---------- NapCat WebUI API（登录状态 / 二维码） ----------
    def _napcat_credential(self, root):
        """用 webui.json 的 token 换取 API 凭证（Credential），缓存复用。"""
        port, token = self._napcat_webui_config(root)
        if not port or not token:
            return None
        if self._napcat_cred:
            return self._napcat_cred
        try:
            digest = hashlib.sha256(
                (token + ".napcat").encode("utf-8")).hexdigest()
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/auth/login" % port,
                data=json.dumps({"hash": digest}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            cred = (data.get("data") or {}).get("Credential")
            if cred:
                self._napcat_cred = str(cred)
            return self._napcat_cred
        except Exception:
            return None

    def _napcat_api(self, root, path, body=None):
        """POST NapCat WebUI API；Unauthorized 时换凭证重试一次。"""
        port, _token = self._napcat_webui_config(root)
        if not port:
            return None
        for _ in range(2):
            cred = self._napcat_credential(root)
            if not cred:
                return None
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/api%s" % (port, path),
                    data=(json.dumps(body or {}).encode("utf-8")
                          if body is not None else b"{}"),
                    method="POST",
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + cred})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("code") == 0:
                    return data.get("data") or {}
                if data.get("message") == "Unauthorized":
                    self._napcat_cred = None  # 凭证失效，下一轮重新登录
                    continue
                return {"_error": data.get("message", "未知错误")}
            except Exception as exc:
                return {"_error": str(exc)[:120]}
        return None

    def napcat_login_status(self, root=None):
        """NapCat 登录状态：{logged_in, qrcode_url, login_error, offline}。"""
        root = root or self.project_root
        data = self._napcat_api(root, "/QQLogin/CheckLoginStatus")
        if data is None:
            return {"ok": False, "error": "NapCat WebUI 不可用"}
        if data.get("_error"):
            return {"ok": False, "error": data["_error"]}
        return {
            "ok": True,
            "logged_in": bool(data.get("isLogin")),
            "offline": bool(data.get("isOffline")),
            "qrcode_url": str(data.get("qrcodeurl") or ""),
            "login_error": str(data.get("loginError") or ""),
        }

    def napcat_refresh_qrcode(self, root=None):
        """刷新登录二维码，并**如实**返回是否真的产生了新码。

        NapCat 的 `/QQLogin/RefreshQRcode` 在内部登录流程卡死时依然回
        `code=0 success`，但 `qrcodeurl` 仍为空——旧实现丢掉返回值直接回状态，
        前端于是继续显示缓存里的旧二维码，表现就是"刷新无效"。
        """
        root = root or self.project_root
        before = self.napcat_login_status(root) or {}
        res = self._napcat_api(root, "/QQLogin/RefreshQRcode")
        st = self.napcat_login_status(root) or {}
        new_url = str(st.get("qrcode_url") or "")
        old_url = str(before.get("qrcode_url") or "")
        st["refresh_ok"] = bool(new_url) and (not old_url or new_url != old_url)
        if st.get("logged_in"):
            st["refresh_ok"] = True          # 已登录：刷新无意义，不算失败
            st["refresh_note"] = "已登录"
        elif not st.get("refresh_ok"):
            api_err = (res or {}).get("_error") if isinstance(res, dict) else None
            st["refresh_error"] = (
                str(api_err) if api_err
                else "NapCat 没有生成新二维码（登录流程可能已卡死），建议重启 NapCat")
            # 刷新失败：作废缓存图，别让前端继续显示过期二维码
            self.invalidate_qrcode_cache(root)
        return st

    @classmethod
    def napcat_qrcode_cache_path(cls, root):
        if cls._qq_login_mode(root) == "framework":
            fw = cls._framework_dir(root)
            if fw:
                for rel in (("cache", "qrcode.png"),
                            ("napcat", "cache", "qrcode.png")):
                    p = os.path.join(fw, *rel)
                    if os.path.isfile(p):
                        return p
        napcat_dir = cls._napcat_dir(root)
        if not napcat_dir:
            return None
        return os.path.join(napcat_dir, "napcat", "cache", "qrcode.png")

    @classmethod
    def qrcode_cache_age(cls, root):
        """缓存二维码文件的年龄（秒）；文件不存在返回 None。"""
        p = cls.napcat_qrcode_cache_path(root)
        if not p or not os.path.isfile(p):
            return None
        try:
            return max(0.0, time.time() - os.path.getmtime(p))
        except OSError:
            return None

    @classmethod
    def invalidate_qrcode_cache(cls, root):
        """删掉缓存二维码：过期图比没有图更误导（用户会一直扫一张死码）。"""
        p = cls.napcat_qrcode_cache_path(root)
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                return True
            except OSError:
                pass
        return False

    def napcat_restart(self, root=None, wait=60):
        """重启 QQ 接入（登录二维码卡死时的可靠解法），返回重启后的登录状态。

        shell 模式只结束命令行指向本项目 NapCat 目录的 node 进程；
        framework 模式只结束本管理台拉起的官方 QQ（用户自己的 QQ 不动）。
        """
        root = root or self.project_root
        mode = self._qq_login_mode(root)
        killed = []
        if mode == "framework":
            self._restart_framework(root)
        else:
            if not self._napcat_dir(root):
                return {"ok": False, "error": "未找到 NapCat 目录（NapCat.Shell.Windows.Node）"}
            port = self._napcat_port(root)
            for pid in self._napcat_pids(root):
                try:
                    proc = psutil.Process(pid)
                    proc.terminate()
                    try:
                        proc.wait(timeout=6)
                    except psutil.TimeoutExpired:
                        proc.kill()
                    killed.append(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            self.log("napcat", "重启 NapCat：结束进程 %s" % (killed or "无"))
            with self.lock:
                self.napcat_proc = None
            if not self._wait_port_closed(port, timeout=10):
                return {"ok": False,
                        "error": "NapCat 端口 %d 仍被占用，请手动结束 node 进程后重试" % port}
            try:
                res = self._ensure_napcat(root)         # 重新拉起（与首次启动同一路径）
            except Exception as exc:
                # 已经杀掉了旧的：这里绝不能再抛出去——否则用户落到"一个 NapCat 都没有"
                self.log("napcat", "重启 NapCat 拉起失败（%s），改用直接启动兜底" % exc)
                res = self._launch_napcat_direct(root)
            if not res.get("ok"):
                return res
        self.invalidate_qrcode_cache(root)              # 旧码一律作废
        deadline = time.time() + max(5, int(wait))
        st = {}
        while time.time() < deadline:
            st = self.napcat_login_status(root) or {}
            if st.get("qrcode_url") or st.get("logged_in"):
                break
            time.sleep(1.5)
        out = dict(st)
        out["ok"] = bool(st.get("qrcode_url") or st.get("logged_in"))
        out["restarted"] = True
        out["killed_pids"] = killed
        if out["ok"]:
            out["refresh_ok"] = True
        elif mode == "framework":
            out["error"] = "QQ 重启后仍未就绪：请在官方 QQ 窗口完成登录，或查看 NapCat.Framework 日志"
        else:
            out["error"] = "NapCat 重启后仍未生成二维码，请打开 NapCat 管理页查看"
        return out

    @staticmethod
    def _napcat_port_pid(port):
        """监听指定端口的 PID（psutil 缺失时返回 None）。"""
        if psutil is None:
            return None
        try:
            for c in psutil.net_connections("tcp"):
                if (c.status == psutil.CONN_LISTEN and c.laddr
                        and c.laddr.port == int(port) and c.pid):
                    return int(c.pid)
        except Exception:
            return None
        return None

    @classmethod
    def _napcat_tree(cls, pid, rows):
        """pid 及其所有后代（NapCat 会 fork 子进程）。"""
        out = {pid}
        changed = True
        while changed:
            changed = False
            for rpid, _n, _c, _w, ppid in rows:
                if ppid in out and rpid not in out:
                    out.add(rpid)
                    changed = True
        return out

    def _napcat_cleanup_duplicates(self, root):
        """检测并清掉多余的 NapCat 实例；framework 模式不碰用户 QQ 进程。"""
        if self._qq_login_mode(root) == "framework":
            return []
        launchers = self.napcat_launcher_pids(root)
        if len(launchers) <= 1:
            return []
        rows = self._napcat_proc_table()
        keeper = self._napcat_port_pid(self._napcat_port(root))
        if keeper not in launchers:
            keeper = max(launchers)                 # 没人占端口就留最新拉起的
        keep_tree = self._napcat_tree(keeper, rows)
        victims = set()
        for pid in launchers:
            if pid == keeper:
                continue
            victims |= (self._napcat_tree(pid, rows) - keep_tree)
        killed = []
        for target in sorted(victims):
            try:
                proc = psutil.Process(target)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                killed.append(target)
            except Exception:
                continue
        if killed:
            self.log("napcat", "检测到 %d 个 NapCat 实例，已清理多余进程 %s（保留 PID %s）"
                     % (len(launchers), killed, keeper))
        return killed

    def _launch_napcat_direct(self, root):
        """直接拉起 QQ 接入（重启兜底）；framework 模式走官方 QQ。"""
        if self._qq_login_mode(root) == "framework":
            return self._ensure_framework(root)
        napcat_dir = self._napcat_dir(root)
        if not napcat_dir:
            return {"ok": False, "error": "未找到 NapCat 目录（NapCat.Shell.Windows.Node）"}
        node = shutil.which("node") or os.path.join(napcat_dir, "node.exe")
        log_path = os.path.join(root, "data", "napcat.log")
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            log_fh = subprocess.DEVNULL
        try:
            proc = subprocess.Popen([node, "index.js"], cwd=napcat_dir,
                                    stdout=log_fh, stderr=subprocess.STDOUT,
                                    creationflags=CREATE_NO_WINDOW)
            self._record_napcat_pid(root, proc.pid)
        except Exception as exc:
            return {"ok": False, "error": "启动 NapCat 失败：%s" % exc}
        with self.lock:
            self.napcat_proc = proc
        return {"ok": True, "started": True, "pid": proc.pid,
                "port": self._napcat_port(root)}

    # ---------- NapCat 进程识别（三种判据取并集，避免漏杀/误杀） ----------
    @classmethod
    def _napcat_pidfile(cls, root):
        return os.path.join(root, "data", "napcat.pid")

    @classmethod
    def _record_napcat_pid(cls, root, pid):
        """记录我们拉起的 NapCat 启动器 PID（下次能精确结束它）。"""
        try:
            os.makedirs(os.path.join(root, "data"), exist_ok=True)
            with open(cls._napcat_pidfile(root), "w", encoding="utf-8") as f:
                f.write(str(int(pid)))
        except OSError:
            pass

    @classmethod
    def _napcat_proc_table(cls):
        """[(pid, name, cmdline, cwd, ppid)]；psutil 缺失时用 PowerShell 兜底。"""
        rows = []
        if psutil is not None:
            for proc in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
                try:
                    info = proc.info
                    try:
                        cwd = proc.cwd() or ""
                    except Exception:
                        cwd = ""
                    rows.append((int(info["pid"]), info.get("name") or "",
                                 " ".join(info.get("cmdline") or []), cwd,
                                 info.get("ppid")))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return rows
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                 "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)\" }"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15,
                creationflags=CREATE_NO_WINDOW).stdout
            for line in (out or "").splitlines():
                pid, _, rest = line.partition("	")
                ppid, _, cmd = rest.partition("	")
                if pid.strip().isdigit():
                    rows.append((int(pid.strip()), "node", cmd, "",
                                 int(ppid) if ppid.strip().isdigit() else None))
        except Exception:
            return rows
        return rows

    @classmethod
    def _napcat_pids(cls, root):
        """属于本项目 NapCat 的 node 进程 PID（含子进程）。

        三个判据取并集——只认命令行路径会**漏掉 `node index.js`**（相对路径启动时
        命令行里没有目录），漏掉的实例会一直活着抢 3001/6099 端口与 QQ 会话：
        1. 命令行含 NapCat 目录 + napcat.mjs/index.js
        2. **工作目录就是 NapCat 目录**（覆盖 `node index.js`）
        3. 我们记录的启动器 PID（data/napcat.pid）及其子进程
        """
        napcat_dir = cls._napcat_dir(root)
        if not napcat_dir:
            return []
        key = os.path.normcase(os.path.normpath(napcat_dir))
        rows = cls._napcat_proc_table()
        by_pid = {r[0]: r for r in rows}
        seeds = set()

        recorded = None
        try:
            with open(cls._napcat_pidfile(root), "r", encoding="utf-8") as f:
                recorded = int((f.read() or "0").strip())
        except (OSError, ValueError):
            recorded = None
        if recorded and recorded in by_pid:
            seeds.add(recorded)

        for pid, name, cmd, cwd, _ppid in rows:
            if "node" not in (name or "").lower():
                continue
            low = os.path.normcase(cmd)
            if key and key in low and ("napcat.mjs" in low or "index.js" in low):
                seeds.add(pid)
                continue
            try:
                if cwd and os.path.normcase(os.path.normpath(cwd)) == key                         and ("index.js" in low or "napcat.mjs" in low):
                    seeds.add(pid)
            except (TypeError, ValueError):
                continue

        # 带上子进程：NapCat 会 fork 出真正干活的 node 进程
        pids = set(seeds)
        changed = True
        while changed:
            changed = False
            for pid, _n, _c, _w, ppid in rows:
                if ppid in pids and pid not in pids and "node" in (_n or "").lower():
                    pids.add(pid)
                    changed = True
        return sorted(pids)

    @classmethod
    def napcat_launcher_pids(cls, root):
        """"启动器"进程（`node index.js` / `napcat.mjs`），用于检测多实例。"""
        napcat_dir = cls._napcat_dir(root)
        if not napcat_dir:
            return []
        key = os.path.normcase(os.path.normpath(napcat_dir))
        rows = cls._napcat_proc_table()
        # 先算出全部 NapCat 进程，用于把"父进程也是 NapCat"的子进程排除出启动器
        cands = []
        for pid, name, cmd, cwd, ppid in rows:
            if "node" not in (name or "").lower():
                continue
            low = os.path.normcase(cmd)
            is_launcher = False
            if "napcat.mjs" in low and key and key in low:
                is_launcher = True
            elif "index.js" in low and cwd and                     os.path.normcase(os.path.normpath(cwd)) == key:
                is_launcher = True
            if is_launcher:
                cands.append((pid, ppid))
        all_nap = {pid for pid, _ppid in cands}
        return sorted(pid for pid, ppid in cands if ppid not in all_nap)

    @staticmethod
    def napcat_qrcode_png(url):
        """把二维码内容（qrcodeurl）渲染成 PNG data URL；库缺失返回 None。"""
        if not url:
            return None
        try:
            import qrcode
            qr = qrcode.QRCode(border=2, error_correction=qrcode.ERROR_CORRECT_M)
            qr.add_data(str(url))
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return "data:image/png;base64," + base64.b64encode(
                buf.getvalue()).decode("ascii")
        except Exception:
            return None

    @classmethod
    def napcat_qrcode_png_from_cache(cls, root, max_age=QRCODE_MAX_AGE):
        """读取 NapCat 自己保存的二维码图片（cache/qrcode.png）→ data URL。

        只在**新鲜**时才用：二维码有寿命（QQ 约 2 分钟），
        把一小时前的文件当作"当前二维码"会让用户白扫（旧实现就是这样）。
        """
        p = cls.napcat_qrcode_cache_path(root)
        if not p or not os.path.isfile(p):
            return None
        age = cls.qrcode_cache_age(root)
        if age is not None and age > max_age:
            return None
        try:
            with open(p, "rb") as f:
                raw = f.read()
            return "data:image/png;base64," + base64.b64encode(
                raw).decode("ascii")
        except Exception:
            return None

    def _ensure_napcat(self, root):
        """QQ 平台：确保 QQ 接入在线（framework=官方 QQ，shell=NapCat 无头）。

        framework 模式不会拉起 NapCat.Shell，也不会清理用户自己的 QQ 进程；
        shell 模式保持原有逻辑（自动拉起 + 多实例清理）。
        """
        if self._qq_login_mode(root) == "framework":
            return self._ensure_framework(root)
        try:
            self._napcat_cleanup_duplicates(root)       # 先收拾历史遗留的多实例
        except Exception as exc:
            self.log("napcat", "多实例检查失败（忽略）：%s" % exc)
        self._start_napcat_health(root)
        port = self._napcat_port(root)
        if self._port_open(port):
            self.log("napcat", "NapCat 已在运行（端口 %d）" % port)
            return {"ok": True, "started": False, "port": port}
        with self.lock:
            existing = self.napcat_proc
        if existing is not None and existing.poll() is None:
            # 进程在但端口未就绪（启动中/WebSocket 重启中）→ 后台等待，绝不重复拉起
            self.log("napcat", "NapCat 进程运行中（PID %s），后台等待端口 %d 就绪…"
                     % (existing.pid, port))
            self._napcat_watch(root, port, existing)
            return {"ok": True, "started": False, "port": port, "pending": True}
        napcat_dir = self._napcat_dir(root)
        if not napcat_dir:
            self.log("napcat",
                     "未找到 NapCat.Shell.Windows.Node（index.js），请先安装 NapCat")
            return {"ok": False,
                    "error": "未找到 NapCat 目录（NapCat.Shell.Windows.Node）"}
        node = shutil.which("node") or os.path.join(napcat_dir, "node.exe")
        if not os.path.isfile(node) and not shutil.which("node"):
            self.log("napcat", "未找到 node.exe，无法启动 NapCat")
            return {"ok": False, "error": "未找到 node.exe"}
        # 日志写文件而非管道：NapCat 会 fork 多个子进程，管道写满会阻塞进程
        # 导致 QQ 掉线；文件尾随线程再把新日志喂回桌面管理台
        log_path = os.path.join(root, "data", "napcat.log")
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log_fh = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError as exc:
            self.log("napcat", "NapCat 日志文件创建失败：%s" % exc)
            log_fh = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                [node, "index.js"], cwd=napcat_dir,
                stdout=log_fh, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
            self._record_napcat_pid(root, proc.pid)      # 记录 PID，重启时精确结束
        except Exception as exc:
            if log_fh is not subprocess.DEVNULL:
                try:
                    log_fh.close()
                except Exception:
                    pass
            self.log("napcat", "NapCat 启动失败：%s" % exc)
            return {"ok": False, "error": str(exc)}
        # 子进程已复制句柄：父进程立即关闭，避免每次启动/自动重启泄漏文件句柄
        if log_fh is not subprocess.DEVNULL:
            try:
                log_fh.close()
            except Exception:
                pass
        _assign_job(proc)
        with self.lock:
            self.napcat_proc = proc
            self.napcat_started = True
            self.napcat_log_path = log_path
        self.log("napcat", "正在启动 NapCat（%s，PID %s）…" % (napcat_dir, proc.pid))
        self._start_napcat_tail(proc, log_path)
        self._napcat_watch(root, port, proc)
        return {"ok": True, "started": True, "port": port, "pending": True}

    def _start_napcat_tail(self, proc, log_path):
        """文件尾随：把 napcat.log 的新增内容喂回桌面管理台（带降噪）。"""
        def _tail():
            offset = 0
            if os.path.isfile(log_path):
                offset = os.path.getsize(log_path)
            while proc.poll() is None:
                try:
                    with open(log_path, "r", encoding="utf-8",
                              errors="replace") as f:
                        f.seek(offset)
                        data = f.read()
                        offset = f.tell()
                    for line in data.splitlines():
                        self._log_napcat_line(line)
                except Exception:
                    pass
                time.sleep(1)
            self.log("napcat", "NapCat 进程已退出（代码 %s）" % proc.poll())

        threading.Thread(target=_tail, daemon=True).start()

    def _napcat_watch(self, root, port, proc):
        """后台监控 NapCat 端口就绪（最多 120 秒），不阻塞启动流程。"""
        def _watch():
            deadline = time.time() + 120
            while time.time() < deadline and proc.poll() is None:
                if self._port_open(port):
                    self.log("napcat", "NapCat 就绪（端口 %d）" % port)
                    self.napcat_ready = True
                    return
                time.sleep(2)
            if self._port_open(port):
                self.log("napcat", "NapCat 就绪（端口 %d）" % port)
                self.napcat_ready = True
                return
            if proc.poll() is None:
                self.log("napcat", "NapCat 启动 120 秒仍未就绪，请查看日志或重启")

        threading.Thread(target=_watch, daemon=True).start()

    def _start_napcat_health(self, root):
        """接收静默 / 离线看门狗（全局只启动一次，30 秒一轮）。"""
        if self._napcat_health_started:
            return
        self._napcat_health_started = True

        def _loop():
            while True:
                try:
                    # 每轮读当前 project_root：切换项目后不继续扫旧项目
                    self._napcat_health_tick(self.project_root)
                except Exception:
                    pass
                time.sleep(30)

        threading.Thread(target=_loop, daemon=True).start()

    def _napcat_health_tick(self, root):
        if not self.napcat_started:
            return
        if self._qq_login_mode(root) == "framework":
            return self._framework_health_tick(root)
        proc = self.napcat_proc
        if proc is None or proc.poll() is not None:
            return
        port = self._napcat_port(root)
        if not self._port_open(port):
            return
        now = time.time()
        if now - self._napcat_last_auto_restart < NAP_AUTO_RESTART_COOLDOWN:
            return
        reason = None
        st = self.napcat_login_status(root)
        logged_in = bool(st.get("ok") and st.get("logged_in"))
        if logged_in:
            if self._napcat_login_seen_ts is None:
                self._napcat_login_seen_ts = now
        elif self._napcat_login_seen_ts is None:
            # 本次启动以来从未登录成功（多半正等用户扫码/快速登录）：
            # 收不到消息本来就是正常的，绝不能重启——那会打断正在扫的二维码
            return
        login_age = now - (self._napcat_login_seen_ts or now)
        if st.get("ok") and (st.get("offline") or not logged_in) and \
                login_age >= NAP_OFFLINE_GRACE_SEC:
            reason = "NapCat WebUI 报告账号已离线"
        offline_seen = self._napcat_offline_seen_ts or 0
        if reason is None and offline_seen and \
                now - offline_seen >= NAP_OFFLINE_GRACE_SEC and \
                (self.napcat_last_receive_ts is None or
                 now - self.napcat_last_receive_ts >= NAP_OFFLINE_GRACE_SEC):
            reason = "日志显示账号已离线且 N 秒内无新消息"
        if reason is None and self.napcat_last_receive_ts is not None:
            silent = now - self.napcat_last_receive_ts
            # 刚登录的宽限期：登录/同步历史本身要时间，别立刻判"链路假死"
            if silent >= NAP_SILENT_RESTART_MIN * 60 and \
                    login_age >= NAP_SILENT_RESTART_MIN * 60:
                active = self._bot_last_activity_age()
                if active is not None and \
                        active <= NAP_ACTIVE_WINDOW_MIN * 60:
                    reason = ("接收链路疑似假死：已 %.0f 分钟未收到任何消息，"
                              "但机器人 %.0f 分钟前仍在回复" %
                              (silent / 60.0, active / 60.0))
        if reason is None:
            return
        self.log("napcat", "【自动恢复】%s，重启 NapCat…" % reason)
        self._restart_napcat(root)

    def _bot_last_activity_age(self):
        """机器人最后一次 LLM 活动距今秒数；拿不到返回 None（不触发静默重启）。"""
        try:
            res = self._bot_http("GET", "/api/status")
        except Exception:
            return None
        if not isinstance(res, dict) or not res.get("ok"):
            return None
        try:
            recent = (res.get("data") or {}).get("usage", {}).get("recent") or []
            if not recent:
                return None
            ts = recent[0].get("ts")
            if not ts:
                return None
            parsed = time.mktime(time.strptime(str(ts), "%Y-%m-%d %H:%M:%S"))
            return max(0.0, time.time() - parsed)
        except Exception:
            return None

    def _restart_napcat(self, root):
        """只重启 QQ 接入（机器人有自动重连，无需跟着重启）。"""
        if self._qq_login_mode(root) == "framework":
            return self._restart_framework(root)
        self._napcat_last_auto_restart = time.time()
        self._stop_napcat()
        self.napcat_started = False
        self.napcat_ready = False
        self.napcat_last_receive_ts = None
        self._napcat_offline_seen_ts = None
        self._napcat_login_seen_ts = None
        self._wait_port_closed(self._napcat_port(root), timeout=15)
        self.log("napcat", "NapCat 重启中…")
        self._ensure_napcat(root)

    def _stop_napcat(self):
        if self._qq_login_mode(self.project_root) == "framework":
            return self._stop_framework()
        with self.lock:
            proc = self.napcat_proc
            self.napcat_proc = None
        if proc is not None and proc.poll() is None:
            try:
                # NapCat 会 fork 出多个子进程（Worker 等），必须整棵进程树终止
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                    creationflags=CREATE_NO_WINDOW)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            self.log("napcat", "NapCat 已停止")

    # ---------- 机器人进程 ----------
    def bot_status(self):
        proc = self.bot_proc
        if proc is None or proc.poll() is not None:
            # 聊天指令切换会另起新进程并退出旧进程：旧 PID 失效但端口仍存活时接管
            if self.bot_managed and self._port_open(self.bot_port):
                pid = self.bot_pid or self._port_pid(self.bot_port)
                if pid and not self._pid_is_bot(pid):
                    # 端口被无关进程占用：不报告"运行中"
                    return {"running": False, "pid": None, "platform": None}
                started = self.bot_started_at or 0
                return {
                    "running": True,
                    "pid": pid,
                    "platform": self.bot_platform,
                    "started_at": (time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(started))
                        if started else None),
                    "uptime_sec": int(time.time() - started) if started else 0,
                    "restarted": True,
                }
            return {"running": False, "pid": None, "platform": None}
        started = self.bot_started_at or 0
        return {
            "running": True,
            "pid": proc.pid,
            "platform": getattr(proc, "_platform", None),
            "restarted": False,
            "started_at": (time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(started))
                           if started else None),
            "uptime_sec": int(time.time() - started) if started else 0,
        }

    def start_bot(self, platform="qq"):
        with self.lock:
            if self.bot_proc is not None and self.bot_proc.poll() is None:
                return {"ok": False, "error": "机器人已在运行"}
            self.stop_event.clear()
            if not self.python:
                return {"ok": False, "error": "未找到系统 Python（py -3 / python），请先安装 Python"}
            root = self.project_root
            self.bot_port = self._read_admin_port(root) or self.bot_port
            # 端口被其他进程占用（残留旧机器人等）→ 拒绝启动，避免新进程崩溃或误接管
            if self._port_open(self.bot_port):
                pid = self._port_pid(self.bot_port)
                return {"ok": False,
                        "error": "机器人管理台端口 %d 已被占用（PID %s），"
                                 "请先关闭旧进程再启动" % (self.bot_port, pid or "?")}
            desktop_vault.apply_to_project(root)
            napcat = None
            if str(platform).lower() == "qq":
                napcat = self._ensure_napcat(root)
            cmd = [self.python, "bot.py", "--platform", str(platform)]
            try:
                proc = subprocess.Popen(
                    cmd, cwd=root,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as exc:
                self.log("bot", "启动失败：%s" % exc)
                return {"ok": False, "error": str(exc)}
            _assign_job(proc)
            proc._platform = platform
            self.bot_proc = proc
            self.bot_pid = proc.pid
            self.bot_managed = True
            self.bot_platform = platform
            self.bot_started_at = time.time()
            self.napcat_ready = bool(napcat and napcat.get("ok"))
            self.log("bot", "机器人启动中（%s，PID %s，平台 %s）" % (root, proc.pid, platform))

        def _pump():
            for line in proc.stdout:
                self.log("bot", line.rstrip("\n"))
            self.log("bot", "机器人进程已退出（代码 %s）" % proc.poll())
            self._maybe_adopt()

        threading.Thread(target=_pump, daemon=True).start()
        return {"ok": True, "pid": proc.pid}

    @staticmethod
    def _port_pid(port):
        """按端口找监听进程 PID（Windows netstat）。"""
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "tcp"],
                text=True, errors="replace", timeout=10,
                creationflags=CREATE_NO_WINDOW)
        except Exception:
            return None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in parts:
                local = parts[1] if len(parts) > 1 else ""
                # 精确比对端口：endswith(":8008") 会误命中 :80080
                try:
                    if int(local.rsplit(":", 1)[1]) != int(port):
                        continue
                except (IndexError, ValueError):
                    continue
                try:
                    return int(parts[-1])
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _kill_pid(pid):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/F"],
                capture_output=True, timeout=10,
                creationflags=CREATE_NO_WINDOW)
            return True
        except Exception:
            return False

    @staticmethod
    def _pid_is_bot(pid):
        """判断 PID 是否为本项目的机器人进程（命令行含 bot.py），防误杀无关进程。

        wmic 在 Win11 已弃用：优先 PowerShell CIM 查询，失败回退 wmic。
        """
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine"
                 % int(pid)],
                text=True, errors="replace", timeout=8,
                creationflags=CREATE_NO_WINDOW)
            if out.strip():
                return "bot.py" in out
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                ["wmic", "process", "where",
                 "ProcessId=%d" % int(pid),
                 "get", "CommandLine", "/value"],
                text=True, errors="replace", timeout=5,
                creationflags=CREATE_NO_WINDOW)
            return "bot.py" in out
        except Exception:
            return False

    def _maybe_adopt(self):
        """旧进程退出后，若机器人管理台端口很快重新监听（聊天指令重启），接管新进程。"""
        deadline = time.time() + 15
        while time.time() < deadline and not self.stop_event.is_set():
            if self._port_open(self.bot_port):
                pid = self._port_pid(self.bot_port)
                if pid and self._pid_is_bot(pid):
                    with self.lock:
                        self.bot_pid = pid
                        self.bot_started_at = time.time()
                    self.log("bot", "检测到机器人已重启并接管（PID %s）" % pid)
                    return
                if pid:
                    # 端口被无关进程占用：不接管，避免之后误杀
                    self.log("bot", "端口 %d 被非机器人进程占用（PID %s），不接管"
                             % (self.bot_port, pid))
                    return
            time.sleep(1)
        self.log("bot", "机器人未在 %d 秒内重新监听，判定已退出" % 15)

    @staticmethod
    def _read_admin_port(root):
        """读取项目 config.json 中 admin.port，避免聊天转发硬编码端口。"""
        try:
            with open(os.path.join(root, "config.json"), "r", encoding="utf-8") as f:
                cfg = json.load(f)
            port = int((cfg.get("admin") or {}).get("port", 0))
            return port if 1 <= port <= 65535 else None
        except Exception:
            return None

    def stop_bot(self, explicit=False):
        """停止机器人。

        explicit=True 表示用户在管理台明确点了「停止」：此时即便是别处启动的
        机器人（bot_managed=False）也按端口结束；程序退出/关窗时是 explicit=False，
        只解除跟踪、绝不杀别人的进程。
        """
        self.stop_event.set()
        with self.lock:
            proc = self.bot_proc
            managed = self.bot_managed
            known_pid = self.bot_pid
            self.bot_proc = None
        self.bot_started_at = None
        stopped = False
        killed_pids = set()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
                stopped = True
            except Exception as exc:
                self.log("bot", "停止失败：%s" % exc)
                return {"ok": False, "error": str(exc)}
        if not managed and not explicit:
            # 机器人不是本管理台启动的（例如用户自己双击了启动机器人.bat）：
            # 只放弃跟踪，绝不按端口去杀——此前关窗会把别人的机器人一起杀掉
            self.log("bot", "机器人非本管理台启动，仅解除跟踪（不结束其进程）")
            self._forget_bot()
            return {"ok": True, "stopped": False, "unmanaged": True}
        if not stopped and self._port_open(self.bot_port):
            pid = known_pid or self._port_pid(self.bot_port)
            if pid and self._pid_is_bot(pid):
                self.log("bot", "停止已重启的机器人（PID %s）" % pid)
                self._kill_pid(pid)
                killed_pids.add(int(pid))
                stopped = True
            elif pid:
                # PID 复用/无关进程占用端口：绝不误杀
                self.log("bot", "端口 %d 的进程（PID %s）不是机器人，跳过清理"
                         % (self.bot_port, pid))
        # 杀完确认端口真正释放；仍占用则按端口再补刀（仅限收管/显式停止的场景）
        if (managed or explicit) and not self._wait_port_closed(self.bot_port, timeout=6):
            pid2 = self._port_pid(self.bot_port)
            if pid2 and int(pid2) not in killed_pids and int(pid2) not in (
                    int(proc.pid) if proc is not None else -1,
                    int(known_pid) if known_pid else -1) \
                    and self._pid_is_bot(pid2):
                self.log("bot", "端口 %d 仍被占用，强制结束 PID %s"
                         % (self.bot_port, pid2))
                self._kill_pid(pid2)
                self._wait_port_closed(self.bot_port, timeout=5)
            elif pid2:
                self.log("bot", "端口 %d 仍被 PID %s 占用（可能被外部进程接管）"
                         % (self.bot_port, pid2))
        self._forget_bot()
        self.log("bot", "机器人已停止")
        if getattr(self, "napcat_started", False):
            self._stop_napcat()
            self.napcat_started = False
        return {"ok": True, "stopped": True}

    def _forget_bot(self):
        with self.lock:
            self.bot_proc = None
            self.bot_pid = None
            self.bot_managed = False
            self.bot_platform = None

    # ---------- 依赖安装 ----------
    def install_status(self):
        proc = self.install_proc
        if proc is None or proc.poll() is not None:
            return {"running": False, "pid": proc.pid if proc else None,
                    "started_at": None, "elapsed_sec": None}
        started = self.install_started_at or time.time()
        return {
            "running": True,
            "pid": proc.pid,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(started)),
            "elapsed_sec": int(time.time() - started),
        }

    def install_deps(self):
        with self.lock:
            if self.install_proc is not None and self.install_proc.poll() is None:
                return {"ok": False, "error": "依赖安装已在运行"}
            if not self.python:
                return {"ok": False, "error": "未找到系统 Python，无法安装依赖"}
            root = self.project_root
            req = os.path.join(root, "requirements.txt")
            if not os.path.isfile(req):
                return {"ok": False, "error": "项目目录没有 requirements.txt"}
            cmd = [self.python, "-m", "pip", "install", "-r", "requirements.txt"]
            self.log("dep", "开始安装依赖：%s" % " ".join(shlex.quote(c) for c in cmd))
            try:
                proc = subprocess.Popen(
                    cmd, cwd=root,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as exc:
                self.log("dep", "安装启动失败：%s" % exc)
                return {"ok": False, "error": str(exc)}
            _assign_job(proc)   # 与 bot/NapCat 一致：EXE 被强杀时一并回收
            self.install_proc = proc
            self.install_started_at = time.time()

        def _pump():
            for line in proc.stdout:
                self.log("dep", line.rstrip("\n"))
            self.log("dep", "依赖安装结束（代码 %s）" % proc.poll())
            with self.lock:
                self.install_proc = None
                self.install_started_at = None

        threading.Thread(target=_pump, daemon=True).start()
        return {"ok": True, "pid": proc.pid}

    # ---------- 状态 ----------
    def status(self):
        with self.lock:
            root = self.project_root
            valid = os.path.isfile(os.path.join(root, "bot.py"))
        bot = self.bot_status()
        dep = self.install_status()
        nap = self.napcat_proc
        napcat_port = self._napcat_port(root)
        last_rx = self.napcat_last_receive_ts
        return {
            "project_root": root,
            "project_valid": valid,
            "python": self.python,
            "python_version": self.python_version,
            "bot": bot,
            "install": dep,
            "napcat": {
                "port": napcat_port,
                "running": self._port_open(napcat_port),
                "managed": self.napcat_started,
                "proc_alive": nap is not None and nap.poll() is None,
                "webui_url": self._napcat_webui_url(root),
                "last_receive": (time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(last_rx))
                                 if last_rx else None),
                "silent_secs": (int(time.time() - last_rx)
                                if last_rx else None),
                "mode": self._qq_login_mode(root),
                "framework_available": bool(self._framework_dir(root)),
                "qq_alive": bool(self._qq_process_pids()),
                "qq_pids": sorted(self.framework_pids),
            },
            "vault_path": desktop_vault.vault_path(),
        }

    # ---------- 重启（人格切换等场景） ----------
    def restart_bot(self, platform=None):
        """停止当前机器人并用指定平台（缺省沿用当前）重新启动。"""
        with self.lock:
            proc = self.bot_proc
            running = proc is not None and proc.poll() is None
            old_platform = getattr(proc, "_platform", None) if proc else None
        target = platform or old_platform
        if not target:
            try:
                with open(os.path.join(self.project_root, "config.json"),
                          "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                target = cfg.get("platform", "qq")
            except Exception:
                target = "qq"
        if running:
            res = self.stop_bot()
            if res is not None and not res.get("ok"):
                # 旧进程没停干净就再拉新进程会造成双机器人
                return {"ok": False,
                        "error": "旧机器人停止失败：" + res.get("error", "")}
            time.sleep(0.8)
        return self.start_bot(target)

    # ---------- 聊天控制台代理（转发到机器人自己的 8008 管理台） ----------
    def _bot_http(self, method, path, body=None):
        url = "http://127.0.0.1:%d%s" % (self.bot_port, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        try:
            from qqbot.admin import _admin_token
            headers["X-Admin-Token"] = _admin_token()
        except Exception:
            pass
        req = urllib.request.Request(
            url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return {"ok": False,
                    "error": "无法连接机器人管理台（%d）：%s" % (self.bot_port, exc)}

    def chat(self, text):
        return self._bot_http("POST", "/api/chat", {"text": text})

    def outbox(self, after=0):
        res = self._bot_http("GET", "/api/outbox?after=%d" % int(after))
        if res.get("ok"):
            return res.get("data") or []
        return []


SERVICE = DesktopService()
