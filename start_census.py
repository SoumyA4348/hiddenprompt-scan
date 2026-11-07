"""Start `census.py run ...` as a fully detached background process (survives closing the terminal).

    python start_census.py 2301-2610 [more census.py run args]

Writes the log to data/logs/run-<time>.log and the PID to data/census.pid.
"""
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
logs = ROOT / "data" / "logs"
logs.mkdir(parents=True, exist_ok=True)
stamp = time.strftime("%Y%m%d-%H%M")
python, flags = sys.executable, 0
if sys.platform == "win32":
    # pythonw has no console, so neither the census nor its worker processes open terminal windows
    python = str(pathlib.Path(sys.executable).with_name("pythonw.exe"))
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
with open(logs / f"run-{stamp}.log", "ab") as out, open(logs / f"run-{stamp}.err", "ab") as err:
    try:
        p = subprocess.Popen([python, "-u", "census.py", "run", *sys.argv[1:]], cwd=ROOT,
                             stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=flags,
                             start_new_session=sys.platform != "win32")
    except OSError:   # the parent job forbids breakaway: start without it
        flags &= ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        p = subprocess.Popen([python, "-u", "census.py", "run", *sys.argv[1:]], cwd=ROOT,
                             stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=flags)
(ROOT / "data" / "census.pid").write_text(str(p.pid))
print(f"started PID {p.pid}, log data/logs/run-{stamp}.log")
