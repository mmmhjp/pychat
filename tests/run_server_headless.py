# -*- coding: utf-8 -*-
"""以隐藏窗口方式运行真实服务器，供客户端集成测试连接。

用法（在仓库根目录执行）：
    python tests/run_server_headless.py

停止方式：在仓库根目录创建 _stop.flag 文件即可优雅退出。
"""
import importlib.util, os, sys, tkinter as tk

# 定位源码目录，兼容两种布局：
#   1) 仓库结构：本文件在 <repo>/tests/，源码在 <repo>/src/
#   2) 平铺结构：本文件与源码在同一目录
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_repo_src = os.path.join(os.path.dirname(TESTS_DIR), "src")
if os.path.isfile(os.path.join(_repo_src, "服务器.py")):
    BASE = os.path.dirname(TESTS_DIR)
    SRC_DIR = _repo_src
else:
    BASE = TESTS_DIR
    SRC_DIR = TESTS_DIR

os.chdir(BASE)
sys.path.insert(0, SRC_DIR)
spec = importlib.util.spec_from_file_location("py_server", os.path.join(SRC_DIR, "服务器.py"))
srv = importlib.util.module_from_spec(spec); sys.modules["py_server"] = srv
spec.loader.exec_module(srv)

root = tk.Tk(); root.withdraw()
server = srv.Server(root)
server._log = lambda m: print("[SRV]", m, flush=True)

STOP = os.path.join(BASE, "_stop.flag")
if os.path.exists(STOP): os.remove(STOP)


def watch():
    if os.path.exists(STOP):
        server.running = False
        try:
            server._file_workers.shutdown(wait=False, cancel_futures=True)
            with server.clients_lock:
                for i in list(server.clients.values()):
                    try: i["sock"].close()
                    except Exception: pass
                server.clients.clear()
            if server.sock: server.sock.close()
        except Exception: pass
        root.quit(); return
    root.after(300, watch)


root.after(300, watch)
root.after(150000, root.quit)
print("[SRV] ready", flush=True)
root.mainloop()
print("[SRV] exit", flush=True)