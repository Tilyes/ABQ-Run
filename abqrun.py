# -*- coding: utf-8 -*-
"""
ABQ-Run - 轻量级 Abaqus 作业排队界面
=====================================

对标 LS-DYNA 的 LS-Run：把一批 .inp 排队，按设定的并发数依次提交
`abaqus job=... interactive`，用浏览器界面实时看状态、步/增量、日志。

只依赖 Python 标准库（3.8+）。启动后自动打开浏览器。

命令行参数：
    --port 8770        监听端口
    --host 127.0.0.1   监听地址
    --no-browser       不自动打开浏览器
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

# ThreadingHTTPServer / subprocess.run(capture_output=...) 都要 3.7+；
# 这个检查必须放在下面那行 import 之前，否则旧版 Python 会先抛 ImportError。
if sys.version_info < (3, 7):
    sys.stderr.write(
        "\nABQ-Run 需要 Python 3.7 或更高版本，当前是 %d.%d。\n"
        "装一个 3.8+ 之后重新双击 ABQ-Run.bat 即可（不需要第三方库）。\n\n"
        % sys.version_info[:2])
    raise SystemExit(1)

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, unquote

try:
    import winsound
except ImportError:  # 非 Windows
    winsound = None

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
# 允许用环境变量把状态目录挪走（跑测试/多实例时不互相踩）
STATE_DIR = os.environ.get("ABQRUN_STATE") or os.path.join(ROOT, "_state")
LOG_DIR = os.path.join(STATE_DIR, "logs")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
APP_VERSION = "1.0"

IS_WIN = os.name == "nt"


def _runtime_tag():
    """当前跑的是不是项目自带的 runtime\\python.exe（绿色运行时）。"""
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable)).lower()
        rt = os.path.abspath(os.path.join(ROOT, "runtime")).lower()
        return "（项目内置，免安装）" if exe_dir == rt else ""
    except Exception:
        return ""

# Windows 下让子进程不弹黑框，并支持按进程树 kill
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

DEFAULT_ABAQUS = "abaqus"
if IS_WIN:
    # Abaqus 一般把启动器放在 <盘>:\\SIMULIA\\Commands，版本脚本叫 abq2020.bat 之类。
    # 这里把常见位置都试一遍，换台机器基本不用手改。
    def _abaqus_candidates():
        roots = []
        if os.environ.get("SIMULIA"):
            roots.append(os.environ["SIMULIA"])
        for drive in ("C:", "D:", "E:", "F:"):
            roots.append(drive + "\\SIMULIA")
        pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
        roots.append(os.path.join(pf, "SIMULIA"))
        out = [os.path.join(r, "Commands", "abaqus.bat") for r in roots]
        for r in roots:
            for year in range(2026, 2015, -1):
                out.append(os.path.join(r, "Commands", "abq%d.bat" % year))
        return out

    for _cand in _abaqus_candidates():
        if os.path.exists(_cand):
            DEFAULT_ABAQUS = _cand
            break
    else:
        _which = shutil.which("abaqus")
        if _which:
            DEFAULT_ABAQUS = _which


def abaqus_available(cmd):
    """这个 abaqus 命令现在能不能用（用于启动时提醒，不阻断）。"""
    c = (cmd or "").strip().strip('"')
    if not c:
        return False
    if os.sep in c or "/" in c or ":" in c:
        return os.path.exists(c)
    return bool(shutil.which(c.split()[0]))

DEFAULT_SETTINGS = {
    "abaqus_cmd": DEFAULT_ABAQUS,
    "max_jobs": 1,          # 同时运行的作业数
    "max_cpus": 0,          # 同时占用的总核数上限，0 = 不限
    "default_cpus": 4,
    "default_memory": "8 gb",
    "default_precision": "",  # "" | double=explicit | double=constraint | double=both
    "default_gpus": 0,        # 0 = 不用 GPU；N = gpus=N（只对 Abaqus/Standard 有意义）
    "extra_args": "",       # 追加到 abaqus 命令行的公共参数
    "paused": False,
    "clear_lck": True,      # 启动前清理残留 .lck
    "beep": True,           # 全部完成后响一声
    "open_folder_cmd": "",
    "odb_open_mode": "cae",  # 打开 ODB 用 cae 还是 viewer
}

# 实测（Abaqus 2020 启动器）：只有这三个值被接受，double=standard 会被拒
PRECISION_CHOICES = {
    "": "单精度（Abaqus 默认）",
    "double=explicit": "双精度 · Explicit",
    "double=constraint": "双精度 · 约束",
    "double=both": "双精度 · 全部",
}

STATUS_TEXT = {
    "queued": "排队中",
    "running": "运行中",
    "done": "已完成",
    "failed": "失败",
    "canceled": "已取消",
    "interrupted": "已中断",
}

_lock = threading.RLock()


# --------------------------------------------------------------------------
# 持久化
# --------------------------------------------------------------------------
def ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# INP 解析：抓每个 step 的名字与时长，用于估算总进度
# --------------------------------------------------------------------------
# 各分析类型的“进度标尺”不一样：
#   *Dynamic / *Static / *Visco ...   -> 时间
#   *Steady State Dynamics            -> 频率区间
#   *Frequency                        -> 模态数
TIME_KEYWORDS = {
    "dynamic", "dynamic explicit", "static", "visco", "soils",
    "coupled temperature-displacement", "coupled thermal-displacement",
    "heat transfer", "mass diffusion", "geostatic", "coupled pore fluid stress",
    "direct cyclic", "coupled temperature-displacement, explicit",
}
MODE_KEYWORDS = {"frequency"}
SSD_KEYWORDS = {"steady state dynamics"}


def _split_fields(s):
    return [x.strip() for x in s.split(",")]


def _numeric(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def scan_inp_meta(path):
    """一次遍历拿到：各步定义 + 求解器类型。

    返回 {"steps": [...], "nsteps": N, "solver": "explicit|standard|modal",
          "procs": ["Dynamic, Explicit", ...]}

    solver 判定：
      - 任一步的过程关键字里带 explicit -> explicit（Abaqus 不允许显隐混合，出现即整体显式）
      - 所有步都是 *Frequency            -> modal（模态/频率）
      - 其余                              -> standard（隐式）
    """
    steps = []
    cur = None
    pending = None
    opened = True
    try:
        with open(path, "r", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("**"):
                    continue
                if line.startswith("*"):
                    head, _, rest = line[1:].partition(",")
                    kw = head.strip().lower()
                    if kw == "step":
                        name = ""
                        for item in _split_fields(rest):
                            k, _, v = item.partition("=")
                            if k.strip().lower() == "name":
                                name = v.strip()
                        cur = {"name": name or "Step-%d" % (len(steps) + 1),
                               "kind": "unknown", "a": None, "b": None, "n": None,
                               "proc": ""}
                        steps.append(cur)
                        pending = None
                    elif cur is None:
                        pending = None
                    else:
                        is_proc = (kw in TIME_KEYWORDS or kw in MODE_KEYWORDS
                                   or kw in SSD_KEYWORDS)
                        if is_proc and not cur["proc"]:
                            cur["proc"] = line[1:].strip()
                        if kw in TIME_KEYWORDS:
                            cur["kind"] = "time"
                            pending = "time"
                        elif kw in SSD_KEYWORDS:
                            cur["kind"] = "ssd"
                            pending = "ssd"
                        elif kw in MODE_KEYWORDS:
                            cur["kind"] = "mode"
                            pending = "mode"
                        else:
                            pending = None
                    continue
                if pending and cur is not None:
                    vals = [_numeric(x) for x in _split_fields(line)]
                    if pending == "time":
                        pick = None
                        if len(vals) > 1 and vals[1] and vals[1] > 0:
                            pick = vals[1]
                        elif vals and vals[0] and vals[0] > 0:
                            pick = vals[0]
                        if pick:
                            cur["a"] = pick
                    elif pending == "ssd":
                        cur["a"] = vals[0] if vals and vals[0] is not None else 0.0
                        cur["b"] = vals[1] if len(vals) > 1 else None
                        if cur["b"] is None:
                            cur["kind"] = "unknown"
                    elif pending == "mode":
                        n = vals[0] if vals else None
                        if n:
                            cur["n"] = int(n)
                        else:
                            cur["kind"] = "unknown"
                    pending = None
    except OSError:
        opened = False

    if not opened:
        # 文件打不开（被删/路径失效）就别猜类型，让界面显示 "—"
        return {"steps": [], "nsteps": 0, "solver": "", "procs": []}

    solver = "standard" if steps else ""   # 没有 *Step 就别硬猜
    if any("explicit" in (s["proc"] or "").lower() for s in steps):
        solver = "explicit"
    elif steps and all(s["kind"] == "mode" for s in steps):
        solver = "modal"
    return {
        "steps": steps,
        "nsteps": len(steps),
        "solver": solver,
        "procs": [s["proc"] for s in steps],
    }


def scan_inp_steps(path):
    """扫 INP 里的 step 定义，返回 [{name, kind, a, b, n, proc}]，解析失败不抛异常。"""
    return scan_inp_meta(path)["steps"]


# --------------------------------------------------------------------------
# .sta 解析：步 / 增量 / 时间
#
# Abaqus 2020 实测两种行格式（列数随分析类型变）：
#   Standard: "   1     1   1     0     0     0  0.00   1.00e-36   1.000e-36"
#   Explicit: "   2   161                        135."
# 因此不按固定列宽解析，统一取“前两个整数是 STEP/INC，最后一个数字字段是时间值”。
# --------------------------------------------------------------------------
def _to_float(tok):
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None


# Explicit 的进度行：第 4 列是 hh:mm:ss 的 CPU 时间，用这个特征和数据行区分开，
# 避免把 "Most critical elements" 那张表（653 10 2.28E-08 ...）误当成进度。
EXP_ROW = re.compile(r"^\s*(\d+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+\d+:\d+:\d+")
EXP_STEP = re.compile(r"^\s*STEP\s+(\d+)\s+ORIGIN", re.I)


def _sta_layout(path):
    """看文件头判断是 Standard 还是 Explicit 写的 .sta。"""
    try:
        with open(path, "r", errors="ignore") as f:
            head = f.read(300)
    except OSError:
        return "unknown"
    if "Abaqus/Explicit" in head:
        return "explicit"
    if "Abaqus/Standard" in head:
        return "standard"
    return "unknown"


def read_sta_tail(path, max_bytes=262144):
    """读 .sta 尾部，返回行列表。Standard 取末列时间，Explicit 取 STEP/TOTAL TIME 两列。"""
    try:
        size = os.path.getsize(path)
        with open(path, "r", errors="ignore") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            lines = f.readlines()
    except OSError:
        return []

    layout = _sta_layout(path)

    if layout == "explicit":
        rows = []
        step_no = 1
        for ln in lines:
            m = EXP_STEP.match(ln)
            if m:
                step_no = int(m.group(1))
                continue
            m = EXP_ROW.match(ln)
            if m:
                inc = int(m.group(1))
                step_time = _to_float(m.group(2))
                total_time = _to_float(m.group(3))
                if total_time is None:
                    continue
                # 按步不翻转时，累计时间就是本步时间
                rows.append({"step": step_no, "inc": inc,
                             "t": total_time, "incTime": step_time})
        return rows

    # Standard：前两个整数是 STEP/INC，最后一个数字字段是时间
    rows = []
    for ln in lines:
        toks = ln.split()
        if len(toks) < 3:
            continue
        try:
            step = int(toks[0])
            inc = int(toks[1])
        except ValueError:
            continue
        tval = None
        for t in reversed(toks[2:]):
            tval = _to_float(t)
            if tval is not None:
                break
        if tval is None:
            continue
        rows.append({"step": step, "inc": inc, "t": tval})
    return rows


def _step_fraction(step, t, inc, steps, si):
    """当前步完成了多少（0~1）；算不出来返回 None。"""
    kind = step.get("kind")
    if kind == "time":
        d = step.get("a")
        if not d or d <= 0:
            return None
        if -1e-12 <= t <= d * 1.02:
            return max(0.0, min(1.0, t / d))
        prev = sum(x["a"] for x in steps[: si - 1]
                   if x.get("kind") == "time" and x.get("a"))
        if prev < t <= prev + d * 1.02:      # .sta 里给的是累计时间
            return max(0.0, min(1.0, (t - prev) / d))
        return None
    if kind == "ssd":
        f0 = step.get("a") or 0.0
        f1 = step.get("b")
        if not f1 or f1 == f0 or not (f0 - 1e-9 <= t <= f1 * 1.0001):
            return None
        return max(0.0, min(1.0, (t - f0) / (f1 - f0)))
    if kind == "mode":
        m = step.get("n")
        if not m:
            return None
        return max(0.0, min(1.0, float(inc) / m))
    return None


def compute_progress(job):
    """从 .sta 取当前步/增量，按步类型算进度。

    多步作业按“已完成步数 + 当前步内的比例”平均，避免不同类型步之间无法比时间。
    """
    d, name = job["workdir"], job["name"]
    sta_path = os.path.join(d, name + ".sta")
    try:
        if job.get("started") and os.path.getmtime(sta_path) < job["started"] - 1.0:
            return job.get("progress") or {}      # 目录里是上一次跑剩的 .sta
    except OSError:
        return job.get("progress") or {}

    rows = read_sta_tail(sta_path)
    if not rows:
        return job.get("progress") or {}
    last = rows[-1]
    steps = job.get("steps") or []
    n = len(steps)
    frac = pct = None
    si = last["step"]
    if n and 1 <= si <= n:
        frac = _step_fraction(steps[si - 1], last["t"], last["inc"], steps, si)
        if frac is not None:
            pct = max(0.0, min(99.9, ((si - 1) + frac) / n * 100.0))

    return {
        "step": si,
        "inc": last["inc"],
        "tval": last["t"],
        "frac": frac,
        "nsteps": n,
        "stepName": steps[si - 1]["name"] if n and 1 <= si <= n else "",
        "kind": steps[si - 1]["kind"] if n and 1 <= si <= n else "unknown",
        "pct": pct,
        "eta": (job.get("progress") or {}).get("eta"),
    }


def file_text(path, started=None, lines=400, max_bytes=300000):
    """读文件尾部；给了 started 就忽略比它更旧的文件（避免读到上次运行的残留）。"""
    try:
        if started and os.path.getmtime(path) < started - 1.0:
            return ""
    except OSError:
        return ""
    return tail_text(path, lines, max_bytes)


PROB_ERR = ("***ERROR", "Abaqus Error", "exited with errors")
# .log 里 Abaqus Error 的续行是顶格写的，靠这两个开头认出来
LOG_CONT = ("status file", "message file", "for further information",
            "for more information")
# 这些是"作业被外部终止"的告示，不是出错根源，排序时靠后
PROB_NOISE = ("external request", "SIGTERM", "SIGINT")
_prob_cache = {}


def scan_problems(job, limit=300):
    """把 .dat/.msg/.sta/.log/输出 里的 ***ERROR / ***WARNING 抽出来，合并续行并去重。

    Abaqus 的告警常写成多行（首行 ***WARNING: ...，后面缩进续行），这里按块收集。
    """
    d, name = job["workdir"], job["name"]
    srcs = [("输出", os.path.join(LOG_DIR, job["id"] + ".out"))]
    for ext in (".dat", ".msg", ".sta", ".log"):
        srcs.append((ext.lstrip("."), os.path.join(d, name + ext)))

    items, seen = [], {}
    n_err = n_warn = 0

    def flush(buf, level, fname, line, path):
        nonlocal n_err, n_warn
        if not buf:
            return
        text = " ".join(buf)
        if level == "error":
            n_err += 1
        else:
            n_warn += 1
        key = level + "|" + text[:180]
        if key in seen:
            seen[key]["count"] += 1
            return
        item = {"level": level, "file": fname, "line": line,
                "text": text[:900], "count": 1, "path": path}
        seen[key] = item
        items.append(item)

    for label, path in srcs:
        if not os.path.exists(path):
            continue
        try:
            if os.path.getsize(path) > 64 * 1024 * 1024:
                continue
        except OSError:
            continue
        buf, level, buf_line = [], None, 0
        try:
            with open(path, "r", errors="ignore") as f:
                for ln_no, raw in enumerate(f, 1):
                    if ln_no > 2000000:
                        break
                    s = raw.rstrip()
                    st = s.strip()
                    if not st:
                        if buf:
                            flush(buf, level, label, buf_line, path)
                        buf, level = [], None
                        continue
                    is_err = any(k in st for k in PROB_ERR)
                    is_warn = "***WARNING" in st
                    if is_err or is_warn:
                        if buf:
                            flush(buf, level, label, buf_line, path)
                        level = "error" if is_err else "warning"
                        buf = [st]
                        buf_line = ln_no
                        continue
                    # 缩进的续行（且不是另一条 *** 消息）；.log 里的
                    # "Abaqus Error: ... Please see the / status|message file for ..." 顶格写，单独认
                    if level and len(buf) < 8 and "***" not in st \
                            and (s[:1].isspace() or st.startswith(LOG_CONT)):
                        buf.append(st)
                        continue
                    if buf:
                        flush(buf, level, label, buf_line, path)
                    buf, level = [], None
                if buf:
                    flush(buf, level, label, buf_line, path)
        except OSError:
            continue

    # 错误排前面；但"被外部中断"这种告示不是根因，压到错误组末尾
    def rank(x):
        lvl = 0 if x["level"] == "error" else 1
        noise = 1 if any(k in x["text"] for k in PROB_NOISE) else 0
        return (lvl, noise, -x["count"])

    items.sort(key=rank)
    return {"ok": True, "counts": {"error": n_err, "warning": n_warn},
            "items": items[:limit], "total": len(items)}


def scan_problems_cached(job):
    key = []
    for ext in (".dat", ".msg", ".sta", ".log"):
        p = os.path.join(job["workdir"], job["name"] + ext)
        try:
            key.append(int(os.path.getmtime(p)))
        except OSError:
            key.append(0)
    try:
        key.append(int(os.path.getmtime(os.path.join(LOG_DIR, job["id"] + ".out"))))
    except OSError:
        key.append(0)
    key = tuple(key)
    hit = _prob_cache.get(job["id"])
    if hit and hit[0] == key:
        return hit[1]
    res = scan_problems(job)
    _prob_cache[job["id"]] = (key, res)
    return res


# --------------------------------------------------------------------------
# 求解器环境 / GPU 能力探测
# --------------------------------------------------------------------------
def clean_env():
    """复制一份环境变量，去掉会污染 abaqus 自带 Python 的那些。"""
    env = os.environ.copy()
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONIOENCODING",
              "PYTHONEXECUTABLE", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
              "VIRTUAL_ENV"):
        env.pop(k, None)
    return env


def _run_quiet(cmd, timeout=90, cwd=None):
    """跑一个会立刻退出的命令，拿 (退出码, 合并后的输出文本)。失败不抛。"""
    flags = CREATE_NO_WINDOW if IS_WIN else 0
    try:
        p = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str),
                           capture_output=True, timeout=timeout,
                           creationflags=flags, env=clean_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    out = (p.stdout or b"").decode("utf-8", "ignore")
    err = (p.stderr or b"").decode("utf-8", "ignore")
    return p.returncode, out + "\n" + err


def _abq_cmd(settings=None):
    abq = ((settings or {}).get("abaqus_cmd") or DEFAULT_ABAQUS).strip() or "abaqus"
    if " " in abq and not abq.startswith('"'):
        abq = '"%s"' % abq
    return abq


def gpu_hardware():
    """列出手上的 NVIDIA 卡；Abaqus 的 GPU 加速只认 NVIDIA。"""
    rc, txt = _run_quiet(["nvidia-smi",
                          "--query-gpu=name,memory.total,driver_version,compute_cap",
                          "--format=csv,noheader"], timeout=20)
    cards = []
    if rc == 0:
        for line in txt.splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 3 and parts[0] and "not found" not in parts[0].lower():
                cards.append({
                    "name": parts[0],
                    "mem": parts[1],
                    "driver": parts[2],
                    "cc": parts[3] if len(parts) > 3 else "",
                })
    return cards


def abaqus_release(abq):
    """从 `abaqus information=release` 里取版本号（2020 这种）。"""
    rc, txt = _run_quiet("%s information=release" % abq, timeout=120)
    m = re.search(r"Abaqus\s+(\d{4})", txt)
    if m:
        return m.group(1)
    m = re.search(r"Abaqus/\w+\s+(\d{4})", txt)
    return m.group(1) if m else ""


def probe_gpus_param(abq, scratch=None):
    """探一下这个启动器认不认 `gpus=`。

    故意给个不存在的输入文件：launcher 在解析参数阶段就会退出，不会真的启动求解器。
    - 报 "...must have a an integer value" -> 参数存在（gpu 是它的别名）
    - 报 "...may not be used with analysis" -> 这个版本不支持
    """
    d = scratch or tempfile.gettempdir()
    rc, txt = _run_quiet(
        '%s job=__abqrun_probe__ input=__abqrun_none__.inp gpus=x interactive' % abq,
        timeout=90, cwd=d)
    low = txt.lower()
    if "must have a an integer" in low or "must have an integer" in low:
        return True
    if "may not be used with" in low:
        return False
    return None


_GPU_FILE = os.path.join(STATE_DIR, "gpu.json")
_gpu_state = {"state": "idle", "data": None}
_gpu_lock = threading.Lock()


def gpu_snapshot():
    """返回探测结果（带缓存）；没探过就先起个后台线程。"""
    with _gpu_lock:
        if _gpu_state["data"] is None and os.path.exists(_GPU_FILE):
            try:
                with open(_GPU_FILE, "r", encoding="utf-8") as f:
                    _gpu_state["data"] = json.load(f)
                    _gpu_state["state"] = "done"
            except (OSError, ValueError):
                pass
        data = _gpu_state["data"]
        state = _gpu_state["state"]
    if data is None and state in ("idle", "failed"):
        start_gpu_detect()
        state = "detecting"
    return {"state": state, "data": data}


def start_gpu_detect(force=False):
    with _gpu_lock:
        if _gpu_state["state"] == "detecting":
            return
        if _gpu_state["data"] is not None and not force:
            return
        _gpu_state["state"] = "detecting"


    def work():
        res = detect_gpu()
        with _gpu_lock:
            _gpu_state["data"] = res
            _gpu_state["state"] = "done"
        try:
            ensure_dirs()
            atomic_write_json(_GPU_FILE, res)
        except OSError:
            pass

    threading.Thread(target=work, daemon=True).start()


def detect_gpu(settings=None):
    """一次性把「能不能开 GPU」查清楚，结论带理由，前端直接展示。"""
    st = settings or (MANAGER.settings if "MANAGER" in globals() else {})
    abq = _abq_cmd(st)
    cards = gpu_hardware()
    ver = abaqus_release(abq)
    param_ok = probe_gpus_param(abq)

    res = {
        "checked": time.time(),
        "abaqus_cmd": st.get("abaqus_cmd", ""),
        "version": ver,
        "cards": cards,
        "param_ok": param_ok,
        "ok": False,
        "verdict": "",
        "reason": "",
        "advice": "",
    }

    if param_ok is False:
        res["verdict"] = "no_param"
        res["reason"] = "这个 abaqus 启动器不接受 gpus= 参数（%s）" % (ver or "版本未知")
    elif not cards:
        res["verdict"] = "no_gpu"
        res["reason"] = "没有检测到 NVIDIA 显卡"
    else:
        res["verdict"] = "ok"
        res["ok"] = True
        card = cards[0]
        low = card["name"].lower()
        if "geforce" in low or "rtx" in low and "quadro" not in low:
            res["advice"] = ("消费级显卡 FP64 算力很低，Abaqus 的 GPU 加速主要吃双精度，"
                             "通常看不到明显加速；专业卡（Tesla/Quadro GV 系列）才有意义。")
        res["reason"] = "%s（%s，驱动 %s）" % (card["name"], card["mem"], card["driver"])
    res["note"] = ("GPU 只加速 Abaqus/Standard 的直接求解器；Abaqus/Explicit 用不上。"
                   "是否真的生效要看 .dat 里有没有 GPU 相关输出。")
    return res


def pid_alive(pid):
    """判断某个 PID 是否还在运行（用于重启后接管作业）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if IS_WIN:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                                 capture_output=True, timeout=10)
            raw = out.stdout
            return str(pid).encode() in raw
        os.kill(pid, 0)
        return True
    except Exception:
        return False


