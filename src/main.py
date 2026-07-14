"""
Process runner to start all FastAPI services in this repo in one container for PoC.
It launches uvicorn subprocesses for the api, ai-service, mcp-service, and stats-service.
"""
import logging
import subprocess
import sys
import time

from src.observability import configure_logging, log_event


SERVICES = [
    ("src.api.main:app", 8000),
    ("src.ai_service.main:app", 8001),
    ("src.mcp_service.main:app", 8002),
    ("src.stats_service.main:app", 8003),
]
logger = logging.getLogger("on_click.runner")


def start():
    configure_logging("service-runner")
    procs = []
    for module, port in SERVICES:
        cmd = [sys.executable, "-m", "uvicorn", module, "--host", "0.0.0.0", "--port", str(port)]
        p = subprocess.Popen(cmd)
        procs.append(p)
        log_event(
            logger,
            logging.INFO,
            "service.process.started",
            serviceModule=module,
            port=port,
            pid=p.pid,
        )
        time.sleep(0.5)

    try:
        while True:
            for (module, port), process in zip(SERVICES, procs):
                exit_code = process.poll()
                if exit_code is not None:
                    log_event(
                        logger,
                        logging.CRITICAL,
                        "service.process.exited",
                        serviceModule=module,
                        port=port,
                        pid=process.pid,
                        exitCode=exit_code,
                    )
                    raise SystemExit(exit_code or 1)
            time.sleep(1)
    except KeyboardInterrupt:
        log_event(logger, logging.INFO, "service.runner.stopping")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    start()
