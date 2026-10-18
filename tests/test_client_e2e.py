# -*- coding: utf-8 -*-
"""客户端端到端：真实客户端 GUI 连真实服务器，验证本轮客户端改动。"""
import base64, hashlib, importlib.util, json, os, socket, struct, sys, threading, time, tkinter as tk

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

os.chdir(BASE); sys.path.insert(0, SRC_DIR)
failures = []


def check(label, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label + (("  " + extra) if extra else ""))
    if not cond: failures.append(label)


spec = importlib.util.spec_from_file_location("py_client", os.path.join(SRC_DIR, "客户端.py"))
cli = importlib.util.module_from_spec(spec); sys.modules["py_client"] = cli
spec.loader.exec_module(cli)

DLG = {"open": "", "save": "", "yesno": True, "errors": [], "warns": []}


class FakeBox:
    @staticmethod
    def showinfo(*a, **k): pass
    @staticmethod
    def showwarning(*a, **k): DLG["warns"].append(a[:2])
    @staticmethod
    def showerror(*a, **k): DLG["errors"].append(a[:2])
    @staticmethod
    def askyesno(*a, **k): return DLG["yesno"]
    @staticmethod
    def askokcancel(*a, **k): return DLG["yesno"]


class FakeDialog:
    @staticmethod
    def askopenfilename(**k): return DLG["open"]
    @staticmethod
    def asksaveasfilename(**k): return DLG["save"]


cli.messagebox = FakeBox
cli.filedialog = FakeDialog

PORT = 9999
_probe = socket.socket(); _probe.settimeout(1.0)
try:
    _probe.connect(("127.0.0.1", PORT))
    print("服务器在线，开始测试")
except OSError:
    print("服务器未启动，跳过")
    sys.exit(2)
finally:
    _probe.close()


