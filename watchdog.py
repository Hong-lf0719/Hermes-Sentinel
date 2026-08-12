"""Hermes Sentinel 看门狗（最稳运行方案）

- 无窗口常驻：由 启动.bat 用 pythonw 拉起，关闭启动窗口不影响本进程。
- 崩溃自启：server.py 异常退出（returncode != 0）后 5 秒自动重启。
- 优雅停止：运行 停止.bat 写入 hermes.stop 标记，看门狗会终止子进程并退出。
- 日志落盘：server.py 的全部输出追加到 hermes.log，便于排查。
- 解释器：使用启动本看门狗的解释器（sys.executable），无需硬编码路径。
"""

import sys
import time
import pathlib
import subprocess

HERE = pathlib.Path(__file__).parent
PY = sys.executable                           # 启动看门狗的解释器（pythonw），同源运行 server.py
SRV = str(HERE / "server.py")
LOG = str(HERE / "hermes.log")
STOP = HERE / "hermes.stop"


def log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _clear_stop() -> None:
    try:
        STOP.unlink()
    except OSError:
        pass


def _stop_child(p: subprocess.Popen) -> None:
    try:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    except Exception:
        pass


def main() -> None:
    _clear_stop()
    log("看门狗启动")
    while True:
        if STOP.exists():
            log("检测到停止标记，退出")
            _clear_stop()
            break

        log("启动 Hermes Sentinel (server.py) ...")
        try:
            # 用 with 管理日志文件句柄，避免每轮重开不关导致 fd 泄漏
            with open(LOG, "a", encoding="utf-8") as logfile:
                p = subprocess.Popen([PY, SRV], stdout=logfile, stderr=subprocess.STDOUT)
        except Exception as e:
            log(f"启动失败: {e}")
            time.sleep(5)
            continue

        # 轮询：子进程退出 或 收到停止标记 即处理
        # （替代原 p.wait() 阻塞写法 —— 旧写法在 server 运行期间不会检查 STOP，导致无法停止）
        try:
            while True:
                if p.poll() is not None:
                    break
                if STOP.exists():
                    log("收到停止标记，终止子进程")
                    _stop_child(p)
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            _stop_child(p)

        code = p.returncode
        log(f"Hermes 退出，returncode={code}")

        if STOP.exists():
            _clear_stop()
            break
        if code == 0:
            log("正常退出，不重启")
            break
        log("异常退出，5 秒后自动重启 ...")
        time.sleep(5)


if __name__ == "__main__":
    main()
