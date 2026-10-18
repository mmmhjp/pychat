# -*- coding: utf-8 -*-
"""以隐藏窗口方式运行真实服务器，供客户端集成测试连接。"""
import importlib.util, os, sys, tkinter as tk

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
spec = importlib.util.spec_from_file_location("py_server", os.path.join(BASE, "服务器.py"))
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