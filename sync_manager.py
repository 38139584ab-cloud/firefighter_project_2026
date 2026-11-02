# -*- coding: utf-8 -*-
"""
sync_manager.py
在使用者電腦背景「內嵌」並自動設定 Syncthing，
把收集到的訓練資料夾【單向】同步到開發者電腦。
使用者不需要安裝、開啟或設定任何東西。
"""
import atexit
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import cv2

# ============ 只需要改這一區 ============
# 開發者電腦上 Syncthing 的裝置 ID（網頁介面 → 操作 → 顯示 ID）
DEV_DEVICE_ID = "GL3757I-TZ7CQBD-OSWM2YL-OSXKACA-YKRK5V7-QDHRJTA-6GVL6ZP-5A3O3QE"
DEV_DEVICE_NAME = "developer-pc"
# 正式使用維持 "dynamic"；若在「同一台電腦」測試，改成 "tcp://127.0.0.1:22000"
DEV_ADDRESS = "dynamic"
SYNCTHING_VERSION = "1.29.6"
SYNCTHING_ZIP_SHA256 = ""  # 選填：若自動下載，建議填入官方 zip 的 SHA256 做驗證
# ========================================

DEV_ID = DEV_DEVICE_ID.strip().upper()

BASE_DIR = Path(__file__).resolve().parent
COLLECT_DIR = str(BASE_DIR / "data")        # 要同步的資料夾（沿用您原本的 data）
ST_BIN_DIR = BASE_DIR / "syncthing"          # syncthing 執行檔位置
ST_HOME = BASE_DIR / "syncthing_home"        # syncthing 設定與金鑰
IS_WIN = platform.system() == "Windows"
EXE_NAME = "syncthing.exe" if IS_WIN else "syncthing"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0


def atomic_imwrite(path, frame, quality=90):
    """先寫成 .tmp 再改名，避免 Syncthing 抓到寫到一半的檔案。
    （同時解決 cv2.imwrite 無法寫入含中文路徑的問題）"""
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("影像編碼失敗")
    tmp = str(path) + ".tmp"
    buf.tofile(tmp)
    os.replace(tmp, path)


def _make_kill_on_close_job():
    """Windows：建立 Job Object，讓 syncthing 跟著 Python 一起結束
    （即使使用者直接按 X 關閉黑視窗、atexit 沒機會執行也一樣）。失敗時回傳 None。"""
    if not IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return job, k32
    except Exception:
        return None


def _kill_leftover():
    """清掉上一次殘留的 syncthing（例如異常關閉）。
    避免：資料庫被鎖、新舊設定打架、以及使用者已拒絕收集但舊行程還在傳資料。"""
    pid_file = ST_HOME / "syncthing.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        return
    try:
        if IS_WIN:  # 只在行程名稱確實是 syncthing.exe 時才殺，避免 PID 被別的程式重用
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, creationflags=NO_WINDOW).stdout
            if EXE_NAME.lower() in out.decode("utf-8", "ignore").lower():
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, creationflags=NO_WINDOW)
    except Exception:
        pass
    try:
        pid_file.unlink()
    except Exception:
        pass


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _set(parent, tag, text):
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    el.text = text