ERR_KEYS = ("***ERROR", "Abaqus Error", "exited with error", "HAS NOT BEEN COMPLETED")
OK_KEYS = ("COMPLETED SUCCESSFULLY", "COMPLETED", "completed successfully")


def classify_outcome(job, rc):
    """判定成功/失败。

    关键：abaqus.bat 即使分析失败也常常返回 0，所以不能只看退出码，
    必须读 stdout 捕获和 .log/.sta/.dat/.msg 里的收尾信息。
    """
    d, name = job["workdir"], job["name"]
    started = job.get("started")
    sources = [(os.path.join(LOG_DIR, job["id"] + ".out"), None)]
    for ext in (".log", ".sta", ".dat", ".msg"):
        sources.append((os.path.join(d, name + ext), started))

    errs = []
    ok = ""
    for path, st in sources:
        txt = file_text(path, st)
        if not txt:
            continue
        for ln in txt.splitlines():
            s = ln.strip()
            if not s:
                continue
            if any(k in s for k in ERR_KEYS):
                if len(errs) < 40:
                    errs.append(s[:260])
            elif any(k in s for k in OK_KEYS):
                ok = s[:260]

    if errs:
        # “被外部终止/SIGTERM”这类收尾信息不是根因，优先报真正的第一条错误
        for e in errs:
            if "external request" in e or "SIGTERM" in e:
                continue
            return "failed", e
        return "failed", errs[0]
    if ok:
        return "done", ok if len(ok) <= 60 else "正常结束"
    if rc not in (0, None):
        return "failed", "退出码 %s，请查看 .log / .dat" % rc
    return "failed", "没有找到完成标记，请查看 .log / .dat"