class Peer:
    def __init__(self, n):
        self.n = n
        self.sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        self.sock.settimeout(5); self.inbox = []; self.lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _exact(self, n):
        b = b""
        while len(b) < n:
            try: c = self.sock.recv(n - len(b))
            except socket.timeout: return None
            if not c: return None
            b += c
        return b

    def _loop(self):
        while self.alive:
            try:
                h = self._exact(4)
                if not h: break
                b = self._exact(struct.unpack(">I", h)[0])
                if b is None: break
                with self.lock: self.inbox.append(json.loads(b.decode()))
            except Exception: break

    def send(self, m):
        raw = json.dumps(m, ensure_ascii=False).encode()
        self.sock.sendall(struct.pack(">I", len(raw)) + raw)

    def login(self):
        self.send({"type": "login", "username": self.n,
                   "device_id": hashlib.md5(self.n.encode()).hexdigest()})
        return self.wait("login_success", 5)

    def wait(self, t, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for m in self.inbox:
                    if m.get("type") == t: return m
            time.sleep(0.05)
        return None

    def wait_content(self, t, content, timeout=5):
        """按 (类型, 内容) 精确匹配，避免匹配到自己发出的那条"""
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for m in self.inbox:
                    if m.get("type") == t and m.get("content") == content:
                        return m
            time.sleep(0.05)
        return None

    def drain(self):
        with self.lock: self.inbox.clear()

    def close(self):
        self.alive = False
        try: self.sock.close()
        except Exception: pass


root = tk.Tk(); root.withdraw()
client = cli.Client(root)
STATE = {}
SRC = os.path.join(BASE, "_c_up.bin")
DST = os.path.join(BASE, "_c_dl.bin")
PAYLOAD = os.urandom(650 * 1024 + 777)
with open(SRC, "wb") as f: f.write(PAYLOAD)


def wait_until(fn, timeout=25, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if fn(): return True
        time.sleep(interval)
    return False


def body():
    print("\n[1] 登录")
    client.name_entry.delete(0, tk.END); client.name_entry.insert(0, "guic")
    client.ip_entry.delete(0, tk.END); client.ip_entry.insert(0, "127.0.0.1")
    client.login()
    check("已连接", client.connected is True)
    check("无错误弹窗", not DLG["errors"], str(DLG["errors"]))

    peer = Peer("peer"); STATE["peer"] = peer
    check("对端上线", peer.login() is not None)
    time.sleep(0.6)

    print("\n[2] 收发消息")
    peer.send({"type": "public_message", "content": "hello-gui"})
    check("显示对端消息", wait_until(lambda: "hello-gui" in client.chat_area.get("1.0", tk.END)))
    peer.drain()
    client.input_text.insert("1.0", "hi-peer")
    client.send_public()
    m = peer.wait_content("public_message", "hi-peer", 5)
    check("对端收到本端消息", m is not None and m.get("content") == "hi-peer",
          str(m))
    check("本端也收到自己的广播回显", wait_until(
        lambda: "hi-peer" in client.chat_area.get("1.0", tk.END)))

    print("\n[3] 分块上传 + 哈希校验")
    DLG["open"] = SRC; DLG["yesno"] = True
    client.upload_shared_file()
    name = os.path.basename(SRC)
    srv_file = os.path.join(BASE, "shared_files", name)
    check("上传完成（服务端已落盘）", wait_until(lambda: os.path.exists(srv_file), 30))
    if os.path.exists(srv_file):
        check("上传内容一致", hashlib.sha256(open(srv_file, "rb").read()).hexdigest()
              == hashlib.sha256(PAYLOAD).hexdigest())
    check("上传无错误弹窗", not DLG["errors"], str(DLG["errors"]))

    print("\n[4] 分块下载 + 哈希校验")
    client.request_shared_files()
    check("拿到共享文件列表", wait_until(lambda: any(
        f.get("name") == name for f in client.shared_files), 10))
    DLG["save"] = DST
    with client.shared_files_lock:
        client.shared_files = [f for f in client.shared_files if f.get("name") == name]
    client.open_shared_files_window()
    time.sleep(0.4)
    kids = client.file_tree.get_children()
    check("共享窗口列出文件", len(kids) >= 1)
    if kids:
        client.file_tree.selection_set(kids[0])
        client.download_selected_file()
        check("下载完成", wait_until(lambda: os.path.exists(DST), 30))
        if os.path.exists(DST):
            check("下载内容一致（哈希已通过校验）", hashlib.sha256(open(DST, "rb").read()).hexdigest()
                  == hashlib.sha256(PAYLOAD).hexdigest())
    check("下载无错误弹窗", not DLG["errors"], str(DLG["errors"]))

    print("\n[5] 断线静默重连")
    client.sock.close()
    check("检测到断线", wait_until(lambda: not client.connected, 12))
    check("自动重连成功", wait_until(lambda: client.connected, 30))
    check("重连期间无错误弹窗", not DLG["errors"], str(DLG["errors"]))

    print("\n[6] 私聊窗口与锁顺序")
    client.open_private_chat_by_name("peer")
    check("私聊窗口已创建", wait_until(lambda: "peer" in client.private_windows, 5))
    check("创建后无未读", client.private_unread.get("peer", 0) == 0)
    peer.drain()
    # 服务端的私聊是「发送方 → to」，所以 Peer 要发给客户端用户名 "guic"
    peer.send({"type": "private_message", "to": "guic", "content": "psst"})
    # 私聊消息应进入私聊窗口的 chat_box
    def priv_shown():
        w = client.private_windows.get("peer")
        if not w: return False
        try: return "psst" in w.chat_box.get("1.0", tk.END)
        except Exception: return False
    check("私聊消息已投递到私聊窗口", wait_until(priv_shown, 8))
    check("私聊历史已记录", wait_until(
        lambda: any(m.get("content") == "psst" for m in client.private_messages.get("peer", [])), 5))
    # 关闭窗口应无死锁
    check("关闭窗口无死锁", wait_until(
        lambda: (client.private_windows.pop("peer").close_win() or True), 5))


def run():
    try:
        body()
    except Exception as e:
        import traceback; traceback.print_exc(); failures.append(f"异常: {e}")
    finally:
        print("\n清理...")
        if STATE.get("peer"): STATE["peer"].close()
        for p in (SRC, DST, os.path.join(BASE, "shared_files", os.path.basename(SRC))):
            try:
                if os.path.exists(p): os.remove(p)
            except Exception: pass
        try:
            client.connected = False; client.username = None
            root.after(0, root.quit)
        except Exception: pass


threading.Thread(target=run, daemon=True).start()
root.mainloop()
print("\n" + "=" * 56)
if failures:
    print(f"结果: {len(failures)} 项失败")
    for f in failures: print("  - " + f)
    sys.exit(1)
print("结果: 全部通过")
sys.exit(0)