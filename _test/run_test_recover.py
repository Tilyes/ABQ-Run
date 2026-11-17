# -*- coding: utf-8 -*-
"""重启接管测试：作业运行中杀掉服务，再启动服务，应能重新接管并判定结束。"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8792
BASE = "http://127.0.0.1:%d" % PORT
PY = sys.executable
ENV = dict(os.environ)
ENV["ABQRUN_STATE"] = os.path.join(HERE, "_state_test")   # 隔离，避免踩到正在用的队列

fails = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        fails.append(msg)


def api(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def start():
    p = subprocess.Popen([PY, os.path.join(ROOT, "abqrun.py"), "--no-browser",
                          "--port", str(PORT)], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=ENV)
    for _ in range(60):
        try:
            api("/api/state")
            return p
        except Exception:
            time.sleep(0.25)
    return p


def main():
    # 清空状态（用隔离目录，绝不碰正在使用的队列）
    stdir = ENV["ABQRUN_STATE"]     # 只清测试自己的隔离目录
    for f in (os.path.join(stdir, "state.json"),):
        if os.path.exists(f):
            os.remove(f)

    srv = start()
    api("/api/settings", {"abaqus_cmd": os.path.join(HERE, "fake_abq.bat"),
                          "max_jobs": 1, "paused": False})
    inp = os.path.join(HERE, "case2", "Job-C.inp")
    api("/api/add", {"paths": [inp], "cpus": 1})
    time.sleep(1.5)
    st = api("/api/state")
    running = [j for j in st["jobs"] if j["status"] == "running"]
    check(len(running) == 1, "作业已开始运行")
    pid = running[0].get("pid") if running else None
    check(pid is not None, "记录了求解进程 PID（%s）" % pid)

    # 杀掉服务（模拟关闭窗口），求解进程应当继续
    subprocess.run(["taskkill", "/F", "/PID", str(srv.pid)], capture_output=True)
    time.sleep(0.8)

    srv2 = start()
    st = api("/api/state")
    j = [x for x in st["jobs"] if x["name"] == "Job-C"][0]
    print("    重启后状态: %s / %s" % (j["status"], j.get("note")))
    # 假作业很快，重启期间可能刚好跑完；关键是别误判成 interrupted
    check(j["status"] in ("running", "done"),
          "重启后没有误判中断（状态=%s）" % j["status"])
    check("接管" in (j.get("note") or ""), "标注为重新接管")

    for _ in range(40):
        st = api("/api/state")
        j = [x for x in st["jobs"] if x["name"] == "Job-C"][0]
        if j["status"] != "running":
            break
        time.sleep(0.6)
    print("    最终: %s / %s" % (j["status"], j.get("note")))
    check(j["status"] == "done", "求解进程结束后被判定为完成")
    check("重启后接管" in (j.get("note") or ""), "结论里保留了接管来源")

    try:
        api("/api/shutdown", {})
    except Exception:
        pass
    time.sleep(0.5)
    if srv2.poll() is None:
        srv2.terminate()

    print("\n结果: %s" % ("全部通过" if not fails else "失败 %d 项" % len(fails)))
    for f in fails:
        print("  × " + f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