# --------------------------------------------------------------------------
# 调度器
# --------------------------------------------------------------------------
class Manager(object):
    def __init__(self):
        self.jobs = []
        self.settings = dict(DEFAULT_SETTINGS)
        self.procs = {}       # job_id -> Popen
        self.handles = {}     # job_id -> file object
        self.seq = 1
        self.cmd_switched = ""   # 旧设置里的 abaqus 路径失效时，记下它以便提示
        self.stop_event = threading.Event()
        self.load()

    # ---------------- 持久化 ----------------
    def load(self):
        ensure_dirs()
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        st = data.get("settings") or {}
        self.settings.update({k: st[k] for k in DEFAULT_SETTINGS if k in st})
        # 换机器、或挪了 Abaqus 安装目录时，旧设置里记的 abaqus 路径可能已经不存在。
        # 这时直接用本机自动探测到的那个，省得用户自己进设置里翻。
        old_cmd = (self.settings.get("abaqus_cmd") or "").strip()
        if (old_cmd and old_cmd != DEFAULT_ABAQUS
                and not abaqus_available(old_cmd) and abaqus_available(DEFAULT_ABAQUS)):
            self.settings["abaqus_cmd"] = DEFAULT_ABAQUS
            self.cmd_switched = old_cmd
        self.seq = int(data.get("seq", 1))
        for job in data.get("jobs", []):
            if job.get("status") == "running":
                # 上次退出时还在跑：尝试重新接管，否则标记中断
                pid = job.get("pid")
                if pid and pid_alive(pid):
                    job["orphan"] = True
                    job["note"] = "重新接管：求解进程仍在后台运行（PID %s）" % pid
                else:
                    job["status"] = "interrupted"
                    job["note"] = "程序关闭时作业仍在运行，进程已退出"
            job.setdefault("progress", {})
            self.jobs.append(job)
        if self.jobs:
            self.seq = max(self.seq, max(j.get("seq", 0) for j in self.jobs) + 1)
        # 老状态文件里没有 solver/nsteps，开个后台线程补扫（大 inp 也有几百毫秒，别挡启动）
        stale = [j for j in self.jobs if "solver" not in j]
        if stale:
            threading.Thread(target=self._backfill_meta, args=(stale,),
                             daemon=True).start()

    def _backfill_meta(self, jobs):
        for job in jobs:
            try:
                meta = scan_inp_meta(job.get("inp") or "")
            except Exception:
                continue
            with _lock:
                job["steps"] = meta["steps"]
                job["nsteps"] = meta["nsteps"]
                job["solver"] = meta["solver"]
                job["procs"] = meta["procs"]
        try:
            self.save()
        except Exception:
            pass

    def save(self):
        with _lock:
            atomic_write_json(STATE_FILE, {
                "settings": self.settings,
                "seq": self.seq,
                "jobs": self.jobs,
            })

    # ---------------- 查询 ----------------
    def snapshot(self):
        with _lock:
            jobs = []
            for j in self.jobs:
                item = dict(j)
                item.pop("steps", None)
                item["statusText"] = STATUS_TEXT.get(j["status"], j["status"])
                odb = os.path.join(j["workdir"], j["name"] + ".odb")
                item["odb"] = odb if os.path.exists(odb) else ""
                jobs.append(item)
            running = sum(1 for j in self.jobs if j["status"] == "running")
            queued = sum(1 for j in self.jobs if j["status"] == "queued")
            cpus = sum(j["cpus"] for j in self.jobs if j["status"] == "running")
            return {
                "settings": self.settings,
                "jobs": jobs,
                "stats": {"running": running, "queued": queued, "cpus": cpus,
                          "total": len(self.jobs)},
                "version": APP_VERSION,
            }

    # ---------------- 作业操作 ----------------
    def add_job(self, inp_path, cpus=None, memory=None, extra=None,
                precision=None, gpus=None):
        inp_path = os.path.abspath(inp_path)
        if not os.path.exists(inp_path):
            return {"ok": False, "error": "文件不存在：%s" % inp_path}
        if not inp_path.lower().endswith(".inp"):
            return {"ok": False, "error": "不是 .inp 文件：%s" % inp_path}
        workdir = os.path.dirname(inp_path)
        name = os.path.splitext(os.path.basename(inp_path))[0]
        with _lock:
            for j in self.jobs:
                if (j["status"] in ("queued", "running")
                        and j["workdir"].lower() == workdir.lower()
                        and j["name"].lower() == name.lower()):
                    return {"ok": False, "error": "同名作业已在队列中：%s" % name}
            sid = "j%d" % self.seq
            self.seq += 1
            meta = scan_inp_meta(inp_path)
            job = {
                "id": sid,
                "seq": int(sid[1:]),
                "name": name,
                "inp": inp_path,
                "workdir": workdir,
                "cpus": int(cpus or self.settings["default_cpus"]),
                "memory": memory or self.settings["default_memory"],
                "extra": (extra if extra is not None else self.settings["extra_args"]) or "",
                "precision": (precision if precision is not None
                              else self.settings.get("default_precision", "")) or "",
                "gpus": int(gpus if gpus is not None
                            else self.settings.get("default_gpus", 0) or 0),
                "status": "queued",
                "created": time.time(),
                "started": None,
                "ended": None,
                "elapsed": None,
                "returncode": None,
                "note": "",
                "waitReason": "",
                "progress": {},
                "steps": meta["steps"],
                "nsteps": meta["nsteps"],
                "solver": meta["solver"],
                "procs": meta["procs"],
            }
            self.jobs.append(job)
        self.save()
        return {"ok": True, "job": job["id"]}

    def _find(self, jid):
        for j in self.jobs:
            if j["id"] == jid:
                return j
        return None

    def action(self, jid, act):
        with _lock:
            job = self._find(jid)
            if not job:
                return {"ok": False, "error": "找不到作业"}
            if act == "up":
                idx = self.jobs.index(job)
                if idx > 0 and self.jobs[idx - 1]["status"] == "queued" and job["status"] == "queued":
                    self.jobs[idx - 1], self.jobs[idx] = self.jobs[idx], self.jobs[idx - 1]
            elif act == "down":
                idx = self.jobs.index(job)
                if idx + 1 < len(self.jobs) and self.jobs[idx + 1]["status"] == "queued" and job["status"] == "queued":
                    self.jobs[idx], self.jobs[idx + 1] = self.jobs[idx + 1], self.jobs[idx]
            elif act == "top":
                if job["status"] == "queued":
                    self.jobs.remove(job)
                    pos = next((i for i, j in enumerate(self.jobs) if j["status"] == "queued"), len(self.jobs))
                    self.jobs.insert(pos, job)
            elif act == "remove":
                if job["status"] == "running":
                    return {"ok": False, "error": "运行中的作业请先取消"}
                self.jobs.remove(job)
            elif act == "cancel":
                if job["status"] != "running":
                    return {"ok": False, "error": "作业未在运行"}
                self._kill(job)
                job["status"] = "canceled"
                job["ended"] = time.time()
                job["note"] = "用户取消"
            elif act == "retry":
                if job["status"] == "running":
                    return {"ok": False, "error": "作业正在运行"}
                job["status"] = "queued"
                job["started"] = None
                job["ended"] = None
                job["elapsed"] = None
                job["returncode"] = None
                job["note"] = ""
                job["waitReason"] = ""
                job["progress"] = {}
                meta = scan_inp_meta(job["inp"])
                job["steps"] = meta["steps"]
                job["nsteps"] = meta["nsteps"]
                job["solver"] = meta["solver"]
                job["procs"] = meta["procs"]
                job["created"] = time.time()
            elif act == "update":
                pass
            else:
                return {"ok": False, "error": "未知操作：%s" % act}
        self.save()
        return {"ok": True}

    def update_job(self, jid, patch):
        allowed = ("cpus", "memory", "extra", "precision", "gpus")
        with _lock:
            job = self._find(jid)
            if not job:
                return {"ok": False, "error": "找不到作业"}
            for k in allowed:
                if k in patch and patch[k] is not None:
                    if k == "cpus":
                        try:
                            job[k] = max(1, int(patch[k]))
                        except (TypeError, ValueError):
                            pass
                    elif k == "gpus":
                        try:
                            job[k] = max(0, int(patch[k]))
                        except (TypeError, ValueError):
                            job[k] = 0
                    elif k == "precision":
                        v = str(patch[k]).strip()
                        job[k] = v if v in PRECISION_CHOICES else ""
                    else:
                        job[k] = str(patch[k])
        self.save()
        return {"ok": True}

    def clear_finished(self):
        with _lock:
            self.jobs = [j for j in self.jobs
                         if j["status"] in ("queued", "running")]
        self.save()
        return {"ok": True}

    def update_settings(self, patch):
        recheck_gpu = False
        with _lock:
            for k, v in patch.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                if k in ("max_jobs", "max_cpus", "default_cpus", "default_gpus"):
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        continue
                    if k == "max_jobs":
                        v = max(1, v)
                    if k == "default_cpus":
                        v = max(1, v)
                    if k == "max_cpus" or k == "default_gpus":
                        v = max(0, v)
                elif k in ("paused", "clear_lck", "beep"):
                    v = bool(v)
                elif k == "default_precision":
                    v = str(v or "").strip()
                    if v not in PRECISION_CHOICES:
                        v = ""
                elif k == "odb_open_mode":
                    v = "viewer" if str(v) == "viewer" else "cae"
                else:
                    v = str(v)
                if k == "abaqus_cmd" and v != self.settings.get("abaqus_cmd"):
                    recheck_gpu = True   # 换了求解器，得重新探一遍
                self.settings[k] = v
        self.save()
        if recheck_gpu:
            start_gpu_detect(force=True)
        return {"ok": True}

    # ---------------- 进程 ----------------
    def _cmdline(self, job):
        abq = _abq_cmd(self.settings)
        parts = [
            "%s job=%s input=%s cpus=%d memory=\"%s\" ask_delete=OFF interactive"
            % (abq, job["name"], os.path.basename(job["inp"]),
               int(job["cpus"]), job["memory"])
        ]
        prec = (job.get("precision") or "").strip()
        if prec in PRECISION_CHOICES and prec:
            parts.append(prec)
        try:
            ngpu = int(job.get("gpus") or 0)
        except (TypeError, ValueError):
            ngpu = 0
        if ngpu > 0:
            parts.append("gpus=%d" % ngpu)
        extra = (job.get("extra") or "").strip()
        if extra:
            parts.append(extra)
        return " ".join(parts)

    def _start(self, job):
        workdir = job["workdir"]
        name = job["name"]
        lck = os.path.join(workdir, name + ".lck")
        if self.settings.get("clear_lck") and os.path.exists(lck):
            for _ in range(5):
                try:
                    os.remove(lck)
                    break
                except OSError:
                    time.sleep(0.2)
        try:
            os.makedirs(workdir, exist_ok=True)
        except OSError:
            pass

        cmd = self._cmdline(job)
        outpath = os.path.join(LOG_DIR, job["id"] + ".out")
        handle = open(outpath, "wb")
        flags = 0
        if IS_WIN:
            flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        env = clean_env()
        try:
            p = subprocess.Popen(
                cmd, cwd=workdir, shell=True,
                stdin=subprocess.DEVNULL,
                stdout=handle, stderr=subprocess.STDOUT,
                creationflags=flags, env=env, close_fds=True,
            )
        except OSError as exc:
            handle.close()
            job["status"] = "failed"
            job["note"] = "启动失败：%s" % exc
            job["ended"] = time.time()
            return False
        self.procs[job["id"]] = p
        self.handles[job["id"]] = handle
        job["pid"] = p.pid
        job["waitReason"] = ""
        job["status"] = "running"
        job["started"] = time.time()
        job["ended"] = None
        job["elapsed"] = None
        job["returncode"] = None
        job["note"] = ""
        job["progress"] = {}
        job["cmd"] = cmd
        return True

    def _kill(self, job):
        p = self.procs.get(job["id"])
        pid = p.pid if p else job.get("pid")
        if pid:
            try:
                if IS_WIN:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=20)
                else:
                    p.terminate()
            except Exception:
                pass
        if p:
            try:
                p.wait(timeout=5)
            except Exception:
                pass

    def _finalize(self, job, rc):
        job["returncode"] = rc
        job["ended"] = time.time()
        if job["started"]:
            job["elapsed"] = job["ended"] - job["started"]
        h = self.handles.pop(job["id"], None)
        if h:
            try:
                h.close()
            except OSError:
                pass
        self.procs.pop(job["id"], None)
        if job["status"] in ("canceled", "interrupted"):
            return
        status, note = classify_outcome(job, rc)
        job["status"] = status
        job["note"] = note
        job["waitReason"] = ""
        prog = compute_progress(job)
        job["progress"] = prog
        if status == "done":
            prog["pct"] = 100.0
            prog["eta"] = 0
        else:
            prog["eta"] = None
        # 清理可能残留的锁文件
        lck = os.path.join(job["workdir"], job["name"] + ".lck")
        if os.path.exists(lck):
            try:
                os.remove(lck)
            except OSError:
                pass

    # ---------------- 调度循环 ----------------
    def _dispatch(self):
        """把排队中的作业按顺序放到空闲槽位，返回是否启动了新作业。"""
        started = False
        if self.settings.get("paused"):
            for j in self.jobs:
                if j["status"] == "queued":
                    j["waitReason"] = "已暂停"
            return False
        max_jobs = int(self.settings.get("max_jobs") or 1)
        max_cpus = int(self.settings.get("max_cpus") or 0)
        while True:
            running = [j for j in self.jobs if j["status"] == "running"]
            queued = [j for j in self.jobs if j["status"] == "queued"]
            if not queued:
                return started
            # 严格按入队顺序：队头放不下就整体等待，等前面的作业结束后再判断
            nxt = queued[0]
            if len(running) >= max_jobs:
                for j in queued:
                    j["waitReason"] = "等槽位 %d/%d" % (len(running), max_jobs)
                return started
            used = sum(j["cpus"] for j in running)
            if max_cpus > 0 and running and used + nxt["cpus"] > max_cpus:
                free = max(0, max_cpus - used)
                nxt["waitReason"] = "缺 %d 核" % max(1, nxt["cpus"] - free)
                for j in queued[1:]:
                    j["waitReason"] = "等前面作业"
                return started
            job = self.jobs[self.jobs.index(nxt)]
            if not os.path.exists(job["inp"]):
                job["status"] = "failed"
                job["note"] = "找不到 inp：%s（文件被移动或删除，或换了机器）" % job["inp"]
                job["ended"] = time.time()
                started = True
                continue
            self._start(job)
            started = True

    def loop(self):
        last_save = 0.0
        while not self.stop_event.is_set():
            changed = False
            try:
                with _lock:
                    changed = self._dispatch()
                    self._poll_running()
            except Exception as exc:  # 调度线程不能死
                sys.stderr.write("[abqrun] 调度异常: %r\n" % (exc,))
            now = time.time()
            if changed or now - last_save > 10:
                try:
                    self.save()
                    last_save = now
                except Exception:
                    pass
            time.sleep(1.0)

    def _poll_running(self):
        done_any = False
        for job in [j for j in self.jobs if j["status"] == "running"]:
            p = self.procs.get(job["id"])
            if p is not None:
                rc = p.poll()
                if rc is not None:
                    self._finalize(job, rc)
                    done_any = True
                    continue
            elif job.get("orphan"):
                # 重启后接管的作业：靠 PID 存活 + 结果文件判断结束
                if not pid_alive(job.get("pid")):
                    job["orphan"] = False
                    self._finalize(job, None)
                    job["note"] = "重启后接管：" + (job.get("note") or "")
                    done_any = True
                    continue
            try:
                prog = compute_progress(job)
                pct = prog.get("pct")
                if pct and pct > 1.0 and job.get("started"):
                    prog["eta"] = (time.time() - job["started"]) * (100.0 - pct) / pct
                job["progress"] = prog
            except Exception:
                pass
        if done_any:
            self.save()
            if self.settings.get("beep") and winsound and \
                    not any(j["status"] in ("queued", "running") for j in self.jobs):
                try:
                    winsound.MessageBeep(0)
                except Exception:
                    pass

    def shutdown(self):
        self.stop_event.set()