class SyncManager:
    def __init__(self):
        self.proc = None
        self.state = "未啟動"
        self.folder_id = None
        self.api_key = None
        self.gui_port = None
        self._want = False
        self._busy = False
        self._job = None
        self._lock = threading.Lock()
        atexit.register(self.stop)

    # ---------- 對外介面 ----------
    def start(self):
        """非阻塞；可重複呼叫。第一次會花幾秒準備（產生金鑰、下載等）。"""
        with self._lock:
            self._want = True
            if self._busy or (self.proc and self.proc.poll() is None):
                return
            self._busy = True
        threading.Thread(target=self._start_worker, daemon=True).start()

    def stop(self):
        with self._lock:
            self._want = False
            proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        _kill_leftover()
        self.state = "已停止"

    def get_status(self):
        if self.proc is not None and self.proc.poll() is not None:
            return "❌ 同步程式已意外結束（詳見 syncthing_home/syncthing.log）"
        if self.proc is None:
            return self.state
        try:
            conns = self._api("/rest/system/connections")["connections"]
            if not conns.get(DEV_ID, {}).get("connected"):
                return "🟡 尚未連線到開發端（資料先存本機，連線後會自動補傳）"
            c = self._api(f"/rest/db/completion?folder={self.folder_id}&device={DEV_ID}")
            return f"🟢 已連線開發端，同步進度 {c.get('completion', 0):.0f}%"
        except urllib.error.HTTPError:
            return "🟡 已連線，等待開發端接受資料夾"
        except Exception:
            return "🟡 同步程式啟動中…"

    # ---------- 內部流程 ----------
    def _start_worker(self):
        try:
            self.state = "準備中…"
            _kill_leftover()
            exe = self._ensure_binary()
            os.makedirs(COLLECT_DIR, exist_ok=True)
            env = {
                **os.environ,
                "STNODEFAULTFOLDER": "1",  # 不要建立預設的 Sync 資料夾
                "STNOUPGRADE": "1",        # 版本由您打包時決定
                "STNORESTART": "1",        # 不用 monitor 子行程，terminate 才收得乾淨
            }
            if not (ST_HOME / "config.xml").exists():
                ST_HOME.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [str(exe), "generate", "--home", str(ST_HOME)],
                    check=True, env=env, capture_output=True,
                    timeout=120, creationflags=NO_WINDOW,
                )
            self._prepare_config()
            if not self._want:
                return
            log = open(ST_HOME / "syncthing.log", "ab")
            proc = subprocess.Popen(
                [str(exe), "--home", str(ST_HOME), "--no-browser"],
                env=env, stdout=log, stderr=log, creationflags=NO_WINDOW,
            )
            (ST_HOME / "syncthing.pid").write_text(str(proc.pid))
            job = _make_kill_on_close_job()
            if job:
                self._job = job[0]  # 保留參考；Python 結束時 Windows 會連帶結束 syncthing
                job[1].AssignProcessToJobObject(job[0], int(proc._handle))
            with self._lock:
                self.proc = proc
            self.state = "執行中"
        except Exception as e:
            self.state = f"❌ 同步啟動失敗：{e}"
        finally:
            self._busy = False

    def _ensure_binary(self):
        exe = ST_BIN_DIR / EXE_NAME
        ver_file = ST_BIN_DIR / "version.txt"
        if exe.exists():
            # 手動放入的執行檔（沒有 version.txt）一律沿用；自動下載的則比對版本
            if not ver_file.exists() or ver_file.read_text().strip() == SYNCTHING_VERSION:
                return exe
        if not IS_WIN:
            raise RuntimeError(f"找不到 {exe}，請先把 Syncthing 執行檔放到該位置")
        arch = "arm64" if "ARM" in platform.machine().upper() else "amd64"
        name = f"syncthing-windows-{arch}-v{SYNCTHING_VERSION}"
        url = f"https://github.com/syncthing/syncthing/releases/download/v{SYNCTHING_VERSION}/{name}.zip"
        ST_BIN_DIR.mkdir(parents=True, exist_ok=True)
        zip_path = ST_BIN_DIR / f"{name}.zip"
        urllib.request.urlretrieve(url, zip_path)
        if SYNCTHING_ZIP_SHA256:
            digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
            if digest.lower() != SYNCTHING_ZIP_SHA256.lower():
                zip_path.unlink()
                raise RuntimeError("Syncthing 下載檔 SHA256 不符，已中止")
        with zipfile.ZipFile(zip_path) as z, z.open(f"{name}/syncthing.exe") as src, open(exe, "wb") as dst:
            shutil.copyfileobj(src, dst)
        zip_path.unlink()
        ver_file.write_text(SYNCTHING_VERSION)
        return exe

    def _prepare_config(self):
        """每次啟動都重新套用設定（可重複執行）：只認開發端一台裝置、資料夾為 Send Only。"""
        cfg_path = ST_HOME / "config.xml"
        tree = ET.parse(cfg_path)
        root = tree.getroot()

        me = next(d for d in root.findall("device") if d.get("id") != DEV_ID)
        my_id = me.get("id")
        # 每位使用者一個獨立資料夾 ID，開發端會各自存成獨立資料夾，不會互相混在一起
        self.folder_id = f"fire-detect-{my_id[:7].lower()}"

        for el in root.findall("folder"):
            root.remove(el)
        for d in root.findall("device"):
            if d is not me:
                root.remove(d)

        dev = ET.SubElement(root, "device", id=DEV_ID, name=DEV_DEVICE_NAME,
                            compression="metadata", introducer="false")
        ET.SubElement(dev, "address").text = DEV_ADDRESS

        folder = ET.SubElement(root, "folder", id=self.folder_id, label=self.folder_id,
                               path=COLLECT_DIR, type="sendonly", rescanIntervalS="60",
                               fsWatcherEnabled="true", fsWatcherDelayS="10", ignorePerms="true")
        ET.SubElement(folder, "device", id=my_id)
        ET.SubElement(folder, "device", id=DEV_ID)

        gui = root.find("gui")
        self.gui_port = _free_port()
        gui.set("enabled", "true")
        gui.set("tls", "false")
        _set(gui, "address", f"127.0.0.1:{self.gui_port}")  # 只綁本機
        self.api_key = gui.findtext("apikey")

        opts = root.find("options")
        for el in opts.findall("listenAddress"):
            opts.remove(el)
        port = _free_port()  # 隨機埠，避免跟使用者自己的 Syncthing 衝突
        for addr in (f"tcp://0.0.0.0:{port}", f"quic://0.0.0.0:{port}",
                     "dynamic+https://relays.syncthing.net/endpoint"):
            ET.SubElement(opts, "listenAddress").text = addr
        for tag, val in {
            "startBrowser": "false",
            "urAccepted": "-1",              # 不回報使用統計
            "crashReportingEnabled": "false",
            "localAnnounceEnabled": "false", # 避免與使用者自己的 Syncthing 搶 21027
            "autoUpgradeIntervalH": "0",
        }.items():
            _set(opts, tag, val)

        tree.write(cfg_path, encoding="utf-8", xml_declaration=True)

        os.makedirs(os.path.join(COLLECT_DIR, ".stfolder"), exist_ok=True)
        ignore = os.path.join(COLLECT_DIR, ".stignore")
        if not os.path.exists(ignore):
            with open(ignore, "w", encoding="utf-8") as f:
                f.write("*.tmp\n")  # 寫到一半的暫存檔不要同步

    def _api(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.gui_port}{path}",
            headers={"X-API-Key": self.api_key},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)
