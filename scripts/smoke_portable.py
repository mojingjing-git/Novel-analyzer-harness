"""便携包真实用户操作闭环（S10）：启动打包产物 → 走 HTTP 入口 → 关掉。

为什么不用 TestClient：验收脚本跑的是进程内 app，跳过了 pythonw/onefile 打包后的真实启动路径
（端口选择、静态挂载、session token、单实例锁）——这些恰好只在打包后才出错。
"""
import json
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "novel-analyzer.exe"


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


def _port_from_log(log: Path):
    if not log.is_file():
        return None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "Uvicorn running on" in line:
            digits = "".join(ch for ch in line.rsplit(":", 1)[-1] if ch.isdigit())
            if digits:
                return int(digits)
    return None


def main() -> int:
    if not EXE.is_file():
        print("FAIL 没有打包产物")
        return 1
    log = EXE.parent / "analyzer.log"
    if log.is_file():
        log.unlink()

    proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    checks = []
    try:
        port = None
        for _ in range(40):
            time.sleep(0.5)
            port = _port_from_log(log)
            if port:
                break
        checks.append(("后端在日志中报出端口", port is not None, f"port={port}"))
        if not port:
            return _report(checks)
        base = f"http://127.0.0.1:{port}"

        try:
            status, body = _get(base + "/")
            text = body.decode("utf-8", "replace")
            checks.append(("首页返回 HTML", status == 200 and 'id="root"' in text, str(status)))
        except Exception as exc:
            checks.append(("首页返回 HTML", False, repr(exc)))

        try:
            status, body = _get(base + "/api/health")
            payload = json.loads(body.decode("utf-8"))
            ok = status == 200 and "status" in payload and payload.get("app") == "novel-analyzer-web"
            checks.append(("/api/health 返回有效服务状态", ok, str(status) + " " + str(payload.get("status"))))
        except Exception as exc:
            checks.append(("/api/health 返回有效服务状态", False, repr(exc)))

        try:
            status, body = _get(base + "/api/queue")
            payload = json.loads(body.decode("utf-8"))
            ok = status == 200 and isinstance(payload, dict) and "items" in payload and "queue" in payload
            checks.append(("/api/queue 返回合法 JSON 且含 items/queue", ok,
                           str(status) + " " + str(list(payload)[:4])))
        except Exception as exc:
            checks.append(("/api/queue 返回合法 JSON 且含 items/queue", False, repr(exc)))

        try:
            status, body = _get(base + "/api/books")
            payload = json.loads(body.decode("utf-8"))
            checks.append(("空工作区 /api/books 返回 0 本书（list 且不报错）",
                           status == 200 and isinstance(payload, list) and payload == [],
                           "books=" + repr(payload)))
        except Exception as exc:
            checks.append(("空工作区 /api/books 返回 0 本书（list 且不报错）", False, repr(exc)))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.run(["taskkill", "/F", "/IM", "novel-analyzer.exe"], capture_output=True)
    return _report(checks)


def _report(checks) -> int:
    ok = 0
    for name, passed, detail in checks:
        print(("PASS " if passed else "FAIL ") + name + "  [" + detail + "]")
        ok += 1 if passed else 0
    print("\nS10 便携包闭环：" + str(ok) + "/" + str(len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