MANAGER = Manager()


# --------------------------------------------------------------------------
# 文件浏览（给界面里的选择框用）
# --------------------------------------------------------------------------
def list_fs(path, exts=(".inp",)):
    if not path:
        path = os.path.expanduser("~")
    path = os.path.abspath(path)
    if os.path.isfile(path):
        path = os.path.dirname(path)
    exts = tuple(x.lower() for x in exts) if exts else ()
    dirs, files = [], []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir():
                    if entry.name.startswith("$") or entry.name.startswith("."):
                        continue
                    dirs.append({"name": entry.name, "path": entry.path})
                else:
                    low = entry.name.lower()
                    if not exts or low.endswith(exts):
                        files.append({
                            "name": entry.name,
                            "path": entry.path,
                            "size": entry.stat().st_size,
                            "mtime": entry.stat().st_mtime,
                        })
            except OSError:
                continue
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": path}
    parent = os.path.dirname(path.rstrip("\\/")) or path
    return {"ok": True, "path": path, "parent": parent, "dirs": dirs, "files": files}


def list_roots():
    roots = []
    if IS_WIN:
        import string
        for ch in string.ascii_uppercase:
            d = "%s:\\" % ch
            if os.path.exists(d):
                roots.append({"name": "%s:" % ch, "path": d})
    else:
        roots.append({"name": "/", "path": "/"})
    for label, p in (("桌面", os.path.join(os.path.expanduser("~"), "Desktop")),
                     ("文档", os.path.join(os.path.expanduser("~"), "Documents")),
                     ("用户目录", os.path.expanduser("~"))):
        if os.path.isdir(p):
            roots.append({"name": label, "path": p})
    return roots


