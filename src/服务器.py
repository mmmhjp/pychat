import socket
import threading
from concurrent.futures import ThreadPoolExecutor
import json
import struct
import time
import queue
import tkinter as tk
from tkinter import scrolledtext, messagebox, simpledialog, filedialog, ttk
import os
import base64
from datetime import datetime
import traceback
from collections import defaultdict, deque
import hashlib
import uuid
import platform
import re


PORT = 9999
DISCOVERY_PORT = 9998
HEARTBEAT_TIMEOUT = 30
PRIVATE_LOG_FILE = 'private_chat_log.txt'
SHARED_FILE_DIR = 'shared_files'
MAX_SHARED_FILES = 20
BLACKLIST_FILE = 'blacklist.txt'
MUTE_LIST_FILE = 'mute_list.txt'
DEVICE_BLACKLIST_FILE = 'device_blacklist.txt'
MAX_BODY_SIZE = 500 * 1024 * 1024
MAX_SHARED_FILE_SIZE = 512 * 1024 * 1024  # 单个共享文件上限
FILE_CHUNK_SIZE = 256 * 1024              # 共享文件分块大小（原始字节）
QUEUE_BATCH_SIZE = 30
GUI_UPDATE_INTERVAL = 100
MAX_LOG_LINES = 5000
MAX_USERNAME_LENGTH = 30
MAX_MESSAGE_LENGTH = 5000        # 单条聊天消息最大长度
MAX_CONNECTIONS = 100            # 最大并发客户端数
MAX_PRIVATE_LOG_BYTES = 5 * 1024 * 1024  # 私聊日志超过该大小即轮转
HISTORY_SEND_COUNT = 100         # 新用户上线时推送的历史条数
SOCKET_RECV_TIMEOUT = 10.0       # 单次 recv 超时
SOCKET_MAX_TIMEOUTS = 20         # 连续超时次数上限后判定连接失效
LOGIN_TIMEOUT = 15.0             # 登录握手超时
ADMIN_NAME = "管理员"
MAX_MESSAGE_HISTORY = 1000
SERVER_HIDDEN_FILE = 'server_hidden.txt'
HARDWARE_BLACKLIST_FILE = 'hardware_blacklist.txt'  # 硬件黑名单

# ============================================================================
# 黑名单管理标签页
#
# 四个标签页（IP / MAC / 设备 / 硬件）的界面结构与增删改查流程几乎相同，
# 因此抽出 BlacklistTab 基类统一处理「输入框 + 列表 + 计数 + 按钮」的搭建、
# 从在线用户选取、添加/移除/清空/刷新，再由子类只提供差异部分：
#   - entries()        该页签要展示哪些条目（纯文本列表或 (key, reason) 元组）
#   - validate(value)  添加前的输入校验，返回 (是否通过, 提示信息)
#   - add(value)       执行添加，返回是否真的新增了
#   - remove(value)    执行移除
#   - clear()          清空全部
# ============================================================================


