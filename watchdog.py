"""Hermes Sentinel 看门狗（最稳运行方案）

- 无窗口常驻：由 启动.bat 用 pythonw 拉起，关闭启动窗口不影响本进程。
- 崩溃自启：server.py 异常退出（returncode != 0）后 5 秒自动重启。
- 重启限频：10 分钟内异常重启超过 5 次即改为 60 秒退避，避免「启动即崩」时
  疯狂重启把 CPU 和日志刷爆（端口被占、依赖缺失属于这类硬故障，快重启无用）。
- 优雅停止：运行 停止.bat 写入 hermes.stop 标记，看门狗会终止子进程并退出。
- 日志落盘：server.py 的全部输出追加到 hermes.log，超过 10MB 自动轮转（保留 3 份）。
- 解释器：使用启动本看门狗的解释器（sys.executable），无需硬编码路径。
"""

import os
import sys
import time
import pathlib
import subprocess

HERE = pathlib.Path(__file__).parent
PY = sys.executable                           # 启动看门狗的解释器（pythonw），同源运行 server.py
SRV = str(HERE / "server.py")
LOG = str(HERE / "hermes.log")
STOP = HERE / "hermes.stop"

# 强制子进程 UTF-8 模式：Caspian SDK 在 Windows 默认 GBK 下读配置会
# 'gbk' codec can't decode 报错导致邮件通道静默掉线（2026-08-21 踩坑）
ENV = {**os.environ, "PYTHONUTF8": "1"}

# 日志轮转：单文件上限与保留份数（hermes.log.1 / .2 / .3）
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 3

# 异常重启限频：滑动窗口内超过阈值则拉长退避
RESTART_WINDOW = 600     # 秒
RESTART_MAX = 5          # 窗口内允许的异常重启次数
RESTART_BACKOFF = 60     # 超限后的退避间隔（秒）


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


def _rotate_log() -> None:
    """hermes.log 超过上限则轮转，保留最近 LOG_BACKUPS 份。

    只能在**子进程已退出**时调用：server.py 运行期间持有该文件句柄，
    Windows 下重命名被占用的文件会直接失败。故调用点放在主循环开头。
    轮转失败一律吞掉——日志维护绝不能挡住服务启动。
    """
    try:
        if not os.path.exists(LOG) or os.path.getsize(LOG) < LOG_MAX_BYTES:
            return
        oldest = f"{LOG}.{LOG_BACKUPS}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(LOG_BACKUPS - 1, 0, -1):
            src, dst = f"{LOG}.{i}", f"{LOG}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(LOG, f"{LOG}.1")
        log(f"日志已轮转（上限 {LOG_MAX_BYTES // 1024 // 1024}MB，保留 {LOG_BACKUPS} 份）")
    except Exception:
        pass


def _sleep_watch(seconds: int) -> bool:
    """可中断等待：退避期间仍每秒检查停止标记。返回 True 表示收到停止请求。"""
    for _ in range(max(1, int(seconds))):
        if STOP.exists():
            return True
        time.sleep(1)
    return False


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
    restarts: list[float] = []      # 最近若干次「异常重启」的时间戳（滑动窗口）
    while True:
        if STOP.exists():
            log("检测到停止标记，退出")
            _clear_stop()
            break

        _rotate_log()   # 此刻无子进程持有句柄，是唯一安全的轮转时机
        log("启动 Hermes Sentinel (server.py) ...")
        try:
            # 用 with 管理日志文件句柄，避免每轮重开不关导致 fd 泄漏
            with open(LOG, "a", encoding="utf-8") as logfile:
                p = subprocess.Popen([PY, SRV], stdout=logfile, stderr=subprocess.STDOUT, env=ENV)
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

        # 异常退出：按滑动窗口限频，防止「启动即崩」变成秒级重启风暴
        now = time.time()
        restarts = [t for t in restarts if now - t < RESTART_WINDOW]
        restarts.append(now)
        if len(restarts) > RESTART_MAX:
            log(f"{RESTART_WINDOW // 60} 分钟内已异常重启 {len(restarts)} 次，"
                f"疑似硬故障（端口占用 / 依赖缺失 / 配置错误），退避 {RESTART_BACKOFF} 秒后再试；"
                f"请查看上方 server.py 报错")
            if _sleep_watch(RESTART_BACKOFF):
                log("退避期间收到停止标记，退出")
                _clear_stop()
                break
        else:
            log("异常退出，5 秒后自动重启 ...")
            if _sleep_watch(5):
                log("等待期间收到停止标记，退出")
                _clear_stop()
                break


if __name__ == "__main__":
    main()