def open_odb(path="", jid=""):
    """用 CAE（或 Viewer）打开一个 .odb —— 等同于 abq_cae_open.bat 的做法。"""
    if not path and jid:
        with _lock:
            job = next((j for j in MANAGER.jobs if j["id"] == jid), None)
        if job:
            path = os.path.join(job["workdir"], job["name"] + ".odb")
    if not path:
        return {"ok": False, "error": "没有指定 ODB 路径"}
    path = os.path.abspath(path)
    if not os.path.exists(path) and os.path.exists(path + ".odb"):
        path += ".odb"
    if not os.path.isdir(path) and not path.lower().endswith(".odb"):
        path += ".odb"
    if not os.path.exists(path):
        return {"ok": False, "error": "找不到文件：%s" % path}
    workdir = os.path.dirname(path)
    base = os.path.splitext(os.path.basename(path))[0]
    mode = MANAGER.settings.get("odb_open_mode") or "cae"
    if mode not in ("cae", "viewer"):
        mode = "cae"
    abq = _abq_cmd(MANAGER.settings)
    cmd = '%s %s database="%s"' % (abq, mode, base)
    flags = CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
    try:
        subprocess.Popen(cmd, cwd=workdir, shell=True,
                         stdin=subprocess.DEVNULL, creationflags=flags,
                         env=clean_env(), close_fds=True)
    except OSError as exc:
        return {"ok": False, "error": "启动失败：%s" % exc}
    return {"ok": True, "cmd": cmd, "cwd": workdir}


