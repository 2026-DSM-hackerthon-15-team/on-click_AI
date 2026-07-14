"""
Process runner to start all FastAPI services in this repo in one container for PoC.
It launches uvicorn subprocesses for the api, ai-service, mcp-service, and stats-service.
"""
import subprocess
import sys
import time


SERVICES = [
    ("src.api.main:app", 8000),
    ("src.ai_service.main:app", 8001),
    ("src.mcp_service.main:app", 8002),
    ("src.stats_service.main:app", 8003),
]


def start():
    procs = []
    for module, port in SERVICES:
        cmd = [sys.executable, "-m", "uvicorn", module, "--host", "0.0.0.0", "--port", str(port)]
        print("Starting:", " ".join(cmd))
        p = subprocess.Popen(cmd)
        procs.append(p)
        time.sleep(0.5)

    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        print("Stopping services...")
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    start()
