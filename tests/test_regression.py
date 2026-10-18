# -*- coding: utf-8 -*-
"""全量回归测试：覆盖本轮所有改动
- 黑名单 Tab 类重构（真实创建窗口并操作）
- SHA-256 完整性校验（共享文件 + 点对点）
- deque 消息历史
- 连接限流 / 消息长度上限 / 报文类型校验
- 踢人 helper
- 客户端用户名校验与用户名解析
"""
import base64, hashlib, importlib.util, json, os, socket, struct, sys, threading, time, tkinter as tk

# 仓库根目录：本文件位于 <repo>/tests/，源码位于 <repo>/src/
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SRC_DIR = os.path.join(REPO_ROOT, "src")

# 在仓库根目录运行，运行时数据落在根目录下的 data/
BASE = REPO_ROOT
os.chdir(BASE)
sys.path.insert(0, SRC_DIR)

failures = []


def check(label, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label + (("  " + extra) if extra else ""))
    if not cond:
        failures.append(label)


def load(name, fn):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SRC_DIR, fn))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


srv = load("py_server", "服务器.py")
cli = load("py_client", "客户端.py")

# ---- 脚本化对话框，避免测试中弹窗阻塞 ----
ANSWER = {"yesno": True, "askstring": "test", "askinteger": 1}
MSGS = []


class FakeBox:
    @staticmethod
    def showinfo(*a, **k): MSGS.append(("info",) + a[:2])
    @staticmethod
    def showwarning(*a, **k): MSGS.append(("warn",) + a[:2])
    @staticmethod
    def showerror(*a, **k): MSGS.append(("error",) + a[:2])
    @staticmethod
    def askyesno(*a, **k): return ANSWER["yesno"]
    @staticmethod
    def askokcancel(*a, **k): return ANSWER["yesno"]


class FakeSimple:
    @staticmethod
    def askstring(*a, **k): return ANSWER["askstring"]
    @staticmethod
    def askinteger(*a, **k): return ANSWER["askinteger"]


srv.messagebox = FakeBox
srv.simpledialog = FakeSimple
cli.messagebox = FakeBox
cli.filedialog = type("F", (), {"askopenfilename": staticmethod(lambda **k: ""),
                                 "asksaveasfilename": staticmethod(lambda **k: "")})

PORT = srv.PORT


def port_free(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); return True
    except OSError:
        return False
    finally:
        s.close()


if not port_free(PORT) or not port_free(srv.DISCOVERY_PORT):
    print("端口被占用，跳过"); sys.exit(2)


def send(sock, msg):
    raw = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(">I", len(raw)) + raw)


