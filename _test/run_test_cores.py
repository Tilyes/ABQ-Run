# -*- coding: utf-8 -*-
"""容量规则测试：总核数上限 + 严格 FIFO + 前一个结束后重新判断。"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8795
BASE = "http://127.0.0.1:%d" % PORT
PY = sys.executable
ENV = dict(os.environ)
ENV["ABQRUN_STATE"] = os.path.join(HERE, "_state_test")   # 隔离，避免踩到正在用的队列
C1 = os.path.join(HERE, "case1")
C2 = os.path.join(HERE, "case2")

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


def rows():
    st = api("/api/state")
    return [(j["name"], j["status"], j["cpus"], j.get("waitReason")) for j in st["jobs"]]


srv = subprocess.Popen([PY, os.path.join(ROOT, "abqrun.py"), "--no-browser",
                        "--port", str(PORT)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.STDOUT, env=ENV)
for _ in range(80):
    try:
        api("/api/state")
        break
    except Exception:
        time.sleep(0.25)

# 清空
for j in api("/api/state")["jobs"]:
    api("/api/action", {"id": j["id"], "action": "cancel"})
    api("/api/action", {"id": j["id"], "action": "remove"})

api("/api/settings", {"abaqus_cmd": os.path.join(HERE, "fake_abq.bat"),
                      "max_jobs": 4, "max_cpus": 4, "paused": False})

# 两个 4 核作业 + 一个 2 核作业，上限 4 核 -> 只能跑一个 4 核
api("/api/add", {"paths": [os.path.join(C1, "Job-A.inp")], "cpus": 4})
api("/api/add", {"paths": [os.path.join(C2, "Job-C.inp")], "cpus": 2})
api("/api/add", {"paths": [os.path.join(C1, "Job-B.inp")], "cpus": 4})
time.sleep(1.6)
r = rows()
print("   队列:", r)
check(r[0][1] == "running", "A(4核) 先跑")
check(r[1][1] == "queued" and r[2][1] == "queued",
      "C(2核) 虽然能塞进空余 0 核之外… 实际应排队（严格 FIFO，A 占满 4 核）")
check("缺" in (r[1][3] or ""), "C 显示缺核原因（%s）" % r[1][3])

# A 结束后，C 应立刻顶上并占满
for _ in range(40):
    r = rows()
    if r[0][1] in ("done", "failed"):
        break
    time.sleep(0.8)
time.sleep(1.6)
r = rows()
print("   A 结束后:", r)
check(r[1][1] == "running", "前面的作业结束后，C 自动开始")
check(r[2][1] == "queued", "B(4核) 仍在排队")

for _ in range(40):
    r = rows()
    if all(x[1] in ("done", "failed") for x in r):
        break
    time.sleep(0.8)
print("   最终:", rows())
check(all(x[1] == "done" for x in rows()), "三个作业全部完成")

# 单作业请求超过上限时也不能永久卡住
api("/api/clear", {})
api("/api/add", {"paths": [os.path.join(C1, "Job-A.inp")], "cpus": 8})
time.sleep(1.6)
r = rows()
print("   超上限作业:", r)
check(r[0][1] == "running", "请求核数超过上限时仍能单独运行（不会永久卡死）")

try:
    api("/api/shutdown", {})
except Exception:
    pass
time.sleep(0.5)
if srv.poll() is None:
    srv.terminate()

print("\n结果: %s" % ("全部通过" if not fails else "失败 %d 项" % len(fails)))
for f in fails:
    print("  × " + f)
sys.exit(0 if not fails else 1)
