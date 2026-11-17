# -*- coding: utf-8 -*-
"""用真实的 .sta 文本片段测解析，重点防止把“最危险元素”表当成进度行。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import abqrun

fails = []


def check(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        fails.append(msg)


def write_tmp(text):
    fd, p = tempfile.mkstemp(suffix=".sta")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return p


EXPLICIT = """Abaqus/Explicit 2020                            DATE 16-11-2026  TIME 11:21:34
-------------------------------------------------------------------------------
 STABLE TIME INCREMENT INFORMATION
-------------------------------------------------------------------------------

   Most critical elements:
    Element number   Rank    Time increment   Increment ratio
    (Instance name)
   ----------------------------------------------------------
         219          1       2.149508E-08      1.000000E+00
    HAMMER2-1
         653         10       2.286717E-08      9.399976E-01
    HAMMER2-1

-------------------------------------------------------------------------------
 SOLUTION PROGRESS
-------------------------------------------------------------------------------

 STEP 1  ORIGIN 0.0000

              STEP     TOTAL       CPU      STABLE    CRITICAL    KINETIC      TOTAL
INCREMENT     TIME      TIME      TIME   INCREMENT     ELEMENT     ENERGY     ENERGY
        0  0.000E+00 0.000E+00  00:00:00 1.935E-08         219  8.802E+01  8.802E+01
INSTANCE WITH CRITICAL ELEMENT: HAMMER2-1
ODB Field Frame Number      0 of    100 requested intervals at increment zero.
     1840  3.560E-05 3.560E-05  00:01:31 1.950E-08         219  8.702E+01  8.712E+01
     3680  7.120E-05 7.120E-05  00:03:02 1.955E-08         219  8.601E+01  8.622E+01
"""

STANDARD = """ Abaqus/Standard 2020                  DATE 15-11-2026 TIME 10:18:03
 SUMMARY OF JOB INFORMATION:
 STEP  INC ATT SEVERE EQUIL TOTAL  TOTAL      STEP       INC OF       DOF    IF
               DISCON ITERS ITERS  TIME/    TIME/LPF    TIME/LPF    MONITOR RIKS
               ITERS               FREQ
   1     1   1     0     0     0  0.00       1.00e-36   1.000e-36
   1     2   1     0     0     0  1.00       5.00e-01   5.000e-01
                           SEVERE DISCONTINUITY ITERATION
"""

p1 = write_tmp(EXPLICIT)
p2 = write_tmp(STANDARD)
try:
    print("--- Explicit 布局")
    check(abqrun._sta_layout(p1) == "explicit", "识别为 Explicit")
    rows = abqrun.read_sta_tail(p1)
    print("   解析行:", rows)
    check(len(rows) == 3, "只有 3 条真实进度行（不是 5 条，不含危险元素表）")
    check(rows[-1]["inc"] == 3680, "末行增量 = 3680（实际 %s）" % rows[-1]["inc"])
    check(abs(rows[-1]["t"] - 7.12e-05) < 1e-12, "末行时间 = 7.12e-05")
    check(all(r["step"] == 1 for r in rows), "步号都算作 STEP 1")

    job = {"id": "t1", "workdir": os.path.dirname(p1),
           "name": os.path.basename(p1)[:-4], "started": 0,
           "steps": [{"name": "Step-1", "kind": "time", "a": 0.05, "b": None, "n": None}]}
    # .sta 文件名需与 name 对应
    prog = abqrun.compute_progress(job)
    print("   进度:", {k: prog.get(k) for k in ("step", "inc", "pct", "frac")})
    check(prog.get("pct") is not None and 0 < prog["pct"] < 1,
          "百分比 = %.4f%%（7.12e-05 / 0.05）" % (prog.get("pct") or -1))

    print("--- Standard 布局")
    check(abqrun._sta_layout(p2) == "standard", "识别为 Standard")
    rows2 = abqrun.read_sta_tail(p2)
    print("   解析行:", rows2)
    check(len(rows2) == 2, "解析出 2 条数据行")
    check(rows2[-1]["inc"] == 2, "末行 INC = 2")
    check(abs(rows2[-1]["t"] - 0.5) < 1e-9, "末行步时间 = 0.5")
finally:
    for p in (p1, p2):
        try:
            os.remove(p)
        except OSError:
            pass

print("\n结果: %s" % ("全部通过" if not fails else "失败 %d 项" % len(fails)))
sys.exit(0 if not fails else 1)
