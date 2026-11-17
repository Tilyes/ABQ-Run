# -*- coding: utf-8 -*-
"""假的 abaqus 求解器：只写 .sta / .log / .msg，用于测试排队界面。"""
import os
import sys
import time

args = {}
for a in sys.argv[1:]:
    if "=" in a:
        k, _, v = a.partition("=")
        args[k.lower()] = v.strip().strip('"')

job = args.get("job", "Job-1")
inp = args.get("input", job + ".inp")
quiet = os.environ.get("FAKE_QUIET") == "1"

print("Abaqus JOB %s" % job)
print("Command line:%s" % " ".join(sys.argv))
sys.stdout.flush()


def step_times(path):
    times = []
    pending = False
    try:
        with open(path, "r", errors="ignore") as f:
            for raw in f:
                s = raw.strip()
                if not s or s.startswith("**"):
                    continue
                if s.startswith("*"):
                    kw = s[1:].split(",")[0].strip().lower()
                    pending = kw in ("dynamic", "dynamic explicit", "static", "visco",
                                     "heat transfer", "coupled temperature-displacement")
                    continue
                if pending:
                    vals = []
                    for x in s.split(",")[:2]:
                        try:
                            vals.append(float(x))
                        except ValueError:
                            vals.append(None)
                    t = None
                    if len(vals) >= 2 and vals[1]:
                        t = vals[1]
                    elif vals and vals[0]:
                        t = vals[0]
                    if t:
                        times.append(t)
                    pending = False
    except OSError:
        pass
    return times or [1.0]


times = step_times(inp)
ninc = 6
bad = job.lower().startswith("bad")

with open(job + ".log", "w") as lg:
    lg.write("Abaqus JOB %s\n" % job)
    lg.write("Begin Analysis Input File Processor\n")
    lg.flush()

with open(job + ".sta", "w") as sta:
    sta.write("                                                    SUMMARY OF JOB INFORMATION\n")
    sta.write("  STEP      INC     ATT     ATT     SEV     EQU      TOTAL     TOTAL\n")
    sta.write("  NUMBER    NUM     NUM     ITER    CONV    ITER     STEP TIME INCREMENT\n")
    sta.flush()

t0 = time.time()
total = 0.0
for si, T in enumerate(times, start=1):
    for k in range(1, ninc + 1):
        time.sleep(0.35)
        st = T * k / ninc
        wall = time.time() - t0
        if bad and si == 1 and k >= 3:
            with open(job + ".msg", "w") as m:
                m.write(" ***ERROR: TOO MANY ATTEMPTS MADE FOR THIS INCREMENT\n")
            with open(job + ".log", "a") as lg:
                lg.write("Abaqus Error: Abaqus/Standard Analysis exited with an error\n")
            print(" ***ERROR: TOO MANY ATTEMPTS MADE FOR THIS INCREMENT")
            sys.exit(3)
        with open(job + ".sta", "a") as sta:
            # 与真实 Abaqus 一致：最后一列是“本步时间”，倒数第三列是累计时间
            sta.write("%5d%6d%4d%6d%6d%6d%7.2f%12.3e%12.3e\n"
                      % (si, k, 1, 0, 1, k, total + st, T / ninc, st))
        if not quiet:
            print("  STEP %d  INC %4d  STEP TIME %8.4f" % (si, k, st))
            sys.stdout.flush()
    total += T

with open(job + ".log", "a") as lg:
    lg.write("End Analysis Input File Processor\n")
    lg.write("Begin Abaqus/Standard Analysis\n")
    lg.write("End Abaqus/Standard Analysis\n")
    lg.write("Abaqus JOB %s COMPLETED\n" % job)

with open(job + ".dat", "w") as d:
    d.write("THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n")
print("Abaqus JOB %s COMPLETED" % job)
sys.exit(0)
