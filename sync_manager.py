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
import time
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
# 請改成您實際測試過的版本；建議直接把 syncthing.exe 放進 syncthing/ 資料夾一起打包
SYNCTHING_VERSION = "1.29.6"
SYNCTHING_ZIP_SHA256 = ""  # 選填：若自動下載，建議填入官方 zip 的 SHA256 做驗證
# ========================================

DEV_ID = DEV_DEVICE_ID.strip().upper()

BASE_DIR = Path(__file__).resolve().parent
COLLECT_DIR = str(BASE_DIR / "data")        # 要同步的資料夾（沿用您原本的 data）
# 模型發佈：每位使用者一個專屬資料夾（fire-model-<裝置ID前7碼>），
# 開發端 Send Only → 該使用者 Receive Only；只分享給那一台裝置，使用者之間看不到彼此的模型
MODEL_FOLDER_PREFIX = "fire-model-"
MODEL_DIR = str(BASE_DIR / "models_sync")    # 收到的模型放這裡（不要與 git 追蹤的 models/ 混用）
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
                _kill_tree(pid)
    except Exception:
        pass
    try:
        pid_file.unlink()
    except Exception:
        pass


def _machine_fingerprint():
    """這台電腦的識別碼。用來偵測 syncthing_home 是不是從別台電腦複製來的。"""
    try:
        if IS_WIN:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                return winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        pass
    import uuid
    return f"{platform.node()}-{uuid.getnode()}"


def _ensure_home_belongs_to_this_machine():
    """若 syncthing_home 不是在這台電腦產生的（例如整個專案資料夾被壓縮傳給別人），
    就整個刪掉重新產生，否則多台電腦會共用同一個裝置 ID，互相把連線踢掉。"""
    marker = ST_HOME / "machine.txt"
    if not ST_HOME.exists():
        return
    try:
        same = marker.read_text().strip() == _machine_fingerprint()
    except Exception:
        same = False
    if not same:
        shutil.rmtree(ST_HOME, ignore_errors=True)


def _kill_tree(pid):
    """結束行程與它底下所有子行程（Syncthing 若以 monitor 模式啟動會有子行程）。"""
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, creationflags=NO_WINDOW)
    else:
        try:
            os.kill(pid, 15)
        except Exception:
            pass


_hash_cache = {}


def find_synced_model():
    """回傳 (模型路徑, 版本字串)；沒有可用的新模型就回傳 None。
    只接受 manifest.json 指到的檔案，且 SHA256 必須吻合（擋掉傳到一半或損毀的檔案）。"""
    try:
        info = json.loads((Path(MODEL_DIR) / "manifest.json").read_text(encoding="utf-8"))
        path = Path(MODEL_DIR) / Path(info["file"]).name  # 只取檔名，防止路徑穿越
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        digest = _hash_cache.get(key)
        if digest is None:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            digest = _hash_cache[key] = h.hexdigest()
        if digest.lower() != str(info["sha256"]).lower():
            return None
        return str(path), str(info.get("version", path.stem))
    except Exception:
        return None


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
        self.model_folder_id = None
        self.api_key = None
        self.gui_port = None
        self._want = False
        self._busy = False
        self._job = None
        self._upload_data = False  # 使用者是否同意上傳資料；False 時資料夾暫停、只收模型
        self._lock = threading.Lock()
        atexit.register(self.stop)

    # ---------- 對外介面 ----------
    def start(self, upload_data=False):
        """非阻塞；可重複呼叫。Syncthing 啟動後一直在背景接收模型更新；
        upload_data=True（使用者已同意）才會恢復上傳 data 資料夾，False 則暫停上傳。"""
        with self._lock:
            self._want = True
            self._upload_data = bool(upload_data)
            if self._busy:
                return  # 啟動流程結束時會套用最新的 _upload_data
            if self.proc and self.proc.poll() is None:
                threading.Thread(target=self._apply_pause_state, daemon=True).start()
                return
            self._busy = True
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _apply_pause_state(self, wait=False):
        """不重啟 Syncthing，直接用 API 暫停/恢復 data 資料夾。"""
        deadline = time.time() + (30 if wait else 0)
        while True:
            try:
                self._api(f"/rest/config/folders/{self.folder_id}", "PATCH",
                          {"paused": not self._upload_data})
                return True
            except Exception:
                if time.time() >= deadline:
                    return False
                time.sleep(1)

    def stop(self):
        with self._lock:
            self._want = False
            proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        _kill_leftover()
        self.state = "已停止"

    def get_status(self):
        if DEV_ADDRESS != "dynamic" and "127.0.0.1" in DEV_ADDRESS:
            return "⚠️ 目前是同機測試位址（DEV_ADDRESS），別台電腦無法同步"
        if self.proc is not None and self.proc.poll() is not None:
            return "❌ 同步程式已意外結束（詳見 syncthing_home/syncthing.log）"
        if self.proc is None:
            return self.state
        try:
            conns = self._api("/rest/system/connections")["connections"]
            if not conns.get(DEV_ID, {}).get("connected"):
                return "🟡 尚未連線到開發端" + ("（資料先存本機，連線後會自動補傳）" if self._upload_data else "")
            if not self._upload_data:
                return "🟢 已連線開發端（僅接收模型更新，未上傳任何資料）"
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
            _ensure_home_belongs_to_this_machine()
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
            (ST_HOME / "machine.txt").write_text(_machine_fingerprint())
            self._prepare_config()
            if not self._want:
                return
            log = open(ST_HOME / "syncthing.log", "ab")
            proc = subprocess.Popen(
                [str(exe), "--home", str(ST_HOME), "--no-browser", "--no-restart"],  # 不要 monitor 子行程
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
            time.sleep(1.5)
            if proc.poll() is not None:
                self.state = "❌ 同步程式一啟動就結束（可能已有另一個實例在跑，詳見 syncthing_home/syncthing.log）"
            else:
                self._apply_pause_state(wait=True)  # 啟動期間若使用者改了同意狀態，這裡補套用
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
        self.model_folder_id = f"{MODEL_FOLDER_PREFIX}{my_id[:7].lower()}"

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
                               paused="false" if self._upload_data else "true",
                               fsWatcherEnabled="true", fsWatcherDelayS="10", ignorePerms="true")
        ET.SubElement(folder, "device", id=my_id)
        ET.SubElement(folder, "device", id=DEV_ID)

        # 專屬模型資料夾：只接收（Receive Only）；開發端要建立同 ID、Send Only 的資料夾（dev_admin.py 會處理）
        mfolder = ET.SubElement(root, "folder", id=self.model_folder_id, label=self.model_folder_id,
                                path=MODEL_DIR, type="receiveonly", rescanIntervalS="3600",
                                fsWatcherEnabled="true", fsWatcherDelayS="10", ignorePerms="true")
        ET.SubElement(mfolder, "device", id=my_id)
        ET.SubElement(mfolder, "device", id=DEV_ID)
        os.makedirs(os.path.join(MODEL_DIR, ".stfolder"), exist_ok=True)

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

    def _api(self, path, method="GET", body=None):
        headers = {"X-API-Key": self.api_key}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{self.gui_port}{path}",
                                     data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=3) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
