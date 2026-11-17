# -*- coding: utf-8 -*-
"""端到端测试：启动服务 -> 排队三个假作业 -> 取消 -> 失败判定 -> 收尾校验"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8791
BASE = "http://127.0.0.1:%d" % PORT
PY = sys.executable
ENV = dict(os.environ)
ENV["ABQRUN_STATE"] = os.path.join(HERE, "_state_test")   # 隔离，避免踩到正在用的队列
CASE1 = os.path.join(HERE, "case1")
CASE2 = os.path.join(HERE, "case2")

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


def state():
    return api("/api/state")


def wait_server():
    for _ in range(80):
        try:
            api("/api/state")
            return True
        except Exception:
            time.sleep(0.25)
    return False


def dump(st, tag):
    rows = ["    %-8s %-8s %6s %8s %s" % (j["name"], j["status"], j["cpus"],
                                          (j.get("progress") or {}).get("pct"),
                                          j.get("note", "")[:40]) for j in st["jobs"]]
    print("  -- %s (run=%d queued=%d)" % (tag, st["stats"]["running"], st["stats"]["queued"]))
    print("\n".join(rows))


def main():
    httpd = subprocess.Popen([PY, os.path.join(ROOT, "abqrun.py"),
                              "--no-browser", "--port", str(PORT)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=ENV)
    try:
        if not wait_server():
            print("服务没起来")
            print(httpd.stdout.read().decode(errors="ignore"))
            return 1

        # 干净起点
        for _ in range(3):
            st0 = state()
            for j in st0["jobs"]:
                if j["status"] == "running":
                    api("/api/action", {"id": j["id"], "action": "cancel"})
            api("/api/clear", {})
            for j in st0["jobs"]:
                api("/api/action", {"id": j["id"], "action": "remove"})
            time.sleep(0.5)
        check(len(state()["jobs"]) == 0, "起始队列为空（%d）" % len(state()["jobs"]))

        # 1) 静态页面可访问
        with urllib.request.urlopen(BASE + "/", timeout=5) as r:
            html = r.read().decode()
        check("ABQ" in html and "app.js" in html, "首页与 static 资源可访问")

        # 2) 设置成假求解器，串行
        api("/api/settings", {"abaqus_cmd": os.path.join(HERE, "fake_abq.bat"),
                              "max_jobs": 1, "max_cpus": 0, "paused": False,
                              "default_cpus": 2, "default_memory": "4 gb"})

        # 3) 加入三个作业
        r = api("/api/add", {"paths": [os.path.join(CASE1, "Job-A.inp"),
                                       os.path.join(CASE1, "Job-B.inp"),
                                       os.path.join(CASE2, "Job-C.inp")],
                             "cpus": 2, "memory": "4 gb"})
        print("    add resp:", r)
        check(len(r["added"]) == 3, "三个作业入队")
        dup = api("/api/add", {"paths": [os.path.join(CASE1, "Job-A.inp")]})
        check(len(dup["errors"]) == 1, "重复添加被拒绝")
        bad = api("/api/add", {"paths": [os.path.join(CASE2, "nope.inp")]})
        check(len(bad["errors"]) == 1, "不存在的文件被拒绝")

        st = state()
        dump(st, "入队后")
        time.sleep(1.4)
        st = state()
        dump(st, "1.4s 后")
        check(st["stats"]["running"] == 1, "并发=1 时只启动一个（实际 %d）"
              % st["stats"]["running"])
        check(st["stats"]["queued"] == 2, "其余两个处于排队")
        runjob = [j for j in st["jobs"] if j["status"] == "running"][0]
        check(runjob["name"] == "Job-A", "按入队顺序先跑 Job-A（实际 %s）" % runjob["name"])

        # 4) 进度推进 + 多步累计 + 剩余时间
        time.sleep(2.2)
        st = state()
        j = [x for x in st["jobs"] if x["name"] == "Job-A"][0]
        p = j.get("progress") or {}
        print("    Job-A progress:", {k: p.get(k) for k in ("step", "inc", "pct", "eta", "total")})
        check(p.get("step") in (1, 2), "解析出 STEP 号")
        check(p.get("pct") is not None and 0 < p["pct"] < 100,
              "百分比在 0~100 之间（实际 %s）" % p.get("pct"))
        check(p.get("nsteps") == 2, "识别出两个 step（实际 %s）" % p.get("nsteps"))
        check(p.get("frac") is not None and 0 <= p["frac"] <= 1,
              "当前步内比例正常（%s）" % p.get("frac"))
        check(p.get("eta") is not None, "能估算剩余时间")

        # 5) 取消正在跑的作业 -> 下一个顶上
        api("/api/action", {"id": j["id"], "action": "cancel"})
        time.sleep(1.6)
        st = state()
        jc = [x for x in st["jobs"] if x["name"] == "Job-A"][0]
        check(jc["status"] == "canceled", "运行中作业可取消")
        check(st["stats"]["running"] == 1 and
              [x for x in st["jobs"] if x["status"] == "running"][0]["name"] == "Job-B",
              "取消后自动顶上 Job-B")

        # 6) 排队作业上移
        api("/api/action", {"id": [x for x in st["jobs"] if x["name"] == "Job-C"][0]["id"],
                            "action": "up"})
        st = state()
        order = [x["name"] for x in st["jobs"] if x["status"] == "queued"]
        check(order and order[0] == "Job-C", "排队顺序可调整（%s）" % order)

        # 7) 等全部跑完
        for _ in range(60):
            st = state()
            if st["stats"]["running"] == 0 and st["stats"]["queued"] == 0:
                break
            time.sleep(0.7)
        dump(st, "全部结束")
        jb = [x for x in st["jobs"] if x["name"] == "Job-B"][0]
        jcc = [x for x in st["jobs"] if x["name"] == "Job-C"][0]
        check(jb["status"] == "done", "Job-B 正常完成")
        check(jcc["status"] == "done", "Job-C 完成后 pct=100（%s）"
              % (jcc.get("progress") or {}).get("pct"))
        check(jb["elapsed"] and jb["elapsed"] > 0, "记录了用时")
        check(not os.path.exists(os.path.join(CASE1, "Job-B.lck")), "结束时清掉 .lck")

        # 8) 失败判定
        api("/api/add", {"paths": [os.path.join(CASE2, "Bad-Job.inp")], "cpus": 1})
        for _ in range(40):
            st = state()
            bj = [x for x in st["jobs"] if x["name"] == "Bad-Job"]
            if bj and bj[0]["status"] in ("failed", "done"):
                break
            time.sleep(0.7)
        bj = [x for x in st["jobs"] if x["name"] == "Bad-Job"][0]
        check(bj["status"] == "failed", "非零退出判定为失败")
        check("ERROR" in (bj.get("note") or ""), "失败原因取自 .msg（%s）" % bj.get("note")[:50])

        # 9) 重跑
        api("/api/action", {"id": bj["id"], "action": "retry"})
        st = state()
        check([x for x in st["jobs"] if x["name"] == "Bad-Job"][0]["status"] in ("queued", "running"),
              "失败作业可重新排队")

        # 10) 日志接口
        log = api("/api/log?id=%s&kind=sta&lines=50" % jb["id"])
        check(log["ok"] and "SUMMARY OF JOB INFORMATION" in log["text"], "能读取 .sta 尾部")
        log2 = api("/api/log?id=%s&kind=out&lines=50" % jb["id"])
        check(log2["ok"] and log2["exists"], "能读取 stdout 捕获")

        # 11) 目录浏览接口
        fs = api("/api/fs?path=" + urllib.request.quote(CASE1))
        check(fs["ok"] and len(fs["files"]) >= 2, "文件浏览器列出 .inp")

    finally:
        try:
            api("/api/shutdown", {})
        except Exception:
            pass
        time.sleep(0.6)
        if httpd.poll() is None:
            httpd.terminate()
        out = httpd.stdout.read().decode(errors="ignore")
        if out.strip():
            print("--- server output ---")
            print(out[:2000])

    print("\n结果: %s" % ("全部通过" if not fails else "失败 %d 项" % len(fails)))
    for f in fails:
        print("  × " + f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
