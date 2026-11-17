# 生成"绿色版"发布包：只含运行必需的四项，解压即用，无需安装 Python
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ITEMS = ["abqrun.py", "ABQ-Run.bat", "web", "runtime"]   # 少一样都跑不起来
SKIP_DIRS = {"__pycache__", ".git"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}


def collect(abs_dir):
    for dirpath, dirs, files in os.walk(abs_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES:
                continue
            yield os.path.join(dirpath, f)


def main():
    missing = [i for i in ITEMS if not os.path.exists(os.path.join(ROOT, i))]
    if missing:
        print("缺少必需项，无法打包：", ", ".join(missing))
        return 1

    outdir = os.path.join(ROOT, "_pkg")
    os.makedirs(outdir, exist_ok=True)
    name = "ABQ-Run-%s.zip" % time.strftime("%Y%m%d")
    out = os.path.join(outdir, name)

    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for item in ITEMS:
            src = os.path.join(ROOT, item)
            if os.path.isfile(src):
                z.write(src, "ABQ-Run/" + item)
                n += 1
            else:
                for full in collect(src):
                    rel = os.path.relpath(full, ROOT).replace("\\", "/")
                    z.write(full, "ABQ-Run/" + rel)
                    n += 1

    size = os.path.getsize(out) / 1048576.0
    print("已生成: %s" % out)
    print("文件数: %d    体积: %.1f MB" % (n, size))
    print("目标机器上解压后，双击 ABQ-Run\\ABQ-Run.bat 即可（不需要装 Python）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
