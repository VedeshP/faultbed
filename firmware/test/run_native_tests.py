"""Compile and run the firmware core unit tests on the host with g++ (no board needed)."""
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "lib" / "fbcore"


def main() -> int:
    cxx = shutil.which("g++") or shutil.which("clang++")
    if not cxx:
        print("g++/clang++ not found on PATH")
        return 2
    out = pathlib.Path(tempfile.gettempdir()) / ("fbcore_tests" + (".exe" if sys.platform == "win32" else ""))
    sources = [str(ROOT / "test" / "native" / "test_core.cpp")] + [str(p) for p in sorted(CORE.glob("*.cpp"))]
    cmd = [cxx, "-std=c++17", "-O2", "-Wall", "-Wextra", "-I", str(CORE), *sources, "-o", str(out)]
    subprocess.run(cmd, check=True)
    return subprocess.run([str(out)]).returncode


if __name__ == "__main__":
    sys.exit(main())