class BlacklistTab:
    """黑名单标签页基类：负责通用界面搭建与增删改查流程。"""

    TAB_TITLE = "黑名单"
    INPUT_LABEL = "输入内容："
    ENTRY_WIDTH = 25
    UNIT = "条"
    ADD_BUTTON = "添加"
    LIST_HINT = ""
    ASK_REASON = False            # 是否询问封禁原因
    REASON_PROMPT = "输入加入黑名单的原因："
    CONFIRM_BEFORE_ADD = False    # 添加前是否二次确认
    CONFIRM_PROMPT = ""
    DUPLICATE_MSG = "该条目已在黑名单中"

    def __init__(self, server: 'Server', notebook: ttk.Notebook):
        self.server = server
        self.win = server.root
        self.notebook = notebook
        self.frame = tk.Frame(notebook)
        self.notebook.add(self.frame, text=self.TAB_TITLE)
        self.entry: tk.Entry | None = None
        self.listbox: tk.Listbox | None = None
        self.count_label: tk.Label | None = None

    # ---------- 需要子类实现 ----------

    def entries(self) -> list:
        """返回当前所有条目。元素为 str，或 (key, reason) 二元组。"""
        raise NotImplementedError

    def validate(self, value: str):
        """添加前校验，返回 (是否通过, 错误提示)。"""
        return True, ""

    def add(self, value: str, reason: str) -> bool:
        """执行添加，返回是否真的新增。"""
        raise NotImplementedError

    def remove(self, value: str) -> bool:
        """执行移除。"""
        raise NotImplementedError

    def clear(self) -> None:
        """清空该页签的全部条目。"""
        raise NotImplementedError

    def normalize(self, item) -> str:
        """把 entries() 的元素转换为列表中显示的文本。"""
        return item if isinstance(item, str) else f"{item[0]} - {item[1]}"

    def key_of(self, item) -> str:
        """取出条目的主键（移除时用）。"""
        return item if isinstance(item, str) else item[0]

    def matches_client(self, info: dict, value: str) -> bool:
        """判断某个在线用户是否命中该条目（用于踢出）。"""
        return False

    def kick_reason(self, value: str, reason: str) -> str:
        """踢出在线用户时给对方的提示语。"""
        return f'您的设备已被列入{self.TAB_TITLE}: {value}'

    # ---------- 通用流程 ----------

    def build(self) -> None:
        """构建整个页签界面。"""
        if self.LIST_HINT:
            for line in self.LIST_HINT:
                tk.Label(self.frame, text=line, font=("Microsoft YaHei", 9),
                         fg="#FF5722" if line.startswith("⚠️") else "#666666",
                         wraplength=860, justify='left').pack(pady=(8, 0))

        input_frame = tk.Frame(self.frame)
        input_frame.pack(pady=5, padx=10, fill='x')
        tk.Label(input_frame, text=self.INPUT_LABEL,
                 font=("Microsoft YaHei", 10)).pack(side='left')
        self.entry = tk.Entry(input_frame, font=("Microsoft YaHei", 10),
                              width=self.ENTRY_WIDTH)
        self.entry.pack(side='left', padx=5)
        tk.Button(input_frame, text=self.ADD_BUTTON, command=self.on_add,
                  font=("Microsoft YaHei", 9), bg="#4CAF50", fg="white").pack(side='left', padx=5)
        tk.Button(input_frame, text="从在线用户添加", command=self.on_add_from_online,
                  font=("Microsoft YaHei", 9), bg="#2196F3", fg="white").pack(side='left', padx=5)

        list_frame = tk.Frame(self.frame)
        list_frame.pack(pady=5, padx=10, fill='both', expand=True)

        listbox_frame = tk.Frame(list_frame)
        listbox_frame.pack(fill='both', expand=True)
        self.listbox = tk.Listbox(listbox_frame, font=("Microsoft YaHei", 10))
        self.listbox.pack(side='left', fill='both', expand=True)
        scrollbar = tk.Scrollbar(listbox_frame, orient='vertical',
                                  command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')

        self.count_label = tk.Label(self.frame, text=f"共 0 {self.UNIT}",
                                    font=("Microsoft YaHei", 9))
        self.count_label.pack(pady=2)

        btn_frame = tk.Frame(self.frame)
        btn_frame.pack(pady=10, padx=10, fill='x')
        tk.Button(btn_frame, text="移除选中", command=self.on_remove,
                  font=("Microsoft YaHei", 9), bg="#FF5722", fg="white",
                  width=12).pack(side='left', padx=5)
        tk.Button(btn_frame, text="清空全部", command=self.on_clear,
                  font=("Microsoft YaHei", 9), bg="#E91E63", fg="white",
                  width=12).pack(side='left', padx=5)
        tk.Button(btn_frame, text="刷新", command=self.refresh,
                  font=("Microsoft YaHei", 9), width=10).pack(side='left', padx=5)

        self.entry.bind('<Return>', lambda e: self.on_add())
        self.refresh()

    def refresh(self) -> None:
        """重新拉取数据并刷新列表显示。"""
        if not self.listbox or not self.count_label:
            return
        try:
            self.listbox.delete(0, tk.END)
            items = self.entries()
            for item in items:
                self.listbox.insert(tk.END, self.normalize(item))
            self.count_label.config(text=f"共 {len(items)} {self.UNIT}")
        except tk.TclError:
            pass

    def _ask_reason(self) -> str | None:
        if not self.ASK_REASON:
            return ""
        return simpledialog.askstring("原因", self.REASON_PROMPT, parent=self.win)

    def on_add(self) -> None:
        """从输入框读取内容并添加。"""
        value = self.entry.get().strip()
        if not value:
            messagebox.showwarning("提示", f"请输入{self.INPUT_LABEL.rstrip('：')}",
                                   parent=self.win)
            return

        ok, hint = self.validate(value)
        if not ok:
            messagebox.showwarning("提示", hint, parent=self.win)
            return

        if self.CONFIRM_BEFORE_ADD and not messagebox.askyesno(
                "⚠️ 确认添加", self.CONFIRM_PROMPT.format(value=value), parent=self.win):
            return

        reason = self._ask_reason()
        if reason is None:
            return

        if self.add(value, reason):
            self.refresh()
            self.entry.delete(0, tk.END)
            messagebox.showinfo("成功", f"{value} 已加入{self.TAB_TITLE}", parent=self.win)
            self.server._kick_matching(
                lambda info: self.matches_client(info, value),
                self.kick_reason(value, reason))
        else:
            messagebox.showinfo("提示", self.DUPLICATE_MSG, parent=self.win)

    def on_add_from_online(self) -> None:
        """挑选一个在线用户，取其对应标识加入黑名单。"""
        with self.server.clients_lock:
            if not self.server.clients:
                messagebox.showinfo("提示", "当前没有在线用户", parent=self.win)
                return

        username = simpledialog.askstring(
            "选择用户", f"请输入要加入{self.TAB_TITLE}的用户名：", parent=self.win)
        if not username:
            return

        with self.server.clients_lock:
            if username not in self.server.clients:
                messagebox.showwarning("提示", f"用户 {username} 不在线", parent=self.win)
                return
            value = self.value_from_client(self.server.clients[username])
            extra = self.describe_client(self.server.clients[username])

        if not value:
            messagebox.showwarning(
                "提示", f"无法获取用户 {username} 的{self.UNIT[:-1]}信息", parent=self.win)
            return

        if self.CONFIRM_BEFORE_ADD and not messagebox.askyesno(
                "⚠️ 确认添加",
                self.CONFIRM_PROMPT.format(value=f"{username}{extra}\n{value}"),
                parent=self.win):
            return

        reason = self._ask_reason()
        if reason is None:
            return

        if self.add(value, reason):
            self.refresh()
            messagebox.showinfo("成功",
                                f"用户 {username}{extra} 已加入{self.TAB_TITLE}", parent=self.win)
            self.server._kick_username(username, self.kick_reason(value, reason))
        else:
            messagebox.showinfo("提示", self.DUPLICATE_MSG, parent=self.win)

    def value_from_client(self, info: dict) -> str:
        """从在线用户信息中取出该页签关心的标识。"""
        return ""

    def describe_client(self, info: dict) -> str:
        """「从在线用户添加」成功提示里的附加说明。"""
        return ""

    def on_remove(self) -> None:
        """移除列表中选中的条目。"""
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", f"请先选择要移除的{self.UNIT[:-1]}", parent=self.win)
            return
        text = self.listbox.get(sel[0])
        value = text.split(' - ')[0]
        if messagebox.askyesno("确认", f"确定将 {value} 从{self.TAB_TITLE}移除吗？",
                               parent=self.win):
            if self.remove(value):
                self.refresh()

    def on_clear(self) -> None:
        """清空该页签全部条目。"""
        if messagebox.askyesno("确认", f"确定要清空所有{self.TAB_TITLE}吗？", parent=self.win):
            self.clear()
            self.refresh()
            self.server._log(f"已清空所有{self.TAB_TITLE}")


class IPBlacklistTab(BlacklistTab):
    """🌐 IP 黑名单"""

    TAB_TITLE = "IP黑名单"
    INPUT_LABEL = "输入IP地址："
    UNIT = "个IP"
    ADD_BUTTON = "添加IP"
    LIST_HINT = ("⚠️ 封禁IP会同时踢出该IP下的所有用户",
                 "💡 若只想封禁单个用户，建议改用 MAC / 设备ID / 硬件黑名单")
    CONFIRM_BEFORE_ADD = True
    CONFIRM_PROMPT = "将封禁IP：{value}\n\n⚠️ 该IP下的所有在线用户都会被踢出，确定继续吗？"
    DUPLICATE_MSG = "该IP已在黑名单中"

    def entries(self) -> list:
        with self.server.blacklist_lock:
            return sorted(self.server.blacklist)

    def validate(self, value: str):
        # 允许 IPv4 / IPv6，做一次基本合法性检查
        try:
            socket.inet_pton(socket.AF_INET, value)
            return True, ""
        except OSError:
            pass
        try:
            socket.inet_pton(socket.AF_INET6, value)
            return True, ""
        except OSError:
            return False, "IP地址格式错误"

    def add(self, value: str, reason: str) -> bool:
        return self.server._add_to_blacklist(ip=value)

    def remove(self, value: str) -> bool:
        return self.server._remove_from_blacklist(ip=value)

    def clear(self) -> None:
        with self.server.blacklist_lock:
            self.server.blacklist.clear()
        self.server._save_blacklist()

    def value_from_client(self, info: dict) -> str:
        return info.get('ip', '')

    def matches_client(self, info: dict, value: str) -> bool:
        return info.get('ip') == value


class MACBlacklistTab(BlacklistTab):
    """🔗 MAC 黑名单"""

    TAB_TITLE = "MAC黑名单"
    INPUT_LABEL = "输入MAC地址："
    UNIT = "个MAC"
    ADD_BUTTON = "添加MAC"
    ASK_REASON = True
    DUPLICATE_MSG = "该MAC已在黑名单中"
    MAC_PATTERN = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')

    @staticmethod
    def is_mac(value: str) -> bool:
        return ':' in value and len(value) == 17

    def entries(self) -> list:
        with self.server.blacklist_lock:
            return [item for item in sorted(self.server.device_blacklist.items())
                    if self.is_mac(item[0])]

    def validate(self, value: str):
        value = value.upper()
        if not self.MAC_PATTERN.match(value):
            return False, "MAC地址格式错误，正确格式: AA:BB:CC:DD:EE:FF"
        return True, ""

    def add(self, value: str, reason: str) -> bool:
        return self.server._add_to_blacklist(mac=value.upper(), reason=reason)

    def remove(self, value: str) -> bool:
        return self.server._remove_from_blacklist(mac=value)

    def clear(self) -> None:
        with self.server.blacklist_lock:
            for mac in [m for m in self.server.device_blacklist if self.is_mac(m)]:
                del self.server.device_blacklist[mac]
        self.server._save_device_blacklist()

    def value_from_client(self, info: dict) -> str:
        mac = info.get('mac', '')
        return '' if mac == "未知MAC" else mac

    def describe_client(self, info: dict) -> str:
        return f" (MAC:{info.get('mac', '')})"

    def matches_client(self, info: dict, value: str) -> bool:
        return info.get('mac') == value

    def kick_reason(self, value: str, reason: str) -> str:
        return f'您的设备已被MAC黑名单: {reason}'


class DeviceBlacklistTab(BlacklistTab):
    """📱 设备ID 黑名单"""

    TAB_TITLE = "设备黑名单"
    INPUT_LABEL = "输入设备ID："
    ENTRY_WIDTH = 30
    UNIT = "个设备"
    ADD_BUTTON = "添加设备ID"
    ASK_REASON = True
    LIST_HINT = ("设备ID由服务器自动生成（32位十六进制），与MAC地址不同",
                 "💡 修改MAC地址无法绕过设备黑名单")
    DUPLICATE_MSG = "该设备ID已在黑名单中"

    def entries(self) -> list:
        with self.server.blacklist_lock:
            return sorted(item for item in self.server.device_blacklist.items()
                          if not MACBlacklistTab.is_mac(item[0]))

    def validate(self, value: str):
        if len(value) != 32:
            return False, "设备ID应为32位十六进制字符串"
        if not all(c in "0123456789abcdefABCDEF" for c in value):
            return False, "设备ID只能包含十六进制字符"
        return True, ""

    def add(self, value: str, reason: str) -> bool:
        return self.server._add_to_blacklist(device_id=value, reason=reason)

    def remove(self, value: str) -> bool:
        return self.server._remove_from_blacklist(device_id=value)

    def clear(self) -> None:
        with self.server.blacklist_lock:
            for device_id in [d for d in self.server.device_blacklist
                              if not MACBlacklistTab.is_mac(d)]:
                del self.server.device_blacklist[device_id]
        self.server._save_device_blacklist()

    def value_from_client(self, info: dict) -> str:
        return info.get('device_id', '')

    def matches_client(self, info: dict, value: str) -> bool:
        return info.get('device_id') == value


class HardwareBlacklistTab(BlacklistTab):
    """💻 硬件指纹黑名单"""

    TAB_TITLE = "硬件黑名单"
    INPUT_LABEL = "输入硬件指纹："
    ENTRY_WIDTH = 30
    UNIT = "个设备"
    ADD_BUTTON = "添加硬件指纹"
    ASK_REASON = True
    LIST_HINT = ("⚠️ 硬件黑名单基于CPU、硬盘、主板、MAC、BIOS UUID组合生成，"
                 "即使修改MAC也无法绕过",
                 "💡 此黑名单用于永久封禁违规用户，添加后请谨慎移除")
    CONFIRM_BEFORE_ADD = True
    CONFIRM_PROMPT = "将永久封禁以下硬件：\n指纹: {value}\n\n⚠️ 此操作不可逆，确定继续吗？"
    DUPLICATE_MSG = "该硬件指纹已在黑名单中"

    def entries(self) -> list:
        with self.server.hardware_blacklist_lock:
            return sorted(self.server.hardware_blacklist)

    def validate(self, value: str):
        if len(value) != 32:
            return False, "硬件指纹应为32位十六进制字符串"
        if not all(c in "0123456789abcdefABCDEF" for c in value):
            return False, "硬件指纹只能包含十六进制字符"
        return True, ""

    def add(self, value: str, reason: str) -> bool:
        return self.server._add_hardware_to_blacklist(value, reason)

    def remove(self, value: str) -> bool:
        return self.server._remove_hardware_from_blacklist(value)

    def clear(self) -> None:
        with self.server.hardware_blacklist_lock:
            self.server.hardware_blacklist.clear()
        self.server._save_hardware_blacklist()

    def value_from_client(self, info: dict) -> str:
        fingerprint = info.get('hardware_fingerprint', '')
        if fingerprint:
            return fingerprint
        # 与 _get_hardware_fingerprint 共用同一套算法，避免指纹不一致导致封禁失效
        return self.server._get_hardware_fingerprint(
            info.get('mac', ''), info.get('device_id', ''))

    def describe_client(self, info: dict) -> str:
        mac = info.get('mac', '')
        return f" (MAC:{mac})" if mac else ""

    def matches_client(self, info: dict, value: str) -> bool:
        # 客户端未上报指纹时，兜底用服务端算法算一次再比对
        actual = info.get('hardware_fingerprint', '')
        if not actual:
            actual = self.server._get_hardware_fingerprint(
                info.get('mac', ''), info.get('device_id', ''))
        return actual == value

    def kick_reason(self, value: str, reason: str) -> str:
        return f'您的设备已被硬件黑名单: {reason}'


class Server:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("服务器管理 - 聊天室")
        self.root.geometry("1000x750")
        self.root.minsize(800, 600)

        # 设置应用图标
        self._set_app_icon()

        self.clients: dict = {}
        self.clients_lock = threading.RLock()
        # id(socket) -> 发送锁：让 _send_msg 能 O(1) 找到对应锁，
        # 避免广播时对每个接收者都遍历一遍 clients（O(n^2)）
        self._sock_locks: dict = {}

        self.sock: socket.socket | None = None
        self.running = False

        self.msg_queue: queue.Queue = queue.Queue(maxsize=2000)

        # 黑名单系统
        self.blacklist: set = set()
        self.device_blacklist: dict = {}
        self.blacklist_lock = threading.RLock()
        # 硬件黑名单系统
        self.hardware_blacklist: set = set()
        self.hardware_blacklist_lock = threading.RLock()
        self._load_hardware_blacklist()

        # 禁言系统
        self.muted_users: dict = {}
        self.mute_lock = threading.RLock()

        # 消息历史管理
        self.message_history: deque = deque(maxlen=MAX_MESSAGE_HISTORY)
        self.message_history_lock = threading.Lock()

        self.file_lock = threading.Lock()

        # 文件传输专用线程池：限制并发数，避免大量文件请求导致线程爆炸
        self._file_workers = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix='fileio')

        # 分块上传会话：username -> {fp, fname, received, total, path}
        self.uploads: dict = {}
        self.uploads_lock = threading.RLock()

        self.ip_mode = tk.StringVar(value="dual")
        self.server_hidden = tk.BooleanVar(value=False)
        # 后台线程只能读这个普通布尔值：Tk 变量跨线程访问会触发
        # "main thread is not in main loop"，且不是线程安全的
        self._server_hidden_flag = False

        # ===== 修复：用户列表脏标志，解决队列满时在线用户不显示的问题 =====
        self._user_list_needs_update = False
        self._user_list_update_lock = threading.Lock()

        # 加载所有数据
        self._load_server_hidden()
        self._load_blacklist()
        self._load_device_blacklist()
        self._load_mute_list()

        if not os.path.exists(SHARED_FILE_DIR):
            os.makedirs(SHARED_FILE_DIR)

        # 初始化所有UI组件变量为None
        self.main_frame: tk.Frame | None = None
        self.left_frame: tk.Frame | None = None
        self.right_frame: tk.Frame | None = None
        self.log_area: scrolledtext.ScrolledText | None = None
        self.admin_input: tk.Text | None = None
        self.hidden_check: tk.Checkbutton | None = None
        self.addr_text: tk.Text | None = None
        self.user_listbox: tk.Listbox | None = None
        self.muted_users_listbox: tk.Listbox | None = None
        self.status_bar: tk.Label | None = None

        # 先设置状态栏（在setup_ui之前）
        self._setup_status_bar()

        # 设置UI
        self._setup_ui()

        # ===== 在 setup_ui 之后调用 =====
        self._update_muted_list_display()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 尝试启动服务器（带自动回退）
        if not self._try_start_server():
            root.destroy()
            return

        threading.Thread(target=self._udp_discovery_service, daemon=True).start()
        threading.Thread(target=self._accept_clients, daemon=True).start()
        threading.Thread(target=self._heartbeat_check, daemon=True).start()
        threading.Thread(target=self._mute_check, daemon=True).start()
        self._process_gui()

    def _set_app_icon(self) -> None:
        """设置应用图标"""
        try:
            if platform.system() == 'Windows':
                try:
                    import ctypes
                    app_id = 'chatserver.app.1.0'
                    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
                except (AttributeError, OSError):
                    pass
            try:
                self.root.iconbitmap(default='')
            except tk.TclError:
                pass
        except Exception:
            pass

    def _udp_discovery_service(self):
        """监听UDP广播，响应服务器发现请求"""
        udp_sock = None
        try:
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp_sock.bind(('0.0.0.0', DISCOVERY_PORT))
            udp_sock.settimeout(1.0)
            while self.running:
                try:
                    data, addr = udp_sock.recvfrom(1024)
                    if data == b"DISCOVER_CHAT_SERVER:v1":
                        # ===== 隐藏模式不响应扫描 =====
                        if self._is_server_hidden():
                            continue  # 假装不存在，不回复任何信息

                        ips = self._get_local_ips()
                        all_ips = ips['ipv4'] + ips['ipv6']
                        if not all_ips:
                            all_ips = [addr[0]]
                        response = json.dumps({
                            'type': 'chat_server',
                            'port': PORT,
                            'ips': all_ips
                        }).encode('utf-8')
                        udp_sock.sendto(response, addr)
                except socket.timeout:
                    continue
                except Exception as e:
                    self._log(f"UDP发现服务异常: {e}")
        except Exception as e:
            self._log(f"UDP发现服务异常: {e}")
        finally:
            if udp_sock:
                try:
                    udp_sock.close()
                except Exception:
                    pass

    def _setup_status_bar(self) -> None:
        """设置状态栏"""
        self.status_bar = tk.Label(
            self.root, text="就绪", bd=1, relief=tk.SUNKEN, anchor=tk.W
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _setup_ui(self) -> None:
        """设置完整的用户界面"""
        # 主框架
        self.main_frame = tk.Frame(self.root)
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左右分栏
        self.main_frame.grid_rowconfigure(0, weight=1)
        self.main_frame.grid_columnconfigure(0, weight=3)
        self.main_frame.grid_columnconfigure(1, weight=1)

        # 左侧面板 - 日志和管理员输入
        self._setup_left_panel()

        # 右侧面板 - 控制面板
        self._setup_right_panel()

    def _setup_left_panel(self) -> None:
        """设置左侧面板"""
        self.left_frame = tk.Frame(self.main_frame)
        self.left_frame.grid(row=0, column=0, sticky='nsew', padx=(0, 5))
        self.left_frame.grid_rowconfigure(0, weight=1)
        self.left_frame.grid_rowconfigure(1, weight=0)
        self.left_frame.grid_columnconfigure(0, weight=1)

        # 日志区域
        log_frame = tk.LabelFrame(self.left_frame, text="📋 服务器日志", font=("Microsoft YaHei", 10, "bold"))
        log_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 5))
        log_frame.grid_rowconfigure(0, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        self.log_area = scrolledtext.ScrolledText(
            log_frame, state='disabled',
            font=("Microsoft YaHei", 10), wrap=tk.WORD
        )
        self.log_area.grid(row=0, column=0, sticky='nsew')

        # 配置日志标签样式
        self.log_area.tag_config('public', foreground='#333333')
        self.log_area.tag_config('private', foreground='#9C27B0')
        self.log_area.tag_config('system', foreground='#666666', font=("Microsoft YaHei", 9, "italic"))
        self.log_area.tag_config('broadcast', foreground='#FF5722', font=("Microsoft YaHei", 10, "bold"))
        self.log_area.tag_config('warning', foreground='#FF9800')

        # 管理员发言区域
        admin_frame = tk.LabelFrame(self.left_frame, text="💬 管理员群聊发言", font=("Microsoft YaHei", 9))
        admin_frame.grid(row=1, column=0, sticky='ew', pady=(0, 5))
        admin_frame.grid_columnconfigure(0, weight=1)
        admin_frame.grid_columnconfigure(1, weight=0)

        self.admin_input = tk.Text(admin_frame, height=3, font=("Microsoft YaHei", 10))
        self.admin_input.grid(row=0, column=0, sticky='ew', padx=5, pady=5)
        self.admin_input.bind('<Return>', self._on_admin_input_return)

        tk.Button(
            admin_frame, text="发送\n(Enter)", width=8, height=3,
            command=self._send_admin_chat,
            font=("Microsoft YaHei", 9, "bold"),
            bg="#FF9800", fg="white"
        ).grid(row=0, column=1, padx=(0, 5), pady=5)

    def _setup_right_panel(self) -> None:
        """设置右侧控制面板"""
        self.right_frame = tk.Frame(self.main_frame)
        self.right_frame.grid(row=0, column=1, sticky='nsew')
        self.right_frame.grid_rowconfigure(0, weight=0)  # 服务器状态
        self.right_frame.grid_rowconfigure(1, weight=0)  # IP模式
        self.right_frame.grid_rowconfigure(2, weight=0)  # 服务器地址
        self.right_frame.grid_rowconfigure(3, weight=1)  # 在线用户
        self.right_frame.grid_rowconfigure(4, weight=1)  # 禁言列表
        self.right_frame.grid_rowconfigure(5, weight=0)  # 按钮
        self.right_frame.grid_rowconfigure(6, weight=0)  # 功能按钮
        self.right_frame.grid_columnconfigure(0, weight=1)

        # 1. 服务器状态
        status_frame = tk.LabelFrame(self.right_frame, text="🔧 服务器状态", font=("Microsoft YaHei", 9))
        status_frame.grid(row=0, column=0, sticky='ew', pady=(0, 5))

        self.hidden_check = tk.Checkbutton(
            status_frame, text="隐藏服务器",
            variable=self.server_hidden,
            command=self._toggle_server_hidden,
            font=("Microsoft YaHei", 9)
        )
        self.hidden_check.pack(anchor='w', padx=5, pady=2)

        # 2. IP协议模式
        ip_mode_frame = tk.LabelFrame(self.right_frame, text="🌐 IP 协议模式", font=("Microsoft YaHei", 9))
        ip_mode_frame.grid(row=1, column=0, sticky='ew', pady=(0, 5))
        tk.Radiobutton(
            ip_mode_frame, text="IPv4 + IPv6 双栈", variable=self.ip_mode,
            value="dual", font=("Microsoft YaHei", 9),
            command=self._on_ip_mode_change
        ).pack(anchor='w', padx=5)
        tk.Radiobutton(
            ip_mode_frame, text="仅 IPv4", variable=self.ip_mode,
            value="ipv4", font=("Microsoft YaHei", 9),
            command=self._on_ip_mode_change
        ).pack(anchor='w', padx=5)
        tk.Radiobutton(
            ip_mode_frame, text="仅 IPv6", variable=self.ip_mode,
            value="ipv6", font=("Microsoft YaHei", 9),
            command=self._on_ip_mode_change
        ).pack(anchor='w', padx=5)

        # 3. 服务器地址
        addr_frame = tk.LabelFrame(self.right_frame, text="📡 服务器地址", font=("Microsoft YaHei", 9))
        addr_frame.grid(row=2, column=0, sticky='ew', pady=(0, 5))
        self.addr_text = tk.Text(
            addr_frame, height=4, font=("Microsoft YaHei", 8),
            state='disabled', bg=self.root.cget('bg'),
            relief='flat', wrap=tk.WORD
        )
        self.addr_text.pack(fill='x', padx=5, pady=2)

        # 4. 在线用户列表
        user_frame = tk.LabelFrame(self.right_frame, text="👥 在线用户", font=("Microsoft YaHei", 9))
        user_frame.grid(row=3, column=0, sticky='nsew', pady=(0, 5))
        user_frame.grid_rowconfigure(0, weight=1)
        user_frame.grid_columnconfigure(0, weight=1)

        self.user_listbox = tk.Listbox(user_frame, font=("Microsoft YaHei", 9), selectmode=tk.EXTENDED)
        self.user_listbox.grid(row=0, column=0, sticky='nsew')

        user_scrollbar = tk.Scrollbar(user_frame, orient='vertical', command=self.user_listbox.yview)
        self.user_listbox.configure(yscrollcommand=user_scrollbar.set)
        user_scrollbar.grid(row=0, column=1, sticky='ns')

        # 5. 禁言列表
        mute_frame = tk.LabelFrame(self.right_frame, text="🔇 禁言列表", font=("Microsoft YaHei", 9))
        mute_frame.grid(row=4, column=0, sticky='nsew', pady=(0, 5))
        mute_frame.grid_rowconfigure(0, weight=1)
        mute_frame.grid_columnconfigure(0, weight=1)

        self.muted_users_listbox = tk.Listbox(mute_frame, font=("Microsoft YaHei", 9))
        self.muted_users_listbox.grid(row=0, column=0, sticky='nsew')

        mute_scrollbar = tk.Scrollbar(mute_frame, orient='vertical', command=self.muted_users_listbox.yview)
        self.muted_users_listbox.configure(yscrollcommand=mute_scrollbar.set)
        mute_scrollbar.grid(row=0, column=1, sticky='ns')

        # 6. 用户管理按钮
        btn_frame = tk.Frame(self.right_frame)
        btn_frame.grid(row=5, column=0, sticky='ew', pady=5)
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)
        btn_frame.grid_columnconfigure(2, weight=1)

        tk.Button(
            btn_frame, text="踢出", command=self._kick_user,
            font=("Microsoft YaHei", 9), bg="#f44336", fg="white"
        ).grid(row=0, column=0, sticky='ew', padx=1)
        tk.Button(
            btn_frame, text="禁言", command=self._mute_user_dialog,
            font=("Microsoft YaHei", 9), bg="#FF9800", fg="white"
        ).grid(row=0, column=1, sticky='ew', padx=1)
        tk.Button(
            btn_frame, text="解禁", command=self._unmute_user_dialog,
            font=("Microsoft YaHei", 9), bg="#4CAF50", fg="white"
        ).grid(row=0, column=2, sticky='ew', padx=1)

        tk.Button(
            btn_frame, text="📢 广播", command=self._broadcast_msg,
            font=("Microsoft YaHei", 9), bg="#FF5722", fg="white"
        ).grid(row=1, column=0, sticky='ew', padx=1, pady=(2, 0))
        tk.Button(
            btn_frame, text="🗑️ 清空记录", command=self._clear_chat_history,
            font=("Microsoft YaHei", 9), bg="#E91E63", fg="white"
        ).grid(row=1, column=1, sticky='ew', padx=1, pady=(2, 0))
        tk.Button(
            btn_frame, text="🔄 刷新", command=self._update_user_list_gui,
            font=("Microsoft YaHei", 9)
        ).grid(row=1, column=2, sticky='ew', padx=1, pady=(2, 0))

        # 7. 功能按钮
        func_frame = tk.Frame(self.right_frame)
        func_frame.grid(row=6, column=0, sticky='ew', pady=(5, 0))
        func_frame.grid_columnconfigure(0, weight=1)
        func_frame.grid_columnconfigure(1, weight=1)
        func_frame.grid_columnconfigure(2, weight=1)

        tk.Button(
            func_frame, text="📁 共享文件", command=self._manage_shared_files,
            font=("Microsoft YaHei", 9), bg="#4CAF50", fg="white"
        ).grid(row=0, column=0, sticky='ew', padx=1)
        tk.Button(
            func_frame, text="📝 私聊记录", command=self._view_private_logs,
            font=("Microsoft YaHei", 9), bg="#2196F3", fg="white"
        ).grid(row=0, column=1, sticky='ew', padx=1)
        tk.Button(
            func_frame, text="🚫 黑名单", command=self._manage_blacklist,
            font=("Microsoft YaHei", 9), bg="#9C27B0", fg="white"
        ).grid(row=0, column=2, sticky='ew', padx=1)

    def _update_muted_list_display(self) -> None:
        """更新禁言列表显示"""
        # 检查组件是否存在
        if self.muted_users_listbox is None:
            return
        try:
            self.muted_users_listbox.delete(0, tk.END)
            with self.mute_lock:
                # 清理已过期的禁言
                expired = []
                for username, unmute_time in list(self.muted_users.items()):
                    if time.time() >= unmute_time:
                        expired.append(username)
                for username in expired:
                    del self.muted_users[username]
                if expired:
                    self._save_mute_list()

                if not self.muted_users:
                    self.muted_users_listbox.insert(tk.END, "（暂无禁言用户）")
                else:
                    for username, unmute_time in sorted(self.muted_users.items()):
                        remaining = max(0, int((unmute_time - time.time()) // 60))
                        if remaining > 0:
                            self.muted_users_listbox.insert(tk.END, f"{username} ({remaining}分钟)")
                        else:
                            self.muted_users_listbox.insert(tk.END, f"{username} (即将解除)")
        except Exception as e:
            self._log(f"更新禁言列表失败: {e}")

    # ============ 服务器启动和管理方法 ============

    def _get_local_ips(self) -> dict:
        """获取本机IP地址（无外部依赖）"""
        ips: dict = {'ipv4': [], 'ipv6': []}
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None):
                family = info[0]
                addr = info[4][0]
                if family == socket.AF_INET and addr not in ips['ipv4']:
                    ips['ipv4'].append(addr)
                elif family == socket.AF_INET6 and addr not in ips['ipv6']:
                    if not addr.startswith('fe80:') and '%' not in addr:
                        ips['ipv6'].append(addr)
        except socket.gaierror:
            pass

        if not ips['ipv4']:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(('8.8.8.8', 80))
                ips['ipv4'].append(s.getsockname()[0])
                s.close()
            except OSError:
                pass
        return ips

    def _get_client_mac(self, ip: str) -> str:
        """获取客户端MAC地址"""
        try:
            import subprocess
            if platform.system() == 'Windows':
                result = subprocess.run(['arp', '-a', ip], capture_output=True, text=True, timeout=3)
                lines = result.stdout.split('\n')
                for line in lines:
                    if ip in line:
                        mac_match = re.search(r'([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})', line.upper())
                        if mac_match:
                            return mac_match.group(0)
            else:
                # Linux/Mac
                result = subprocess.run(['arp', '-n', ip], capture_output=True, text=True, timeout=3)
                lines = result.stdout.split('\n')
                for line in lines:
                    if ip in line:
                        mac_match = re.search(r'([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})', line.upper())
                        if mac_match:
                            return mac_match.group(0)
        except Exception as e:
            self._log(f"获取MAC地址失败 ({ip}): {e}")
        return "未知MAC"

    def _get_hardware_ids(self, ip: str) -> dict:
        """获取客户端的多个硬件标识（通过ARP缓存和系统信息）"""
        hardware = {
            'mac': '未知',
            'cpu': '未知',
            'disk': '未知',
            'motherboard': '未知',
            'bios_uuid': '未知'
        }

        try:
            mac = self._get_client_mac(ip)
            if mac:
                hardware['mac'] = mac
        except Exception as e:
            self._log(f"获取硬件标识失败: {e}")

        return hardware

    @staticmethod
    def _get_hardware_fingerprint(mac: str = "未知MAC", device_id: str = "") -> str:
        """服务端兜底生成硬件指纹。

        注意：客户端上报的 hardware_fingerprint 才是主要依据（由 CPU/硬盘/
        主板/MAC/BIOS UUID 组合而成）。本方法仅在客户端未上报时兜底，
        且必须与黑名单界面「从在线用户添加」使用的是同一套算法，
        否则同一台设备算出的指纹不一致会导致封禁失效。
        """
        if mac and device_id and mac != "未知MAC":
            raw = f"{mac}:{device_id}"
        else:
            raw = mac or "未知MAC"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _load_server_hidden(self) -> None:
        """加载服务器隐藏状态"""
        try:
            if os.path.exists(SERVER_HIDDEN_FILE):
                with open(SERVER_HIDDEN_FILE, 'r', encoding='utf-8') as f:
                    hidden = f.read().strip() == '1'
                    self.server_hidden.set(hidden)
                    self._server_hidden_flag = hidden
        except (OSError, IOError) as e:
            print(f"加载服务器隐藏状态失败: {e}")

    def _save_server_hidden(self) -> None:
        """保存服务器隐藏状态"""
        try:
            with open(SERVER_HIDDEN_FILE, 'w', encoding='utf-8') as f:
                f.write('1' if self._server_hidden_flag else '0')
        except (OSError, IOError) as e:
            self._log(f"保存服务器隐藏状态失败: {e}")

    def _is_server_hidden(self) -> bool:
        """线程安全的隐藏状态读取（供后台线程使用）"""
        return self._server_hidden_flag

    def _load_blacklist(self) -> None:
        """加载IP黑名单"""
        try:
            if os.path.exists(BLACKLIST_FILE):
                with self.blacklist_lock:
                    with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
                        self.blacklist.clear()
                        for line in f:
                            ip = line.strip()
                            if ip:
                                self.blacklist.add(ip)
                if self.blacklist:
                    self._log(f"已加载黑名单 {len(self.blacklist)} 个IP")
        except (OSError, IOError) as e:
            self._log(f"加载黑名单失败: {e}")

    def _save_blacklist(self) -> None:
        """保存IP黑名单"""
        try:
            with self.blacklist_lock:
                with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
                    for ip in sorted(self.blacklist):
                        f.write(ip + '\n')
        except (OSError, IOError) as e:
            self._log(f"保存黑名单失败: {e}")

    def _load_device_blacklist(self) -> None:
        """加载设备黑名单"""
        try:
            if os.path.exists(DEVICE_BLACKLIST_FILE):
                with self.blacklist_lock:
                    with open(DEVICE_BLACKLIST_FILE, 'r', encoding='utf-8') as f:
                        self.device_blacklist.clear()
                        for line in f:
                            line = line.strip()
                            if line:
                                parts = line.split('|', 1)
                                if len(parts) == 2:
                                    self.device_blacklist[parts[0]] = parts[1]
                                elif len(parts) == 1:
                                    self.device_blacklist[parts[0]] = "违规用户"
                self._log(f"已加载设备黑名单 {len(self.device_blacklist)} 个设备")
        except (OSError, IOError) as e:
            self._log(f"加载设备黑名单失败: {e}")

    def _save_device_blacklist(self) -> None:
        """保存设备黑名单"""
        try:
            with self.blacklist_lock:
                with open(DEVICE_BLACKLIST_FILE, 'w', encoding='utf-8') as f:
                    for device_id, reason in self.device_blacklist.items():
                        f.write(f"{device_id}|{reason}\n")
        except (OSError, IOError) as e:
            self._log(f"保存设备黑名单失败: {e}")

    def _load_hardware_blacklist(self) -> None:
        """加载硬件黑名单"""
        try:
            if os.path.exists(HARDWARE_BLACKLIST_FILE):
                with self.hardware_blacklist_lock:
                    with open(HARDWARE_BLACKLIST_FILE, 'r', encoding='utf-8') as f:
                        self.hardware_blacklist.clear()
                        for line in f:
                            fingerprint = line.strip()
                            if fingerprint:
                                self.hardware_blacklist.add(fingerprint)
                    self._log(f"已加载硬件黑名单 {len(self.hardware_blacklist)} 个设备")
        except Exception as e:
            self._log(f"加载硬件黑名单失败: {e}")

    def _save_hardware_blacklist(self) -> None:
        """保存硬件黑名单"""
        try:
            with self.hardware_blacklist_lock:
                with open(HARDWARE_BLACKLIST_FILE, 'w', encoding='utf-8') as f:
                    for fingerprint in sorted(self.hardware_blacklist):
                        f.write(fingerprint + '\n')
        except Exception as e:
            self._log(f"保存硬件黑名单失败: {e}")

    def _is_hardware_blacklisted(self, fingerprint: str) -> bool:
        """检查硬件指纹是否在黑名单中"""
        with self.hardware_blacklist_lock:
            return fingerprint in self.hardware_blacklist

    def _add_hardware_to_blacklist(self, fingerprint: str, reason: str = "") -> bool:
        """添加硬件指纹到黑名单"""
        with self.hardware_blacklist_lock:
            if fingerprint and fingerprint not in self.hardware_blacklist:
                self.hardware_blacklist.add(fingerprint)
                self._save_hardware_blacklist()
                self._log(f"已添加硬件到黑名单: {fingerprint[:12]}... (原因: {reason or '违规用户'})")
                return True
        return False

    def _remove_hardware_from_blacklist(self, fingerprint: str) -> bool:
        """从硬件黑名单移除"""
        with self.hardware_blacklist_lock:
            if fingerprint in self.hardware_blacklist:
                self.hardware_blacklist.discard(fingerprint)
                self._save_hardware_blacklist()
                self._log(f"已从硬件黑名单移除: {fingerprint[:12]}...")
                return True
        return False

    def _load_mute_list(self) -> None:
        """加载禁言列表"""
        try:
            if os.path.exists(MUTE_LIST_FILE):
                with self.mute_lock:
                    with open(MUTE_LIST_FILE, 'r', encoding='utf-8') as f:
                        self.muted_users.clear()
                        for line in f:
                            line = line.strip()
                            if line:
                                parts = line.split('|')
                                if len(parts) == 2:
                                    username = parts[0]
                                    unmute_time = float(parts[1])
                                    if unmute_time > time.time():
                                        self.muted_users[username] = unmute_time
                if self.muted_users:
                    self._log(f"已加载禁言列表 {len(self.muted_users)} 个用户")
        except (OSError, IOError, ValueError) as e:
            self._log(f"加载禁言列表失败: {e}")

    def _save_mute_list(self) -> None:
        """保存禁言列表"""
        try:
            with self.mute_lock:
                with open(MUTE_LIST_FILE, 'w', encoding='utf-8') as f:
                    for username, unmute_time in self.muted_users.items():
                        f.write(f"{username}|{unmute_time}\n")
        except (OSError, IOError) as e:
            self._log(f"保存禁言列表失败: {e}")

    def _is_blacklisted(self, ip: str, device_id: str = "", mac: str = "") -> tuple:
        """检查IP、设备或MAC是否在黑名单中"""
        with self.blacklist_lock:
            if ip in self.blacklist:
                return True, "IP黑名单"
            if mac and mac in self.device_blacklist:
                return True, f"MAC黑名单: {self.device_blacklist[mac]}"
            if device_id and device_id in self.device_blacklist:
                return True, f"设备黑名单: {self.device_blacklist[device_id]}"
        return False, ""

    def _add_to_blacklist(self, ip: str = "", device_id: str = "", mac: str = "", reason: str = "") -> bool:
        """添加到黑名单（支持IP、设备ID、MAC）"""
        added = False
        with self.blacklist_lock:
            if ip and ip not in self.blacklist:
                self.blacklist.add(ip)
                added = True
                self._log(f"已添加IP到黑名单: {ip}")
            if mac and mac not in self.device_blacklist:
                self.device_blacklist[mac] = reason or "违规用户(MAC)"
                added = True
                self._log(f"已添加MAC到黑名单: {mac} (原因: {reason or '违规用户'})")
            if device_id and device_id not in self.device_blacklist:
                self.device_blacklist[device_id] = reason or "违规用户(设备)"
                added = True
                self._log(f"已添加设备到黑名单: {device_id} (原因: {reason or '违规用户'})")

        if added:
            if ip:
                self._save_blacklist()
            if mac or device_id:
                self._save_device_blacklist()
        return added

    def _remove_from_blacklist(self, ip: str = "", device_id: str = "", mac: str = "") -> bool:
        """从黑名单移除（支持IP、设备ID、MAC）"""
        removed = False
        with self.blacklist_lock:
            if ip and ip in self.blacklist:
                self.blacklist.discard(ip)
                removed = True
                self._log(f"已从IP黑名单移除: {ip}")
            if mac and mac in self.device_blacklist:
                del self.device_blacklist[mac]
                removed = True
                self._log(f"已从MAC黑名单移除: {mac}")
            if device_id and device_id in self.device_blacklist:
                del self.device_blacklist[device_id]
                removed = True
                self._log(f"已从设备黑名单移除: {device_id}")

        if removed:
            if ip:
                self._save_blacklist()
            if mac or device_id:
                self._save_device_blacklist()
        return removed

    def _is_muted(self, username: str) -> tuple:
        """检查用户是否被禁言"""
        with self.mute_lock:
            if username in self.muted_users:
                if time.time() < self.muted_users[username]:
                    return True, self.muted_users[username]
                del self.muted_users[username]
                self._save_mute_list()
        return False, 0.0

    def _mute_user(self, username: str, duration_minutes: int) -> None:
        """禁言用户"""
        unmute_time = time.time() + duration_minutes * 60
        with self.mute_lock:
            self.muted_users[username] = unmute_time
        self._save_mute_list()
        self._log(f"用户 {username} 被禁言 {duration_minutes} 分钟")
        try:
            self.root.after(0, self._update_muted_list_display)
        except Exception:
            pass

        with self.clients_lock:
            if username in self.clients:
                try:
                    self._send_msg(self.clients[username]['sock'], {
                        'type': 'muted',
                        'duration': duration_minutes,
                        'reason': f'您已被管理员禁言 {duration_minutes} 分钟'
                    })
                except OSError:
                    pass

    def _unmute_user(self, username: str) -> None:
        """解除禁言"""
        removed = False
        with self.mute_lock:
            if username in self.muted_users:
                del self.muted_users[username]
                removed = True
        if removed:
            self._save_mute_list()
            self._log(f"用户 {username} 已解除禁言")
            try:
                self.root.after(0, self._update_muted_list_display)
            except Exception:
                pass

            with self.clients_lock:
                if username in self.clients:
                    try:
                        self._send_msg(self.clients[username]['sock'], {
                            'type': 'unmuted',
                            'reason': '您已被管理员解除禁言'
                        })
                    except OSError:
                        pass

    def _mute_check(self) -> None:
        """定期检查禁言状态"""
        while self.running:
            time.sleep(30)
            expired = []
            with self.mute_lock:
                for u, t in list(self.muted_users.items()):
                    if time.time() >= t:
                        expired.append(u)
                for u in expired:
                    if u in self.muted_users:
                        del self.muted_users[u]
                        self._log(f"用户 {u} 禁言已自动解除")
            if expired:
                self._save_mute_list()
                # 通过主线程更新 GUI，避免后台线程直接操作 tkinter
                try:
                    self.root.after(0, self._update_muted_list_display)
                except Exception:
                    pass

    @staticmethod
    def _generate_device_id(addr: tuple) -> str:
        """生成设备唯一标识"""
        raw = f"{addr[0]}:{addr[1]}:{time.time()}:{uuid.uuid4()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    # ============ 服务器启动和网络方法 ============

    def _try_start_server(self) -> bool:
        """尝试启动服务器，失败时自动回退到 IPv4"""
        if self._start_server():
            return True
        if self.ip_mode.get() == "dual":
            self._log("⚠️ 双栈模式启动失败，尝试回退到 IPv4...")
            self.ip_mode.set("ipv4")
            if self._start_server():
                return True
        if self.ip_mode.get() == "ipv6":
            self._log("⚠️ IPv6 模式启动失败，尝试回退到 IPv4...")
            self.ip_mode.set("ipv4")
            if self._start_server():
                return True
        self._log("❌ 服务器启动彻底失败，请检查端口是否被占用")
        messagebox.showerror("启动错误", f"无法绑定端口 {PORT}，请检查端口是否被其他程序占用。")
        return False

    def _start_server(self) -> bool:
        """启动服务器，返回是否成功"""
        mode = self.ip_mode.get()
        try:
            if mode == "ipv4":
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                host = '0.0.0.0'
                mode_label = "IPv4"
            elif mode == "ipv6":
                self.sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                host = '::'
                try:
                    self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                except (AttributeError, OSError):
                    pass
                mode_label = "IPv6"
            else:  # dual
                self.sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                host = '::'
                mode_label = "IPv4+IPv6"
                try:
                    self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except (AttributeError, OSError):
                    self.sock.close()
                    self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    host = '0.0.0.0'
                    mode_label = "IPv4 (回退)"
                    self.ip_mode.set("ipv4")

            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.settimeout(1.0)
            self.sock.bind((host, PORT))
            self.sock.listen(50)
            self.running = True

            hidden_status = "隐藏模式" if self.server_hidden.get() else "公开模式"
            self._log(f"✅ 服务器启动成功 ({mode_label} - {hidden_status})，监听端口 {PORT}")
            self._update_status(f"服务器运行中 - 端口 {PORT} ({mode_label} - {hidden_status})")
            self._update_addr_display()
            return True
        except OSError as e:
            self._log(f"❌ 服务器启动失败: {e}")
            if self.sock:
                try:
                    self.sock.close()
                except:
                    pass
                self.sock = None
            return False

    def _update_addr_display(self) -> None:
        """更新地址显示"""
        ips = self._get_local_ips()
        lines = []
        if self.server_hidden.get():
            lines.append("⚠️ 服务器处于隐藏模式")
        mode = self.ip_mode.get()
        if mode in ("dual", "ipv4"):
            for ip in ips['ipv4']:
                lines.append(f"IPv4: {ip}:{PORT}")
        if mode in ("dual", "ipv6"):
            for ip in ips['ipv6']:
                lines.append(f"IPv6: [{ip}]:{PORT}")
        if not lines or (len(lines) == 1 and lines[0].startswith("⚠️")):
            lines.append("未能获取本机IP地址")
            lines.append(f"请使用 127.0.0.1:{PORT} (本机)")

        self.addr_text.config(state='normal')
        self.addr_text.delete(1.0, tk.END)
        self.addr_text.insert(tk.END, '\n'.join(lines))
        self.addr_text.config(state='disabled')

    def _send_msg(self, sock: socket.socket, msg_dict: dict) -> bool:
        """发送消息（线程安全：同一 socket 串行发送）"""
        # 查找该 socket 对应的发送锁
        send_lock = self._sock_locks.get(id(sock))
        try:
            data = json.dumps(msg_dict, ensure_ascii=False).encode('utf-8')
            if len(data) > MAX_BODY_SIZE:
                return False
            header = struct.pack('>I', len(data))
            encoded = header + data
            if send_lock:
                with send_lock:
                    sock.sendall(encoded)
            else:
                sock.sendall(encoded)
            return True
        except (OSError, BrokenPipeError, ConnectionResetError):
            return False

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
        """精确接收指定字节数

        同时使用「整体截止时间」与「连续超时次数」两个条件：
        慢速连接（持续有零星数据）不会因反复重置计数而永久挂住，
        完全静默的死连接也能被及时判定失效。
        """
        if n <= 0:
            return b''
        buf = bytearray()
        timeout_count = 0
        deadline = time.monotonic() + n / 1024 + SOCKET_RECV_TIMEOUT
        while len(buf) < n:
            if time.monotonic() > deadline:
                return None
            try:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
                timeout_count = 0
            except socket.timeout:
                timeout_count += 1
                if timeout_count > SOCKET_MAX_TIMEOUTS:
                    return None
            except (OSError, ConnectionResetError):
                return None
        return bytes(buf)

    @staticmethod
    def _recv_msg(sock: socket.socket) -> dict | None:
        """接收完整消息"""
        try:
            raw_len = Server._recv_exact(sock, 4)
            if not raw_len:
                return None
            body_len = struct.unpack('>I', raw_len)[0]
            # 下限校验：拒绝 0 长度与非 dict 的畸形报文
            if body_len <= 0 or body_len > MAX_BODY_SIZE:
                return None
            body = Server._recv_exact(sock, body_len)
            if body is None:
                return None
            parsed = json.loads(body.decode('utf-8'))
            # 后续处理全部按 dict 访问，非 dict 直接丢弃
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _accept_clients(self) -> None:
        """接受客户端连接"""
        while self.running and self.sock:
            try:
                client_sock, addr = self.sock.accept()
                client_sock.settimeout(SOCKET_RECV_TIMEOUT)

                # 并发连接数限流，避免连接耗尽攻击与线程爆炸
                with self.clients_lock:
                    at_capacity = len(self.clients) >= MAX_CONNECTIONS
                if at_capacity:
                    self._send_msg(client_sock, {
                        'type': 'error',
                        'reason': f'服务器连接数已满（上限 {MAX_CONNECTIONS}）'
                    })
                    try:
                        client_sock.close()
                    except OSError:
                        pass
                    continue

                threading.Thread(target=self._handle_client, args=(client_sock, addr), daemon=True).start()
            except socket.timeout:
                continue
            except OSError as e:
                if self.running:
                    self._log(f"接受连接异常: {e}")
                break

    def _handle_client(self, client_sock: socket.socket, addr: tuple) -> None:
        """处理客户端连接（增强版防重名）"""
        username = None
        client_ip = addr[0]
        client_sock_fileno = client_sock.fileno()  # 用于唯一标识此连接

        if client_ip.startswith('::ffff:'):
            client_ip = client_ip[7:]

        # 获取客户端MAC地址
        client_mac = self._get_client_mac(client_ip)

        try:
            # 登录握手阶段使用更宽松的超时，给慢速客户端机会
            try:
                client_sock.settimeout(LOGIN_TIMEOUT)
            except OSError:
                pass

            login_data = self._recv_msg(client_sock)
            if not login_data or login_data.get('type') != 'login':
                client_sock.close()
                return

            # 登录完成后恢复常规收发超时
            try:
                client_sock.settimeout(SOCKET_RECV_TIMEOUT)
            except OSError:
                pass

            tmp_username = login_data.get('username', '')
            if isinstance(tmp_username, str):
                tmp_username = tmp_username.strip()
            else:
                tmp_username = ''
            client_device_id = login_data.get('device_id', '')
            if not isinstance(client_device_id, str):
                client_device_id = ''
            client_hw_fingerprint = login_data.get('hardware_fingerprint', '')
            if not isinstance(client_hw_fingerprint, str):
                client_hw_fingerprint = ''

            if login_data.get('mac'):
                client_mac = login_data.get('mac')

            # ===== 硬件黑名单检查 =====
            if client_hw_fingerprint and self._is_hardware_blacklisted(client_hw_fingerprint):
                self._log(f"❌ 硬件黑名单拒绝连接: {tmp_username} (指纹:{client_hw_fingerprint[:12]}...)")
                self._send_msg(client_sock, {'type': 'error', 'reason': '您的设备已被禁止连接服务器'})
                client_sock.close()
                return

            # ===== 隐藏模式检查 =====
            if self._is_server_hidden():
                self._log(f"🔒 隐藏模式拒绝新连接: {tmp_username} (IP:{client_ip}, MAC:{client_mac})")
                self._send_msg(client_sock, {'type': 'error', 'reason': '服务器处于隐藏模式，不接受新连接'})
                client_sock.close()
                return

            # ===== 基本验证 =====
            if not tmp_username:
                self._send_msg(client_sock, {'type': 'error', 'reason': '用户名不能为空'})
                client_sock.close()
                return
            if len(tmp_username) > MAX_USERNAME_LENGTH:
                self._send_msg(client_sock,
                               {'type': 'error', 'reason': f'用户名长度不能超过{MAX_USERNAME_LENGTH}个字符'})
                client_sock.close()
                return
            # 非法字符检查：分隔符 / 控制字符（含 '(' ')'，否则会破坏客户端显示名解析）
            if any(c in tmp_username for c in ['\n', '\r', '\t', '|', '/', '\\', ':', '(', ')']):
                self._send_msg(client_sock, {'type': 'error', 'reason': '用户名包含非法字符'})
                client_sock.close()
                return
            if any(ord(c) < 32 or ord(c) == 127 for c in tmp_username):
                self._send_msg(client_sock, {'type': 'error', 'reason': '用户名包含控制字符'})
                client_sock.close()
                return

            # ===== 防重名检查（核心：使用锁和双重验证） =====
            with self.clients_lock:
                # 检查同名用户是否在线
                if tmp_username in self.clients:
                    # 额外检查：该用户是否真的还连接着
                    existing_sock = self.clients[tmp_username].get('sock')
                    if existing_sock:
                        try:
                            # 尝试发送一个ping检查连接是否存活
                            self._send_msg(existing_sock, {'type': 'ping'})
                            # 如果发送成功，说明用户真的在线
                            self._log(f"❌ 重复用户名拒绝连接: {tmp_username} (IP:{client_ip}) - 该用户名已在线")
                            self._send_msg(client_sock, {
                                'type': 'error',
                                'reason': f'用户名 "{tmp_username}" 已在其他设备登录，请更换用户名'
                            })
                            client_sock.close()
                            return
                        except:
                            # 发送失败，说明连接已断开，清理该条目
                            self._log(f"⚠️ 清理僵尸连接: {tmp_username}")
                            try:
                                existing_sock.close()
                            except:
                                pass
                            del self.clients[tmp_username]
                            # 继续执行，允许新用户登录

            # ===== 检查黑名单（IP、设备、MAC） =====
            is_blacklisted, reason = self._is_blacklisted(client_ip, client_device_id, client_mac)
            if is_blacklisted:
                self._log(f"❌ 黑名单拒绝连接: {tmp_username} (IP:{client_ip}, MAC:{client_mac}) - {reason}")
                self._send_msg(client_sock, {'type': 'error', 'reason': f'您已被服务器列入黑名单: {reason}'})
                client_sock.close()
                return

            # ===== 所有检查通过，正式注册用户 =====
            username = tmp_username

            if not client_device_id:
                client_device_id = self._generate_device_id(addr)

            with self.clients_lock:
                # 再次确认用户名未被占用（双重检查）
                if username in self.clients:
                    self._log(f"⚠️ 并发冲突: {username} 已被占用，拒绝连接")
                    self._send_msg(client_sock, {'type': 'error', 'reason': f'用户名 "{username}" 已被占用，请稍后重试'})
                    client_sock.close()
                    return

                self.clients[username] = {
                    'sock': client_sock,
                    'send_lock': threading.Lock(),  # 每个客户端 socket 独立发送锁
                    'addr': addr,
                    'last_heartbeat': time.time(),
                    'device_id': client_device_id,
                    'ip': client_ip,
                    'mac': client_mac,
                    'hardware_fingerprint': client_hw_fingerprint,
                    'fileno': client_sock_fileno  # 存储文件描述符用于验证
                }
                self._sock_locks[id(client_sock)] = self.clients[username]['send_lock']

            self._send_msg(client_sock, {
                'type': 'login_success',
                'device_id': client_device_id,
                'server_hidden': self._is_server_hidden()
            })

            hw_fingerprint_display = client_hw_fingerprint if client_hw_fingerprint else "无"
            self._log(
                f"✅ 用户 {username} 上线 | IP:{client_ip} | MAC:{client_mac} | 设备:{client_device_id} | 指纹:{hw_fingerprint_display}")
            self._update_user_list_gui()
            self._broadcast_system(f"🎉 用户 {username} 加入聊天室", send_user_list=True)

            # ===== 检查禁言状态 =====
            is_muted, unmute_time = self._is_muted(username)
            if is_muted:
                remaining = max(0, int((unmute_time - time.time()) // 60))
                self._send_msg(client_sock, {
                    'type': 'muted',
                    'duration': remaining,
                    'reason': f'您当前处于禁言状态，剩余 {remaining} 分钟'
                })

            # ===== 发送消息历史 =====
            self._send_message_history(client_sock)

            while self.running:
                msg = self._recv_msg(client_sock)
                if msg is None:
                    break
                with self.clients_lock:
                    if username in self.clients:
                        self.clients[username]['last_heartbeat'] = time.time()
                self._process_message(username, msg)

        except Exception as e:
            if username:
                self._log(f"用户 {username} 连接异常: {e}")
            else:
                self._log(f"连接处理异常 ({client_ip}): {e}")
        finally:
            # 清理该连接的发送锁注册（无论是否已注册都安全）
            with self.clients_lock:
                self._sock_locks.pop(id(client_sock), None)

            # ===== 安全下线：只有用户名存在时才执行删除 =====
            if username:
                # 丢弃该用户未完成的分块上传，避免临时文件与句柄泄漏
                self._abort_upload(username)
                with self.clients_lock:
                    if username in self.clients:
                        # 额外验证：确保当前socket与存储的socket匹配，防止误删新连接
                        stored_sock = self.clients[username].get('sock')
                        if stored_sock is client_sock:
                            del self.clients[username]
                            self._log(f"用户 {username} 下线")
                            self._update_user_list_gui()
                            self._broadcast_system(f"用户 {username} 离开聊天室", send_user_list=True)
                        else:
                            # 不匹配，说明有新连接替换了，不删除
                            self._log(f"⚠️ 跳过删除 {username}：socket不匹配（可能是新连接）")
            # 关闭socket
            try:
                client_sock.close()
            except OSError:
                pass


    def _send_message_history(self, client_sock: socket.socket) -> None:
        """发送消息历史（deque 自动限长，直接取尾部切片）"""
        with self.message_history_lock:
            history = list(self.message_history)[-HISTORY_SEND_COUNT:]
        if history:
            self._send_msg(client_sock, {
                'type': 'message_history',
                'messages': history
            })

    def _process_message(self, sender: str, msg: dict) -> None:
        """处理消息"""
        mtype = msg.get('type')

        if mtype == 'heartbeat':
            with self.clients_lock:
                sock = self.clients[sender]['sock'] if sender in self.clients else None
            if sock:
                self._send_msg(sock, {'type': 'heartbeat_ack'})

        elif mtype == 'public_message':
            is_muted, unmute_time = self._is_muted(sender)
            if is_muted:
                with self.clients_lock:
                    sock = self.clients[sender]['sock'] if sender in self.clients else None
                if sock:
                    remaining = max(0, int((unmute_time - time.time()) // 60))
                    self._send_msg(sock, {
                        'type': 'error',
                        'reason': f'您当前处于禁言状态，剩余 {remaining} 分钟'
                    })
                return

            content = msg.get('content', '')
            if not isinstance(content, str):
                return
            if len(content) > MAX_MESSAGE_LENGTH:
                with self.clients_lock:
                    sock = self.clients[sender]['sock'] if sender in self.clients else None
                if sock:
                    self._send_msg(sock, {
                        'type': 'error',
                        'reason': f'消息过长（{len(content)} 字符），已限制为 {MAX_MESSAGE_LENGTH} 字符'
                    })
                return
            # 管理员身份由服务端判定，禁止客户端伪造
            is_admin = (sender == ADMIN_NAME)

            with self.message_history_lock:
                self.message_history.append({
                    'type': 'public',
                    'from': sender,
                    'content': content,
                    'is_admin': is_admin,
                    'time': time.time()
                })

            self._log_message('public', f"[群聊] {sender}: {content}")
            self._broadcast_public(sender, content, is_admin=is_admin)

        elif mtype == 'private_message':
            is_muted, unmute_time = self._is_muted(sender)
            if is_muted:
                with self.clients_lock:
                    sock = self.clients[sender]['sock'] if sender in self.clients else None
                if sock:
                    remaining = max(0, int((unmute_time - time.time()) // 60))
                    self._send_msg(sock, {
                        'type': 'error',
                        'reason': f'您当前处于禁言状态，剩余 {remaining} 分钟'
                    })
                return

            target = msg.get('to')
            content = msg.get('content', '')

            # 参数校验：目标必须是非空字符串且不能是自己
            if not isinstance(target, str) or not target or target == sender:
                with self.clients_lock:
                    sock = self.clients[sender]['sock'] if sender in self.clients else None
                if sock:
                    self._send_msg(sock, {'type': 'error', 'reason': '私聊目标无效'})
                return
            if not isinstance(content, str) or len(content) > MAX_MESSAGE_LENGTH:
                with self.clients_lock:
                    sock = self.clients[sender]['sock'] if sender in self.clients else None
                if sock:
                    self._send_msg(sock, {
                        'type': 'error',
                        'reason': f'消息过长（上限 {MAX_MESSAGE_LENGTH} 字符）'
                    })
                return

            self._save_private_message(sender, target, content)
            self._log_message('private', f"[私聊] {sender} -> {target}: {content}")

            with self.clients_lock:
                target_sock = self.clients[target]['sock'] if target in self.clients else None
                sender_sock = self.clients[sender]['sock'] if sender in self.clients else None

            if target_sock:
                self._send_msg(target_sock, {
                    'type': 'private_message', 'from': sender, 'content': content
                })
            else:
                self._log(f"私聊失败：{sender} -> {target}（目标不在线）")
            if sender_sock:
                if target_sock:
                    self._send_msg(sender_sock, {
                        'type': 'system_message',
                        'content': f"💬 私聊消息已发送给 {target}"
                    })
                else:
                    self._send_msg(sender_sock, {
                        'type': 'error', 'reason': f'用户 {target} 不在线'
                    })

        elif mtype == 'file_request':
            target = msg.get('to')
            with self.clients_lock:
                target_sock = self.clients[target]['sock'] if target in self.clients else None
            if target_sock:
                self._send_msg(target_sock, {
                    'type': 'file_request', 'from': sender,
                    'filename': msg['filename'], 'size': msg['size'],
                    'transfer_id': msg['transfer_id']
                })

        elif mtype in ('file_response', 'file_chunk', 'file_end', 'file_error'):
            target = msg.get('to')
            if not isinstance(target, str) or not target:
                return
            with self.clients_lock:
                target_sock = self.clients[target]['sock'] if target in self.clients else None
            if target_sock:
                self._send_msg(target_sock, msg)

        elif mtype == 'shared_file_list':
            self._send_shared_file_list(sender)

        elif mtype == 'download_shared_file':
            fname = msg.get('filename', '')
            if not isinstance(fname, str):
                return
            # 每个下载请求一个线程池任务，而非无限创建裸线程
            self._file_workers.submit(self._send_shared_file, sender, fname)

        elif mtype == 'upload_shared_file_start':
            self._upload_start(sender, msg)

        elif mtype == 'upload_shared_file_chunk':
            self._upload_chunk(sender, msg)

        elif mtype == 'upload_shared_file_end':
            self._upload_end(sender, msg)

        elif mtype == 'upload_shared_file':
            # 兼容旧客户端：整文件 base64 上传
            fname = msg.get('filename', '')
            data = msg.get('data', '')
            if not isinstance(fname, str) or not isinstance(data, str):
                return
            self._file_workers.submit(self._receive_shared_file, sender, fname, data)

        else:
            self._log(f"未知消息[{mtype}] 来自 {sender}")

    # ============ 共享文件相关 ============

    @staticmethod
    def _list_shared_files() -> list:
        """列出正式共享文件（排除上传中的 .part 临时文件与隐藏文件）"""
        if not os.path.exists(SHARED_FILE_DIR):
            return []
        try:
            return [f for f in os.listdir(SHARED_FILE_DIR)
                    if not f.startswith('.') and not f.endswith('.part')]
        except OSError:
            return []

    def _send_shared_file_list(self, sender: str) -> None:
        """发送共享文件列表（附带大小）"""
        files = []
        if os.path.exists(SHARED_FILE_DIR):
            for fname in self._list_shared_files():
                fpath = os.path.join(SHARED_FILE_DIR, fname)
                size = os.path.getsize(fpath) if os.path.isfile(fpath) else 0
                files.append({'name': fname, 'size': size})
        with self.clients_lock:
            sender_sock = self.clients[sender]['sock'] if sender in self.clients else None
        if sender_sock:
            self._send_msg(sender_sock, {'type': 'shared_file_list', 'files': files})

    def _get_client_sock(self, username: str):
        """获取指定用户的 socket（无则返回 None）"""
        with self.clients_lock:
            info = self.clients.get(username)
            return info['sock'] if info else None

    def _send_shared_file(self, sender: str, fname: str) -> None:
        """分块流式发送共享文件（不再整文件读入内存）"""
        fname = os.path.basename(fname)
        file_path = os.path.join(SHARED_FILE_DIR, fname)

        if not os.path.isfile(file_path):
            sock = self._get_client_sock(sender)
            if sock:
                self._send_msg(sock, {'type': 'error', 'reason': f'文件 {fname} 不存在'})
            return

        try:
            total_size = os.path.getsize(file_path)
            total_chunks = (total_size + FILE_CHUNK_SIZE - 1) // FILE_CHUNK_SIZE
            sock = self._get_client_sock(sender)
            if not sock:
                return

            # 第一遍：计算 SHA-256，供客户端校验内容完整性
            digest = hashlib.sha256()
            with open(file_path, 'rb') as f:
                while True:
                    part = f.read(FILE_CHUNK_SIZE)
                    if not part:
                        break
                    digest.update(part)
            file_hash = digest.hexdigest()

            if not self._send_msg(sock, {
                'type': 'shared_file_start',
                'filename': fname,
                'total_size': total_size,
                'total_chunks': total_chunks,
                'sha256': file_hash
            }):
                self._log(f"发送共享文件失败(起始包): {fname}")
                return

            index = 0
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(FILE_CHUNK_SIZE)
                    if not chunk:
                        break
                    sock = self._get_client_sock(sender)
                    if not sock:
                        self._log(f"用户 {sender} 已离线，中断文件发送: {fname}")
                        return
                    if not self._send_msg(sock, {
                        'type': 'shared_file_chunk',
                        'filename': fname,
                        'index': index,
                        'data': base64.b64encode(chunk).decode('ascii')
                    }):
                        self._log(f"发送共享文件分块失败: {fname} (第 {index} 块)")
                        return
                    index += 1

            sock = self._get_client_sock(sender)
            if sock:
                self._send_msg(sock, {
                    'type': 'shared_file_end',
                    'filename': fname,
                    'total_size': total_size,
                    'total_chunks': index,
                    'sha256': file_hash
                })
            self._log(f"📥 用户 {sender} 下载共享文件: {fname} ({total_size} 字节 / {index} 块)")
        except OSError as e:
            self._log(f"发送共享文件失败: {e}")

    # ---------- 分块上传 ----------

    @staticmethod
    def _safe_filename(fname: str) -> str:
        """清理文件名，防止路径穿越与非法字符"""
        fname = os.path.basename(fname.replace('\\', '/')).strip()
        fname = ''.join(c for c in fname if c not in '\r\n\t' and ord(c) >= 32)
        return fname

    def _upload_reply(self, sender: str, reason: str) -> None:
        sock = self._get_client_sock(sender)
        if sock:
            self._send_msg(sock, {'type': 'error', 'reason': reason})

    def _upload_start(self, sender: str, msg: dict) -> None:
        """开始接收分块上传"""
        fname = self._safe_filename(msg.get('filename', ''))
        total_size = int(msg.get('total_size', 0) or 0)

        if not fname:
            self._upload_reply(sender, '文件名无效')
            return
        if total_size <= 0:
            self._upload_reply(sender, '文件大小无效')
            return
        if total_size > MAX_SHARED_FILE_SIZE:
            self._upload_reply(sender, f'文件过大（上限 {MAX_SHARED_FILE_SIZE // (1024 * 1024)}MB）')
            return

        # 同名文件覆盖时不算新增；否则检查数量上限
        existing = self._list_shared_files()
        if fname not in existing and len(existing) >= MAX_SHARED_FILES:
            self._upload_reply(sender, f'共享文件已达上限 ({MAX_SHARED_FILES}个)')
            return

        tmp_path = os.path.join(SHARED_FILE_DIR, f'.upload_{sender}_{int(time.time())}.part')
        try:
            fp = open(tmp_path, 'wb')
        except OSError as e:
            self._upload_reply(sender, f'服务器无法创建文件: {e}')
            return

        with self.uploads_lock:
            old = self.uploads.pop(sender, None)
            self.uploads[sender] = {
                'fp': fp, 'fname': fname, 'received': 0,
                'total': total_size, 'path': tmp_path,
                'hasher': hashlib.sha256()  # 增量累积，用于校验内容完整性
            }
        if old:
            try:
                old['fp'].close()
            except OSError:
                pass
            try:
                os.remove(old['path'])
            except OSError:
                pass

        sock = self._get_client_sock(sender)
        if sock:
            self._send_msg(sock, {'type': 'upload_accepted', 'filename': fname})

    def _upload_chunk(self, sender: str, msg: dict) -> None:
        """写入一个上传分块（增量累积 SHA-256）"""
        with self.uploads_lock:
            info = self.uploads.get(sender)
        if not info:
            return
        try:
            binary = base64.b64decode(msg.get('data', ''))
            info['fp'].write(binary)
            info['received'] += len(binary)
            info['hasher'].update(binary)
        except Exception as e:
            self._log(f"接收上传分块失败 ({sender}): {e}")
            self._abort_upload(sender, '接收分块失败')

    @staticmethod
    def _file_sha256(path: str, limit: int) -> str:
        """计算文件 SHA-256（最多读 limit 字节）"""
        digest = hashlib.sha256()
        remaining = limit
        with open(path, 'rb') as f:
            while remaining > 0:
                part = f.read(min(FILE_CHUNK_SIZE, remaining))
                if not part:
                    break
                digest.update(part)
                remaining -= len(part)
        return digest.hexdigest()

    def _upload_end(self, sender: str, msg: dict) -> None:
        """结束上传：校验大小与哈希后落盘"""
        with self.uploads_lock:
            info = self.uploads.pop(sender, None)
        if not info:
            return
        try:
            info['fp'].close()
        except OSError:
            pass

        fname = info['fname']
        if info['received'] != info['total']:
            self._log(f"⚠️ 上传大小不符: {fname} 收到 {info['received']} / 期望 {info['total']}")
            try:
                os.remove(info['path'])
            except OSError:
                pass
            self._upload_reply(sender, f'文件不完整（收到 {info["received"]} 字节，期望 {info["total"]} 字节）')
            return

        # 内容完整性校验：客户端声明的哈希必须与实际写入内容一致
        client_hash = msg.get('sha256', '')
        if isinstance(client_hash, str) and client_hash:
            actual = self._file_sha256(info['path'], info['received'])
            if actual != client_hash.lower():
                self._log(f"⚠️ 上传哈希不符: {fname} 期望 {client_hash[:12]} 实际 {actual[:12]}")
                try:
                    os.remove(info['path'])
                except OSError:
                    pass
                self._upload_reply(sender, '文件内容校验失败（哈希不匹配），请重试')
                return

        file_path = os.path.join(SHARED_FILE_DIR, fname)
        try:
            os.replace(info['path'], file_path)
        except OSError as e:
            self._log(f"上传落盘失败: {e}")
            try:
                os.remove(info['path'])
            except OSError:
                pass
            self._upload_reply(sender, f'保存文件失败: {e}')
            return

        self._log(f"📤 用户 {sender} 上传共享文件: {fname} ({info['received']} 字节)")
        self._broadcast_system(f"📤 {sender} 上传了新文件: {fname}")

    def _abort_upload(self, sender: str, reason: str = '') -> None:
        """中断并清理某个用户的上传会话"""
        with self.uploads_lock:
            info = self.uploads.pop(sender, None)
        if not info:
            return
        try:
            info['fp'].close()
        except OSError:
            pass
        try:
            os.remove(info['path'])
        except OSError:
            pass
        if reason:
            self._upload_reply(sender, reason)

    def _receive_shared_file(self, sender: str, fname: str, data: str) -> None:
        """接收共享文件（旧协议：整文件 base64，保留兼容）"""
        fname = self._safe_filename(fname)
        if not fname:
            self._upload_reply(sender, '文件名无效')
            return

        if len(data) > MAX_BODY_SIZE:
            with self.clients_lock:
                sender_sock = self.clients[sender]['sock'] if sender in self.clients else None
            if sender_sock:
                self._send_msg(sender_sock, {'type': 'error', 'reason': '上传文件过大'})
            return

        current_files = self._list_shared_files()
        if len(current_files) >= MAX_SHARED_FILES:
            with self.clients_lock:
                sender_sock = self.clients[sender]['sock'] if sender in self.clients else None
            if sender_sock:
                self._send_msg(sender_sock, {
                    'type': 'error',
                    'reason': f'共享文件已达上限 ({MAX_SHARED_FILES}个)'
                })
            return
        file_path = os.path.join(SHARED_FILE_DIR, fname)
        try:
            binary_data = base64.b64decode(data)
            with self.file_lock:
                with open(file_path, 'wb') as f:
                    f.write(binary_data)
            self._log(f"📤 用户 {sender} 上传共享文件: {fname}")
            self._broadcast_system(f"📤 {sender} 上传了新文件: {fname}")
        except Exception as e:
            self._log(f"接收共享文件失败: {e}")

    # ============ 广播方法 ============

    def _broadcast_public(self, sender: str, content: str, is_admin: bool = False) -> None:
        """广播公开消息"""
        data = {
            'type': 'public_message',
            'from': sender,
            'content': content,
            'is_admin': is_admin
        }
        with self.clients_lock:
            socks = list(self._client_socks())
        for sock in socks:
            self._send_msg(sock, data)

    def _client_socks(self):
        """在 clients_lock 下调用，返回所有 socket"""
        for info in self.clients.values():
            yield info['sock']

    def _broadcast_system(self, content: str, send_user_list: bool = False, is_broadcast: bool = False) -> None:
        """广播系统消息"""
        data: dict = {'type': 'system_message', 'content': content, 'is_broadcast': is_broadcast}
        with self.clients_lock:
            if send_user_list:
                data['user_list'] = list(self.clients.keys())
            socks = list(self._client_socks())
        for sock in socks:
            self._send_msg(sock, data)

    # ============ 日志和状态方法 ============

    def _log(self, msg: str) -> None:
        """添加日志"""
        try:
            self.msg_queue.put(('log', 'system', msg), block=False)
        except queue.Full:
            pass

    def _log_message(self, tag: str, msg: str) -> None:
        """添加带标签的日志"""
        try:
            self.msg_queue.put(('log', tag, msg), block=False)
        except queue.Full:
            pass

    def _update_status(self, msg: str) -> None:
        """更新状态栏"""
        try:
            self.msg_queue.put(('status', msg), block=False)
        except queue.Full:
            pass

    def _update_user_list_gui(self) -> None:
        """标记用户列表需要更新（线程安全，不会因队列满而丢失）"""
        with self._user_list_update_lock:
            self._user_list_needs_update = True

    def _do_update_user_list_gui(self) -> None:
        """实际执行用户列表GUI更新（仅从主线程调用）"""
        if not self.user_listbox:
            return
        try:
            with self.clients_lock:
                user_list = []
                for username, info in self.clients.items():
                    mac = info.get('mac', '未知MAC')
                    hw_fingerprint = info.get('hardware_fingerprint', '')
                    if hw_fingerprint:
                        display = f"{username} ({mac}) [指纹:{hw_fingerprint}]"
                    else:
                        display = f"{username} ({mac})"
                    user_list.append(display)
            self.user_listbox.delete(0, tk.END)
            for u in user_list:
                self.user_listbox.insert(tk.END, u)
        except tk.TclError:
            pass

    def _process_gui(self) -> None:
        """处理GUI消息队列（主线程）"""
        batch_count = 0
        while not self.msg_queue.empty() and batch_count < QUEUE_BATCH_SIZE:
            try:
                item = self.msg_queue.get_nowait()
                if item[0] == 'log':
                    tag = item[1]
                    text = f"{time.strftime('%H:%M:%S')} | {item[2]}"
                    if self.log_area:
                        self.log_area.config(state='normal')
                        self.log_area.insert(tk.END, text + '\n', tag)
                        line_count = int(self.log_area.index('end-1c').split('.')[0])
                        excess = line_count - MAX_LOG_LINES
                        if excess > 0:
                            # 一次性多清理一批，避免每次插入都裁剪
                            self.log_area.delete('1.0', f'{excess + 100}.0')
                        self.log_area.see(tk.END)
                        self.log_area.config(state='disabled')
                elif item[0] == 'status':
                    if self.status_bar:
                        self.status_bar.config(text=item[1])
                batch_count += 1
            except queue.Empty:
                break
            except tk.TclError:
                break
            except Exception as e:
                # 记录异常但继续保持GUI更新循环
                print(f"GUI更新异常: {e}")
                traceback.print_exc()
                continue

        # ===== 修复：无论队列状态如何，始终检查并更新用户列表（脏标志模式） =====
        with self._user_list_update_lock:
            needs_update = self._user_list_needs_update
            self._user_list_needs_update = False

        if needs_update:
            self._do_update_user_list_gui()

        self.root.after(GUI_UPDATE_INTERVAL, self._process_gui)

    # ============ 私聊记录方法 ============

    def _save_private_message(self, from_user: str, to_user: str, content: str) -> None:
        """保存私聊记录（超过上限自动轮转，避免磁盘无限增长）"""
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_entry = f"[{timestamp}] {from_user} -> {to_user}: {content}\n"
            with self.file_lock:
                # 追加前检查体积，超限则把旧文件重命名为带时间戳的历史文件
                if os.path.exists(PRIVATE_LOG_FILE):
                    try:
                        if os.path.getsize(PRIVATE_LOG_FILE) > MAX_PRIVATE_LOG_BYTES:
                            backup = f"{PRIVATE_LOG_FILE}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
                            os.replace(PRIVATE_LOG_FILE, backup)
                    except OSError:
                        pass  # 轮转失败也要继续记录，不能因为它丢掉聊天记录
                with open(PRIVATE_LOG_FILE, 'a', encoding='utf-8') as f:
                    f.write(log_entry)
        except OSError as e:
            self._log(f"保存私聊记录失败: {e}")

    def _view_private_logs(self) -> None:
        """查看私聊记录"""
        if not os.path.exists(PRIVATE_LOG_FILE):
            messagebox.showinfo("提示", "暂无私聊记录")
            return
        try:
            with self.file_lock:
                with open(PRIVATE_LOG_FILE, 'r', encoding='utf-8') as f:
                    content = f.read()
            log_window = tk.Toplevel(self.root)
            log_window.title("私聊记录")
            log_window.geometry("600x400")
            text_area = scrolledtext.ScrolledText(log_window, wrap=tk.WORD, font=("Microsoft YaHei", 10))
            text_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
            text_area.insert(tk.END, content)
            text_area.config(state='disabled')
            btn_frame = tk.Frame(log_window)
            btn_frame.pack(pady=10)
            tk.Button(btn_frame, text="🔄 刷新", command=lambda: self._refresh_log_view(text_area),
                      font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=5)
            tk.Button(btn_frame, text="清空记录", command=lambda: self._clear_private_logs(log_window),
                      font=("Microsoft YaHei", 9), bg="#FF5722", fg="white").pack(side=tk.LEFT, padx=5)
            tk.Button(btn_frame, text="关闭", command=log_window.destroy,
                      font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=5)
        except OSError as e:
            messagebox.showerror("错误", f"读取记录失败: {e}")

    def _refresh_log_view(self, text_area: scrolledtext.ScrolledText) -> None:
        """刷新日志视图"""
        try:
            with self.file_lock:
                with open(PRIVATE_LOG_FILE, 'r', encoding='utf-8') as f:
                    content = f.read()
            text_area.config(state='normal')
            text_area.delete(1.0, tk.END)
            text_area.insert(tk.END, content)
            text_area.config(state='disabled')
        except OSError as e:
            messagebox.showerror("错误", f"刷新失败: {e}")

    def _clear_private_logs(self, window: tk.Toplevel) -> None:
        """清空私聊记录"""
        if messagebox.askyesno("确认", "确定要清空所有私聊记录吗？"):
            try:
                with self.file_lock:
                    open(PRIVATE_LOG_FILE, 'w', encoding='utf-8').close()
                window.destroy()
                self._log("已清空所有私聊记录")
            except OSError as e:
                messagebox.showerror("错误", f"清空失败: {e}")

    # ============ 共享文件管理 ============

    def _manage_shared_files(self) -> None:
        """管理共享文件"""
        files = self._list_shared_files()
        file_window = tk.Toplevel(self.root)
        file_window.title("共享文件管理")
        file_window.geometry("500x400")

        list_frame = tk.Frame(file_window)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        tk.Label(list_frame, text=f"📁 共享文件列表 ({len(files)}/{MAX_SHARED_FILES})",
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor='w')

        file_listbox = tk.Listbox(list_frame, font=("Microsoft YaHei", 10))
        file_listbox.pack(fill=tk.BOTH, expand=True, pady=5)
        for f in files:
            file_listbox.insert(tk.END, f)

        btn_frame = tk.Frame(file_window)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="📤 上传", command=lambda: self._upload_shared_file(file_listbox),
                  font=("Microsoft YaHei", 9), bg="#4CAF50", fg="white").pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="📥 下载", command=lambda: self._download_shared_file(file_listbox),
                  font=("Microsoft YaHei", 9), bg="#2196F3", fg="white").pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="删除", command=lambda: self._delete_shared_file(file_listbox),
                  font=("Microsoft YaHei", 9), bg="#FF5722", fg="white").pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="🔄 刷新", command=lambda: self._refresh_file_list(file_listbox),
                  font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="关闭", command=file_window.destroy,
                  font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=5)

    def _upload_shared_file(self, file_listbox: tk.Listbox) -> None:
        """上传共享文件（管理员本地直传）"""
        current_files = self._list_shared_files()
        if len(current_files) >= MAX_SHARED_FILES:
            messagebox.showwarning("提示", f"共享文件已达上限 ({MAX_SHARED_FILES}个)，请先删除一些文件")
            return
        path = filedialog.askopenfilename(title="选择要上传的文件")
        if not path:
            return
        fname = os.path.basename(path)
        dest_path = os.path.join(SHARED_FILE_DIR, fname)
        if os.path.exists(dest_path):
            if not messagebox.askyesno("文件已存在", f"文件 {fname} 已存在，是否覆盖？"):
                return
        try:
            with open(path, 'rb') as src:
                with open(dest_path, 'wb') as dst:
                    dst.write(src.read())
            self._log(f"📤 管理员上传共享文件: {fname}")
            self._refresh_file_list(file_listbox)
            messagebox.showinfo("成功", f"文件 {fname} 上传成功！")
            self._broadcast_system(f"📤 管理员上传了新文件: {fname}")
        except OSError as e:
            messagebox.showerror("错误", f"上传失败: {e}")

    def _download_shared_file(self, file_listbox: tk.Listbox) -> None:
        """下载共享文件"""
        sel = file_listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要下载的文件")
            return
        fname = file_listbox.get(sel[0])
        src_path = os.path.join(SHARED_FILE_DIR, fname)
        if not os.path.exists(src_path):
            messagebox.showerror("错误", "文件不存在")
            return
        save_path = filedialog.asksaveasfilename(title="保存文件", initialfile=fname)
        if not save_path:
            return
        try:
            with open(src_path, 'rb') as src:
                with open(save_path, 'wb') as dst:
                    dst.write(src.read())
            self._log(f"📥 管理员下载共享文件: {fname}")
            messagebox.showinfo("成功", f"文件 {fname} 下载成功！")
        except OSError as e:
            messagebox.showerror("错误", f"下载失败: {e}")

    def _delete_shared_file(self, file_listbox: tk.Listbox) -> None:
        """删除共享文件"""
        sel = file_listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要删除的文件")
            return
        fname = file_listbox.get(sel[0])
        if not messagebox.askyesno("确认删除", f"确定要删除文件 {fname} 吗？"):
            return
        file_path = os.path.join(SHARED_FILE_DIR, fname)
        try:
            os.remove(file_path)
            self._log(f"管理员删除共享文件: {fname}")
            self._refresh_file_list(file_listbox)
            messagebox.showinfo("成功", f"文件 {fname} 已删除")
            self._broadcast_system(f"管理员删除了共享文件: {fname}")
        except OSError as e:
            messagebox.showerror("错误", f"删除失败: {e}")

    @classmethod
    def _refresh_file_list(cls, file_listbox: tk.Listbox) -> None:
        """刷新文件列表"""
        file_listbox.delete(0, tk.END)
        for f in cls._list_shared_files():
            file_listbox.insert(tk.END, f)

    # ============ 心跳检测 ============

    def _heartbeat_check(self) -> None:
        """心跳检测"""
        while self.running:
            time.sleep(10)
            now = time.time()
            offline_users: list = []
            with self.clients_lock:
                for user, info in list(self.clients.items()):
                    if now - info['last_heartbeat'] > HEARTBEAT_TIMEOUT:
                        offline_users.append((user, info['sock']))
                for user, sock in offline_users:
                    if user in self.clients:
                        self._log(f"用户 {user} 心跳超时断开")
                        del self.clients[user]
                        self._sock_locks.pop(id(sock), None)

            # 关闭 socket 让阻塞中的 _handle_client 立即醒来退出
            for _, sock in offline_users:
                try:
                    sock.close()
                except OSError:
                    pass

            if offline_users:
                self._update_user_list_gui()
                names = "、".join(u for u, _ in offline_users)
                if len(offline_users) == 1:
                    msg = f"用户 {names} 长时间无响应已下线"
                else:
                    msg = f"用户 {names} 长时间无响应已下线（共 {len(offline_users)} 人）"
                # 合并为一次广播，避免 N 次全量用户列表下发
                self._broadcast_system(msg, send_user_list=True)

    # ============ UI事件处理方法 ============

    def _on_admin_input_return(self, event: tk.Event) -> str:
        """管理员输入框回车处理"""
        if event.state & 0x4:  # Ctrl+Enter
            self.admin_input.insert(tk.INSERT, '\n')
        else:
            self._send_admin_chat()
        return "break"

    def _send_admin_chat(self) -> None:
        """发送管理员聊天"""
        txt = self.admin_input.get('1.0', tk.END).strip()
        if not txt:
            return
        if not self.running:
            messagebox.showwarning("提示", "服务器未启动")
            return
        self.admin_input.delete('1.0', tk.END)

        with self.message_history_lock:
            self.message_history.append({
                'type': 'public',
                'from': ADMIN_NAME,
                'content': txt,
                'is_admin': True,
                'time': time.time()
            })
            if len(self.message_history) > MAX_MESSAGE_HISTORY:
                self.message_history.pop(0)

        self._broadcast_public(ADMIN_NAME, txt, is_admin=True)
        self._log_message('public', f"[群聊] 管理员: {txt}")

    def _toggle_server_hidden(self) -> None:
        """切换服务器隐藏状态"""
        self._server_hidden_flag = bool(self.server_hidden.get())
        if self._server_hidden_flag:
            self._log("🔒 服务器已切换到隐藏模式")
            self._log("⚠️ 新用户将无法连接，已在线用户不受影响")
            self._broadcast_system("🔒 服务器已进入隐藏模式，不再接受新连接", is_broadcast=True)
        else:
            self._log("🔓 服务器已切换到公开模式")
            self._log("✅ 新用户现在可以连接")
            self._broadcast_system("🔓 服务器已切换为公开模式，欢迎新用户加入", is_broadcast=True)

        self._save_server_hidden()
        self._update_addr_display()

    def _on_ip_mode_change(self) -> None:
        """IP模式变更"""
        if self.running:
            messagebox.showinfo("提示", "IP 模式变更将在下次启动服务器时生效。")

    def _clear_chat_history(self) -> None:
        """清空群聊记录"""
        if messagebox.askyesno("确认", "确定要清空所有群聊记录吗？\n此操作不可恢复！"):
            with self.message_history_lock:
                self.message_history.clear()
            self._broadcast_system("管理员已清空群聊记录", is_broadcast=True)
            self._log("管理员清空了所有群聊记录")

    def _mute_user_dialog(self) -> None:
        """禁言用户对话框"""
        sel = self.user_listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要禁言的用户")
            return

        username_raw = self.user_listbox.get(sel[0])
        username = username_raw.split(' (')[0] if ' (' in username_raw else username_raw

        if username == ADMIN_NAME:
            messagebox.showinfo("提示", "不能禁言管理员")
            return

        duration = simpledialog.askinteger(
            "禁言用户",
            f"请输入禁言 {username} 的时长（分钟）：\n(0表示永久禁言)",
            minvalue=0, maxvalue=43200,
            parent=self.root
        )
        if duration is None:
            return
        if duration == 0:
            self._mute_user(username, 43200)
            messagebox.showinfo("成功", f"用户 {username} 已被永久禁言")
        else:
            self._mute_user(username, duration)
            messagebox.showinfo("成功", f"用户 {username} 已被禁言 {duration} 分钟")

    def _unmute_user_dialog(self) -> None:
        """解除禁言对话框"""
        expired = []
        with self.mute_lock:
            for username, unmute_time in list(self.muted_users.items()):
                if time.time() >= unmute_time:
                    expired.append(username)
            for username in expired:
                del self.muted_users[username]
            if expired:
                self._save_mute_list()

        self._update_muted_list_display()

        if not self.muted_users:
            messagebox.showinfo("提示", "当前没有被禁言的用户")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("解除禁言")
        dialog.geometry("350x300")
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(dialog, text="选择要解除禁言的用户：", font=("Microsoft YaHei", 10)).pack(pady=10)

        listbox = tk.Listbox(dialog, font=("Microsoft YaHei", 10))
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        with self.mute_lock:
            for username in sorted(self.muted_users.keys()):
                remaining = max(0, int((self.muted_users[username] - time.time()) // 60))
                listbox.insert(tk.END, f"{username} (剩余 {remaining} 分钟)")

        def do_unmute() -> None:
            sel = listbox.curselection()
            if not sel:
                messagebox.showinfo("提示", "请选择用户", parent=dialog)
                return
            selected = listbox.get(sel[0])
            username = selected.split(' (')[0]

            self._unmute_user(username)

            dialog.destroy()
            messagebox.showinfo("成功", f"用户 {username} 已解除禁言")

        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="✅ 解除禁言", command=do_unmute,
                  bg="#4CAF50", fg="white", font=("Microsoft YaHei", 9), width=12).pack(side='left', padx=5)
        tk.Button(btn_frame, text="❌ 取消", command=dialog.destroy,
                  font=("Microsoft YaHei", 9), width=12).pack(side='left', padx=5)

    def _disconnect_locked(self, username: str, info: dict, reason: str) -> None:
        """在已持有 clients_lock 的前提下移除连接并发送踢出通知。"""
        sock = info.get('sock')
        self.clients.pop(username, None)
        if sock is not None:
            self._sock_locks.pop(id(sock), None)
            try:
                self._send_msg(sock, {'type': 'kicked', 'reason': reason})
            except Exception:
                pass
            try:
                sock.close()
            except OSError:
                pass
        # 清理未完成的上传，避免句柄与临时文件泄漏
        try:
            self._abort_upload(username)
        except Exception:
            pass

    def _kick_username(self, username: str, reason: str) -> bool:
        """踢出指定用户名的在线用户（返回是否确实踢出了）"""
        with self.clients_lock:
            info = self.clients.get(username)
            if info is None:
                return False
            self._disconnect_locked(username, info, reason)
        self._update_user_list_gui()
        return True

    def _kick_matching(self, matcher, reason: str) -> list:
        """踢出所有匹配条件的在线用户，返回被踢出的用户名列表。

        matcher 接收 clients 中的 info dict，返回 True 表示踢出该用户。
        四个黑名单页签共用此实现，避免各自复制一遍
        「发消息 → 关 socket → 删记录 → 广播」的逻辑。
        """
        kicked: list = []
        with self.clients_lock:
            for uname, info in list(self.clients.items()):
                try:
                    if not matcher(info):
                        continue
                except Exception:
                    continue
                kicked.append(uname)
                self._disconnect_locked(uname, info, reason)

        if kicked:
            self._update_user_list_gui()
            names = "、".join(kicked)
            if len(kicked) == 1:
                msg = f"用户 {kicked[0]} 被踢出（{reason}）"
            else:
                msg = f"用户 {names} 被踢出（{reason}，共 {len(kicked)} 人）"
            self._log(msg)
            self._broadcast_system(msg, send_user_list=True)
        return kicked

    def _kick_user(self) -> None:
        """踢出用户"""
        sel = self.user_listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要踢出的用户")
            return
        username_raw = self.user_listbox.get(sel[0])
        username = username_raw.split(' (')[0] if ' (' in username_raw else username_raw
        if username == ADMIN_NAME:
            messagebox.showinfo("提示", "不能踢出管理员")
            return
        with self.clients_lock:
            if username not in self.clients:
                messagebox.showinfo("提示", "该用户已不在线")
                return
        if messagebox.askyesno("确认", f"确定要踢出用户 {username} 吗？"):
            if self._kick_username(username, '您已被管理员踢出'):
                self._log(f"管理员踢出用户 {username}")
                self._broadcast_system(f"用户 {username} 已被管理员踢出", send_user_list=True)

    def _broadcast_msg(self) -> None:
        """广播消息"""
        txt = simpledialog.askstring("📢 全局广播", "输入广播内容：", parent=self.root)
        if txt and txt.strip():
            content = f"【管理员广播】{txt}"
            self._broadcast_system(content, is_broadcast=True)
            self._log_message('broadcast', f"[广播] 管理员: {txt}")

    def _manage_blacklist(self) -> None:
        """黑名单管理窗口（四个标签页由独立 Tab 类实现）"""
        win = tk.Toplevel(self.root)
        win.title("黑名单管理")
        win.geometry("900x700")
        win.resizable(True, True)

        tab_control = ttk.Notebook(win)
        tab_control.pack(expand=1, fill='both', padx=5, pady=5)

        for cls in (IPBlacklistTab, MACBlacklistTab, DeviceBlacklistTab, HardwareBlacklistTab):
            cls(self, tab_control).build()

        bottom_frame = tk.Frame(win)
        bottom_frame.pack(pady=10)
        tk.Button(bottom_frame, text="关闭", command=win.destroy,
                  font=("Microsoft YaHei", 10), width=15).pack()

    def on_close(self) -> None:
        """关闭服务器"""
        if messagebox.askokcancel("退出", "确定要关闭服务器吗？"):
            self._log("服务器正在关闭...")
            self.running = False

            # 关闭文件传输线程池
            try:
                self._file_workers.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                self._log(f"关闭文件线程池异常: {e}")

            # 清理未完成的分块上传（临时文件 + 句柄）
            with self.uploads_lock:
                pending = list(self.uploads.values())
                self.uploads.clear()
            for info in pending:
                try:
                    info['fp'].close()
                except Exception:
                    pass
                try:
                    os.remove(info['path'])
                except Exception:
                    pass

            self._save_blacklist()
            self._save_device_blacklist()
            self._save_mute_list()
            self._save_server_hidden()
            try:
                # 先复制 socket 列表并发送关闭通知，再清理 clients
                with self.clients_lock:
                    socks = [info['sock'] for info in self.clients.values()]
                for sock in socks:
                    try:
                        self._send_msg(sock, {'type': 'system_message', 'content': '服务器已关闭'})
                    except OSError:
                        pass
                    finally:
                        try:
                            sock.close()
                        except OSError:
                            pass
                with self.clients_lock:
                    self.clients.clear()
                    self._sock_locks.clear()
                if self.sock:
                    self.sock.close()
                    self.sock = None
            except Exception as e:
                self._log(f"关闭服务器时发生异常: {e}")
            finally:
                self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    app = Server(root)
    root.mainloop()