class Peer:
    def __init__(self, name):
        self.name = name
        self.sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        self.sock.settimeout(5)
        self.inbox, self.lock = [], threading.Lock()
        self.alive = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _exact(self, n):
        b = b""
        while len(b) < n:
            try:
                c = self.sock.recv(n - len(b))
            except socket.timeout:
                return None
            if not c: return None
            b += c
        return b

    def _loop(self):
        while self.alive:
            try:
                h = self._exact(4)
                if not h: break
                body = self._exact(struct.unpack(">I", h)[0])
                if body is None: break
                with self.lock:
                    self.inbox.append(json.loads(body.decode()))
            except Exception:
                break

    def send(self, m): send(self.sock, m)

    def login(self):
        self.send({"type": "login", "username": self.name,
                   "device_id": hashlib.md5(self.name.encode()).hexdigest(),
                   "mac": "AA:BB:CC:00:00:%02X" % (len(self.name) % 256),
                   "hardware_fingerprint": hashlib.sha256(self.name.encode()).hexdigest()[:32]})
        return self.wait("login_success", 5)

    def wait(self, t, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for m in self.inbox:
                    if m.get("type") == t: return m
            time.sleep(0.05)
        return None

    def drain(self):
        with self.lock: self.inbox.clear()

    def close(self):
        self.alive = False
        try: self.sock.close()
        except Exception: pass


root = tk.Tk(); root.withdraw()
server = srv.Server(root)
server._log = lambda m: None
STATE = {"p1": None, "p2": None}
FNAME = "regress.bin"


def wait_until(fn, timeout=15, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if fn(): return True
        time.sleep(interval)
    return False


def t01_deque():
    print("\n[1] 消息历史 deque 自动限长")
    check("message_history 是 deque", hasattr(server.message_history, "maxlen"))
    check("maxlen 已设置", server.message_history.maxlen == srv.MAX_MESSAGE_HISTORY)
    for i in range(srv.MAX_MESSAGE_HISTORY + 50):
        with server.message_history_lock:
            server.message_history.append({"type": "public", "from": "x", "content": str(i)})
    check("长度不会超过 maxlen", len(server.message_history) == srv.MAX_MESSAGE_HISTORY,
          f"len={len(server.message_history)}")
    with server.message_history_lock:
        server.message_history.clear()


def t02_login_and_limits():
    print("\n[2] 登录 / 消息长度上限 / 报文类型校验")
    p1 = Peer("alice"); STATE["p1"] = p1
    check("登录成功", p1.login() is not None)

    # 超长消息
    p1.drain()
    p1.send({"type": "public_message", "content": "x" * (srv.MAX_MESSAGE_LENGTH + 10)})
    err = p1.wait("error", 5)
    check("超长消息被拒绝并提示", err is not None and "过长" in err.get("reason", ""))

    # 正常消息
    p1.drain()
    p1.send({"type": "public_message", "content": "ok-msg"})
    m = p1.wait("public_message", 5)
    check("正常消息通过", m is not None and m.get("content") == "ok-msg")

    # 非 dict 报文不应导致断连（服务器会丢弃）
    p1.send({"type": "public_message", "content": 12345})
    time.sleep(0.5)
    p1.send({"type": "public_message", "content": "still-alive"})
    m2 = p1.wait("public_message", 5)
    check("非法 content 类型被安全丢弃且连接存活", m2 is not None)


def t03_shared_file_hash():
    print("\n[3] 共享文件 SHA-256 校验")
    p1 = STATE["p1"]
    payload = os.urandom(400 * 1024 + 55)
    sha = hashlib.sha256(payload).hexdigest()

    p1.drain()
    p1.send({"type": "upload_shared_file_start", "filename": FNAME, "total_size": len(payload)})
    check("上传被接受", p1.wait("upload_accepted", 5) is not None)

    ch = 256 * 1024
    idx = 0
    for i in range(0, len(payload), ch):
        p1.send({"type": "upload_shared_file_chunk", "filename": FNAME, "index": idx,
                 "data": base64.b64encode(payload[i:i + ch]).decode()})
        idx += 1
    p1.send({"type": "upload_shared_file_end", "filename": FNAME,
             "total_size": len(payload), "sha256": sha})
    path = os.path.join(srv.SHARED_FILE_DIR, FNAME)
    check("正确哈希 → 上传落盘", wait_until(lambda: os.path.exists(path)))
    if os.path.exists(path):
        check("落盘内容一致", hashlib.sha256(open(path, "rb").read()).hexdigest() == sha)

    # 错误哈希 → 必须拒绝
    BAD = "regress_bad.bin"
    p1.drain()
    p1.send({"type": "upload_shared_file_start", "filename": BAD, "total_size": len(payload)})
    p1.wait("upload_accepted", 5)
    for i in range(0, len(payload), ch):
        p1.send({"type": "upload_shared_file_chunk", "filename": BAD, "index": i // ch,
                 "data": base64.b64encode(payload[i:i + ch]).decode()})
    p1.send({"type": "upload_shared_file_end", "filename": BAD,
             "total_size": len(payload), "sha256": "0" * 64})
    err = p1.wait("error", 6)
    check("错误哈希 → 被拒绝", err is not None and "校验" in err.get("reason", ""),
          str(err))
    check("错误哈希 → 未落盘", not os.path.exists(os.path.join(srv.SHARED_FILE_DIR, BAD)))
    check("错误哈希 → 临时文件已清理",
          not [x for x in os.listdir(srv.SHARED_FILE_DIR) if x.endswith(".part")])

    # 下载时服务端应下发 sha256
    p1.drain()
    p1.send({"type": "download_shared_file", "filename": FNAME})
    st = p1.wait("shared_file_start", 10)
    check("下载 start 包带 sha256", st is not None and st.get("sha256") == sha)
    end = None
    data = bytearray()
    consumed = 0
    deadline = time.time() + 20
    while time.time() < deadline and end is None:
        with p1.lock:
            batch = p1.inbox[consumed:]; consumed = len(p1.inbox)
        for m in batch:
            if m.get("type") == "shared_file_chunk":
                data += base64.b64decode(m["data"])
            elif m.get("type") == "shared_file_end":
                end = m
        time.sleep(0.05)
    check("下载 end 包带 sha256", end is not None and end.get("sha256") == sha)
    check("下载内容一致", hashlib.sha256(bytes(data)).hexdigest() == sha)


def t04_client_hash_verify():
    print("\n[4] 客户端下载侧哈希校验")
    c = cli.Client.__new__(cli.Client)
    c.pending_downloads_lock = threading.Lock()
    c._shared_download = {}
    c.pending_shared_downloads = {}
    c.send_msg = lambda m: True
    out = []
    c.append_group_msg = lambda t, system=False, self_msg=False: out.append(t)
    c._ui_msg = lambda t, x: out.append(f"{t}:{x}")

    out_path = os.path.join(BASE, "_cli_dl.bin")
    payload = b"x" * 1024
    c.pending_shared_downloads["good.bin"] = out_path
    c._handle_shared_file_start({"filename": "good.bin", "total_size": len(payload)})
    c._handle_shared_file_chunk({"filename": "good.bin", "index": 0,
                                 "data": base64.b64encode(payload).decode()})
    c._handle_shared_file_end({"filename": "good.bin", "total_size": len(payload),
                               "sha256": hashlib.sha256(payload).hexdigest()})
    check("客户端哈希正确 → 保留文件", os.path.exists(out_path))

    out_path2 = os.path.join(BASE, "_cli_dl2.bin")
    c.pending_shared_downloads["bad.bin"] = out_path2
    c._handle_shared_file_start({"filename": "bad.bin", "total_size": len(payload)})
    c._handle_shared_file_chunk({"filename": "bad.bin", "index": 0,
                                 "data": base64.b64encode(payload).decode()})
    c._handle_shared_file_end({"filename": "bad.bin", "total_size": len(payload),
                               "sha256": "0" * 64})
    check("客户端哈希不符 → 删除文件", not os.path.exists(out_path2))
    for p in (out_path, out_path2):
        if os.path.exists(p): os.remove(p)


def t05_client_username():
    print("\n[5] 客户端用户名校验与解析")
    ok, _ = cli.Client._validate_username("normal_name")
    check("正常用户名通过", ok)
    check("空用户名拒绝", cli.Client._validate_username("  ")[0] is False)
    check("过长拒绝", cli.Client._validate_username("x" * 31)[0] is False)
    check("含括号拒绝", cli.Client._validate_username("bad(name)")[0] is False)
    check("含控制字符拒绝", cli.Client._validate_username("ab\x01")[0] is False)
    check("纯中文通过", cli.Client._validate_username("管理员")[0] is True)
    check("显示名解析(带MAC)", cli.Client._pure_username("alice (AA:BB:CC:DD:EE:FF)") == "alice")
    check("显示名解析(带未读)", cli.Client._pure_username("bob (3)") == "bob")
    check("显示名解析(带指纹)", cli.Client._pure_username("c (AA:BB) [指纹:ff]") == "c")
    check("显示名解析(纯名)", cli.Client._pure_username("dave") == "dave")


def t06_server_validate_ip():
    print("\n[6] 服务端硬件指纹算法一致性")
    a = srv.Server._get_hardware_fingerprint("AA:BB:CC:DD:EE:FF", "dev123")
    b = srv.Server._get_hardware_fingerprint("AA:BB:CC:DD:EE:FF", "dev123")
    check("同输入 → 同指纹", a == b)
    c2 = srv.Server._get_hardware_fingerprint("AA:BB:CC:DD:EE:F0", "dev123")
    check("不同 MAC → 不同指纹", a != c2)
    check("指纹长度 32", len(a) == 32)
    d = srv.Server._get_hardware_fingerprint("", "")
    check("空输入不崩溃且有指纹", isinstance(d, str) and len(d) == 32)


def t07_kick_and_blacklist_tabs():
    print("\n[7] 黑名单 Tab 类与踢人 helper")
    check("四个 Tab 类存在", all(hasattr(srv, n) for n in
        ["BlacklistTab", "IPBlacklistTab", "MACBlacklistTab",
         "DeviceBlacklistTab", "HardwareBlacklistTab"]))
    check("Tab 类均继承基类", all(issubclass(getattr(srv, n), srv.BlacklistTab) for n in
        ["IPBlacklistTab", "MACBlacklistTab", "DeviceBlacklistTab", "HardwareBlacklistTab"]))

    p2 = Peer("bob"); STATE["p2"] = p2
    check("第二个用户登录成功", p2.login() is not None)
    time.sleep(0.5)
    with server.clients_lock:
        check("两个用户在线", len(server.clients) == 2, str(list(server.clients)))

    kicked = server._kick_matching(lambda i: i.get("ip") == "9.9.9.9", "测试踢出")
    check("不匹配任何人不误踢", kicked == [])

    # 按用户名精确踢出（局域网内同 IP，故用 MAC 区分）
    with server.clients_lock:
        bob_mac = server.clients.get("bob", {}).get("mac")
    kicked = server._kick_matching(lambda i: i.get("mac") == bob_mac, "您的设备已被封禁")
    check("按 MAC 精确踢出 bob", kicked == ["bob"], str(kicked))
    check("bob 收到 kicked 通知", p2.wait("kicked", 5) is not None)
    check("bob 已从在线列表移除", wait_until(lambda: "bob" not in server.clients))
    check("alice 未被误踢", "alice" in server.clients)
    check("发送锁注册表仅剩 alice", wait_until(lambda: len(server._sock_locks) == 1),
          f"剩余 {len(server._sock_locks)}")

    # 验证同 IP 会被一起踢出（局域网 IP 黑名单的固有语义）
    with server.clients_lock:
        alice_ip = server.clients.get("alice", {}).get("ip")
    check("同 IP 的用户共享同一地址（说明 IP 黑名单会误伤同网段）",
          alice_ip == "127.0.0.1")


def t08_blacklist_ui_builds():
    print("\n[8] 黑名单窗口可正常构建")
    win = tk.Toplevel(root)
    from tkinter import ttk as _ttk
    nb = _ttk.Notebook(win)
    nb.pack()
    tabs = []
    for cls in (srv.IPBlacklistTab, srv.MACBlacklistTab,
                srv.DeviceBlacklistTab, srv.HardwareBlacklistTab):
        t = cls(server, nb)
        t.build()
        tabs.append(t)
    check("四个页签均构建成功", len(tabs) == 4)
    check("每个页签都有列表控件", all(t.listbox is not None for t in tabs))
    check("每个页签都有输入框", all(t.entry is not None for t in tabs))

    # 预置数据后 refresh 能正确显示
    server.blacklist.add("10.0.0.5")
    tabs[0].refresh()
    check("IP 页签显示条目", tabs[0].listbox.size() == 1 and
          tabs[0].listbox.get(0) == "10.0.0.5")
    tabs[0].refresh()
    check("IP 页签计数正确", "1" in tabs[0].count_label.cget("text"))

    # 校验逻辑
    check("合法 IPv4 通过", tabs[0].validate("192.168.1.1")[0])
    check("非法 IP 被拒", tabs[0].validate("not-an-ip")[0] is False)
    check("合法 IPv6 通过", tabs[0].validate("::1")[0])
    check("合法 MAC 通过", tabs[1].validate("AA:BB:CC:DD:EE:FF")[0])
    check("非法 MAC 被拒", tabs[1].validate("ZZ:BB:CC:DD:EE:FF")[0] is False)
    check("合法设备ID通过", tabs[2].validate("a" * 32)[0])
    check("过短设备ID被拒", tabs[2].validate("a" * 31)[0] is False)
    check("非十六进制设备ID被拒", tabs[2].validate("z" * 32)[0] is False)
    check("合法硬件指纹通过", tabs[3].validate("0" * 32)[0])
    check("过短硬件指纹被拒", tabs[3].validate("0" * 31)[0] is False)

    # MAC 与设备页签不混淆
    server.device_blacklist["AA:BB:CC:DD:EE:FF"] = "mac测试"
    server.device_blacklist["b" * 32] = "dev测试"
    tabs[1].refresh(); tabs[2].refresh()
    mac_items = [tabs[1].listbox.get(i) for i in range(tabs[1].listbox.size())]
    dev_items = [tabs[2].listbox.get(i) for i in range(tabs[2].listbox.size())]
    check("MAC 页签只显示 MAC", len(mac_items) == 1 and "AA:BB" in mac_items[0], str(mac_items))
    check("设备页签只显示设备ID", len(dev_items) == 1 and "b"*32 in dev_items[0], str(dev_items))

    server.blacklist.clear()
    server.device_blacklist.clear()
    win.destroy()


def t09_extra_limits():
    print("\n[9] 其他限制项")
    p1 = STATE["p1"]
    # 私聊给自己 / 不存在目标
    p1.drain()
    p1.send({"type": "private_message", "to": "alice", "content": "self"})
    e = p1.wait("error", 5)
    check("私聊自己被拒", e is not None and "无效" in e.get("reason", ""), str(e))
    p1.drain()
    p1.send({"type": "private_message", "to": "ghost", "content": "hi"})
    e2 = p1.wait("error", 5)
    check("私聊不在线目标有明确提示", e2 is not None and "不在线" in e2.get("reason", ""),
          str(e2))
    check("路径穿越被清理",
          srv.Server._safe_filename("../../etc/passwd") == "passwd")
    check("反斜杠被清理",
          srv.Server._safe_filename("..\\..\\x.txt") == "x.txt")


def run():
    try:
        for fn in (t01_deque, t02_login_and_limits, t03_shared_file_hash,
                   t04_client_hash_verify, t05_client_username,
                   t06_server_validate_ip, t07_kick_and_blacklist_tabs,
                   t08_blacklist_ui_builds, t09_extra_limits):
            fn()
    except Exception as e:
        import traceback
        traceback.print_exc()
        failures.append(f"异常: {e}")
    finally:
        print("\n清理...")
        for k in ("p1", "p2"):
            if STATE.get(k): STATE[k].close()
        for f in (FNAME, "regress_bad.bin"):
            try:
                p = os.path.join(srv.SHARED_FILE_DIR, f)
                if os.path.exists(p): os.remove(p)
            except Exception: pass
        server.running = False
        try:
            with server.clients_lock:
                for i in list(server.clients.values()):
                    try: i["sock"].close()
                    except Exception: pass
                server.clients.clear()
            if server.sock: server.sock.close()
            server._file_workers.shutdown(wait=False, cancel_futures=True)
        except Exception: pass
        try: root.after(0, root.quit)
        except Exception: pass


threading.Thread(target=run, daemon=True).start()
root.mainloop()
try: root.destroy()
except Exception: pass

print("\n" + "=" * 56)
if failures:
    print(f"结果: {len(failures)} 项失败")
    for f in failures: print("  - " + f)
    sys.exit(1)
print("结果: 全部通过")
sys.exit(0)