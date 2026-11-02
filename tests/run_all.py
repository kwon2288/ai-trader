"""모든 테스트 실행:  python tests/run_all.py   (pytest 없이도 돌아감, 파일마다 따로 실행)"""
import subprocess
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
files = sorted(here.glob("test_*.py"))
failed = []
t0 = time.time()
for f in files:
    r = subprocess.run([sys.executable, str(f)], cwd=here.parent, capture_output=True, text=True)
    n = r.stdout.count("PASS ")
    print(f"{'✅' if r.returncode == 0 else '❌'} {f.name:28s} {n}개")
    if r.returncode:
        failed.append(f.name)
        print("\n".join(("   " + x) for x in (r.stdout + r.stderr).strip().splitlines()[-15:]))
print(f"\n{len(files) - len(failed)}/{len(files)} 파일 통과 ({time.time() - t0:.0f}초)")
sys.exit(1 if failed else 0)