def tail_text(path, max_lines=300, max_bytes=262144):
    if not os.path.exists(path):
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "r", errors="ignore") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except OSError as exc:
        return "<读取失败: %s>" % exc


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ABQRun/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- helpers --
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- GET --
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = unquote(u.path)
        try:
            if path == "/api/state":
                self._json(MANAGER.snapshot())
            elif path == "/api/fs":
                ext = q.get("ext", [""])[0].strip()
                exts = (".inp",) if not ext else tuple(
                    x if x.startswith(".") else "." + x
                    for x in ext.split(",") if x.strip())
                self._json(list_fs(q.get("path", [""])[0], exts))
            elif path == "/api/gpu":
                if q.get("refresh", ["0"])[0] == "1":
                    start_gpu_detect(force=True)
                self._json(gpu_snapshot())
            elif path == "/api/roots":
                self._json({"roots": list_roots(), "home": os.path.expanduser("~")})
            elif path == "/api/log":
                self._log(q)
            elif path == "/api/problems":
                jid = q.get("id", [""])[0]
                with _lock:
                    job = next((j for j in MANAGER.jobs if j["id"] == jid), None)
                if not job:
                    self._json({"ok": False, "error": "找不到作业"})
                else:
                    self._json(scan_problems_cached(job))
            elif path in ("/", "/index.html"):
                self._static("index.html")
            else:
                self._static(path.lstrip("/"))
        except Exception as exc:
            self._json({"ok": False, "error": repr(exc)}, 500)

    def _log(self, q):
        jid = q.get("id", [""])[0]
        kind = q.get("kind", ["out"])[0]
        lines = int(q.get("lines", ["300"])[0] or 300)
        with _lock:
            job = next((j for j in MANAGER.jobs if j["id"] == jid), None)
        if not job:
            self._json({"ok": False, "error": "找不到作业"})
            return
        if kind == "out":
            p = os.path.join(LOG_DIR, jid + ".out")
        else:
            p = os.path.join(job["workdir"], job["name"] + "." + kind)
        self._json({"ok": True, "kind": kind, "text": tail_text(p, lines),
                    "path": p, "exists": os.path.exists(p)})

    def _static(self, rel):
        p = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not p.startswith(WEB_DIR) or not os.path.isfile(p):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(p)[1].lower()
        with open(p, "rb") as f:
            data = f.read()
        self._send(200, data, MIME.get(ext, "application/octet-stream"))

    # -- POST --
    def do_POST(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        body = self._body()
        try:
            if path == "/api/add":
                paths = body.get("paths") or []
                added, errors = [], []
                for inp in paths:
                    r = MANAGER.add_job(inp, body.get("cpus"), body.get("memory"),
                                        body.get("extra"), body.get("precision"),
                                        body.get("gpus"))
                    if r.get("ok"):
                        added.append(r["job"])
                    else:
                        errors.append(r.get("error"))
                self._json({"ok": True, "added": added, "errors": errors})
            elif path == "/api/action":
                self._json(MANAGER.action(body.get("id"), body.get("action")))
            elif path == "/api/update":
                self._json(MANAGER.update_job(body.get("id"), body))
            elif path == "/api/settings":
                self._json(MANAGER.update_settings(body))
            elif path == "/api/clear":
                self._json(MANAGER.clear_finished())
            elif path == "/api/open":
                target = body.get("path") or ""
                if body.get("id"):
                    with _lock:
                        job = next((j for j in MANAGER.jobs if j["id"] == body["id"]), None)
                    if job:
                        target = job["workdir"]
                if target and os.path.exists(target):
                    if IS_WIN:
                        os.startfile(target)
                    else:
                        subprocess.Popen(["xdg-open", target])
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "error": "路径不存在"})
            elif path == "/api/open-odb":
                self._json(open_odb(body.get("path") or "", body.get("id") or ""))
            elif path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception as exc:
            self._json({"ok": False, "error": repr(exc)}, 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description="ABQ-Run - Abaqus 作业排队界面")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    ensure_dirs()
    port = args.port
    httpd = None
    for attempt in range(20):
        try:
            httpd = Server((args.host, port), Handler)
            break
        except OSError:
            port += 1
    if httpd is None:
        print("端口被占用，启动失败")
        return 1

    t = threading.Thread(target=MANAGER.loop, daemon=True)
    t.start()
    # 启动时探一次 GPU（后台跑，不挡界面；约十秒）
    start_gpu_detect()

    url = "http://%s:%d/" % (args.host, port)
    print("=" * 60)
    print("  ABQ-Run  v%s   Abaqus 作业排队" % APP_VERSION)
    print("  地址: %s" % url)
    print("  命令: %s" % MANAGER.settings["abaqus_cmd"])
    if MANAGER.cmd_switched:
        print("  [i] 上次的 abaqus 路径用不了了（%s），已改用本机探测到的这个"
              % MANAGER.cmd_switched)
    if not abaqus_available(MANAGER.settings["abaqus_cmd"]):
        print("  [!] 这个 abaqus 现在用不了 —— 点界面上的「设置」选一下 abaqus.bat")
    print("  Python: %s%s" % (sys.version.split()[0], _runtime_tag()))
    print("  关闭此窗口即停止服务（运行中的作业会继续求解）")
    print("=" * 60)
    sys.stdout.flush()
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.shutdown()
        MANAGER.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
