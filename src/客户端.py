import socket
import threading
import json
import struct
import time
import base64
import os
import queue
import tkinter as tk
from tkinter import scrolledtext, messagebox, filedialog, ttk
from datetime import datetime
import ctypes
import sys
import platform
import traceback
from collections import OrderedDict
import hashlib
import uuid
from collections import defaultdict

# ---------- 任务栏闪烁支持 ----------
try:
    FLASHW_STOP = 0
    FLASHW_CAPTION = 1
    FLASHW_TRAY = 2
    FLASHW_ALL = 3
    FLASHW_TIMER = 4
    FLASHW_TIMERNOFG = 12

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint),
                    ("hwnd", ctypes.c_void_p),
                    ("dwFlags", ctypes.c_uint),
                    ("uCount", ctypes.c_uint),
                    ("dwTimeout", ctypes.c_uint)]

    def flash_taskbar(window, count=5, timeout=0):
        if not window.winfo_exists():
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            info = FLASHWINFO(cbSize=ctypes.sizeof(FLASHWINFO),
                              hwnd=hwnd,
                              dwFlags=FLASHW_ALL | FLASHW_TIMERNOFG,
                              uCount=count,
                              dwTimeout=timeout)
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except:
            pass

    def stop_flash_taskbar(window):
        if not window.winfo_exists():
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            info = FLASHWINFO(cbSize=ctypes.sizeof(FLASHWINFO),
                              hwnd=hwnd,
                              dwFlags=FLASHW_STOP,
                              uCount=0,
                              dwTimeout=0)
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except:
            pass

    TASKBAR_FLASH_SUPPORT = True
except:
    TASKBAR_FLASH_SUPPORT = False

# ---------- 协议常量（必须与 服务器.py 保持一致）----------
SERVER_PORT = 9999            # 聊天服务端口
DISCOVERY_PORT = 9998         # 局域网发现（UDP 广播）端口
DISCOVERY_MAGIC = b"DISCOVER_CHAT_SERVER:v1"
DISCOVERY_BROADCAST = '255.255.255.255'
MAX_MESSAGE_LENGTH = 5000     # 单条聊天消息最大长度（与服务端一致）


class Client:
    CHUNK_SIZE = 65536
    FILE_CHUNK_SIZE = 256 * 1024                      # 共享文件分块大小（与服务器一致）
    MAX_SHARED_FILE_SIZE = 512 * 1024 * 1024          # 单个共享文件上限
    MAX_BODY_SIZE = 500 * 1024 * 1024
    HEARTBEAT_INTERVAL = 5
    RECV_TIMEOUT = 10
    QUEUE_BATCH_SIZE = 30
    GUI_UPDATE_INTERVAL = 100
    MAX_RECV_TIMEOUT_COUNT = 30

    def __init__(self, root):
        self.root = root
        self.root.title("局域网聊天室客户端")
        self.root.geometry("900x600")
        self.root.minsize(700, 500)

        self.sock = None
        self.username = None
        self.device_id = None
        self.connected = False
        self.send_lock = threading.Lock()

        self.msg_queue = queue.Queue(maxsize=1000)
        self.private_windows = {}
        self.private_windows_lock = threading.Lock()
        self.file_transfers = {}
        self.file_transfers_lock = threading.Lock()
        self.shared_files = []
        self.shared_files_lock = threading.Lock()
        self.private_messages = OrderedDict()
        self.private_messages_lock = threading.Lock()
        self.unread_count = 0
        self.unread_lock = threading.Lock()   # 未读计数跨线程保护
        self.private_unread = {}
        self.private_unread_lock = threading.Lock()
        self.online_users = []
        self.users_lock = threading.Lock()
        self.pending_shared_downloads = {}
        self.pending_downloads_lock = threading.Lock()
        self._download_counter = 0
        self.shared_files_window = None
        self.flash_id = None
        self.max_private_messages = 1000
        self.muted = False
        self.muted_until = 0
        # 重连相关
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        self.reconnect_delay = 2
        # 共享文件下载缓存
        self._shared_download = {}  # filename -> {fp, total_size, received, save_path}
        # 缓存硬件指纹（避免每次登录重复调用子进程）
        self._cached_hw_fingerprint = None

        # IP 协议模式
        self.ip_mode_var = tk.StringVar(value="auto")

        # 引用回复
        self.reply_to = None

        # 草稿文件
        self.draft_file = 'message_draft.txt'

        # 加载或生成设备ID
        self.load_device_id()

        self.setup_login_ui()
        self.setup_chat_ui()
        self.chat_frame.pack_forget()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind('<FocusIn>', self.on_focus_in)
        self.process_queue()

        # 启动自动保存草稿
        self._auto_save_draft()

    def load_device_id(self):
        device_id_file = 'client_device_id.txt'
        if os.path.exists(device_id_file):
            try:
                with open(device_id_file, 'r') as f:
                    self.device_id = f.read().strip()
            except:
                self.device_id = None
        if not self.device_id:
            machine_id = str(uuid.getnode()) + (os.environ.get('COMPUTERNAME')
                                               or platform.node() or '')
            self.device_id = hashlib.sha256(machine_id.encode()).hexdigest()[:32]
            try:
                with open(device_id_file, 'w') as f:
                    f.write(self.device_id)
            except:
                pass

    def _get_local_mac(self) -> str:
        """获取本机MAC地址"""
        try:
            mac = uuid.getnode()
            mac_str = ':'.join(('%012X' % mac)[i:i + 2] for i in range(0, 12, 2))
            return mac_str
        except:
            return "未知MAC"

    def _get_hardware_ids(self) -> dict:
        """获取本机所有硬件标识"""
        import subprocess
        import platform
        import re

        hardware = {
            'mac': self._get_local_mac(),
            'cpu': '未知',
            'disk': '未知',
            'motherboard': '未知',
            'bios_uuid': '未知'
        }

        try:
            system = platform.system()

            # 1. CPU序列号
            if system == 'Windows':
                try:
                    result = subprocess.run(
                        ['wmic', 'cpu', 'get', 'processorid'],
                        capture_output=True, text=True, timeout=3
                    )
                    lines = result.stdout.split('\n')
                    for line in lines:
                        line = line.strip()
                        if line and line != 'ProcessorId' and line != 'ProcessorId':
                            hardware['cpu'] = line
                            break
                except:
                    pass
            else:
                # Linux: 读取 /proc/cpuinfo
                try:
                    with open('/proc/cpuinfo', 'r') as f:
                        for line in f:
                            if 'processor' in line and ':' in line:
                                # 获取CPU信息
                                parts = line.split(':')
                                if len(parts) > 1:
                                    hardware['cpu'] = parts[1].strip()
                                    break
                except:
                    pass

            # 2. 硬盘序列号
            if system == 'Windows':
                try:
                    result = subprocess.run(
                        ['wmic', 'diskdrive', 'get', 'serialnumber'],
                        capture_output=True, text=True, timeout=3
                    )
                    lines = result.stdout.split('\n')
                    for line in lines:
                        line = line.strip()
                        if line and line != 'SerialNumber' and line != 'SerialNumber':
                            hardware['disk'] = line
                            break
                except:
                    pass
            else:
                # Linux
                try:
                    result = subprocess.run(
                        ['lsblk', '-o', 'SERIAL', '-n'],
                        capture_output=True, text=True, timeout=3
                    )
                    lines = result.stdout.split('\n')
                    for line in lines:
                        if line.strip():
                            hardware['disk'] = line.strip()
                            break
                except:
                    pass

            # 3. 主板序列号
            if system == 'Windows':
                try:
                    result = subprocess.run(
                        ['wmic', 'baseboard', 'get', 'serialnumber'],
                        capture_output=True, text=True, timeout=3
                    )
                    lines = result.stdout.split('\n')
                    for line in lines:
                        line = line.strip()
                        if line and line != 'SerialNumber' and line != 'SerialNumber':
                            hardware['motherboard'] = line
                            break
                except:
                    pass
            else:
                # Linux: 直接读取 sysfs（无需 root，且不会像 sudo 那样阻塞等待密码）
                try:
                    with open('/sys/class/dmi/id/board_serial', 'r') as f:
                        serial = f.read().strip()
                    if serial:
                        hardware['motherboard'] = serial
                except:
                    pass

            # 4. BIOS UUID
            if system == 'Windows':
                try:
                    result = subprocess.run(
                        ['wmic', 'csproduct', 'get', 'uuid'],
                        capture_output=True, text=True, timeout=3
                    )
                    lines = result.stdout.split('\n')
                    for line in lines:
                        line = line.strip()
                        if line and line != 'UUID' and line != 'UUID':
                            hardware['bios_uuid'] = line
                            break
                except:
                    pass
            else:
                # Linux
                try:
                    with open('/sys/class/dmi/id/product_uuid', 'r') as f:
                        hardware['bios_uuid'] = f.read().strip()
                except:
                    pass

        except Exception as e:
            print(f"获取硬件标识失败: {e}")

        return hardware

    def _get_hardware_fingerprint(self) -> str:
        """生成硬件指纹（缓存版，避免重复调用子进程）"""
        if self._cached_hw_fingerprint:
            return self._cached_hw_fingerprint

        hardware = self._get_hardware_ids()

        # 组合所有硬件标识
        components = [
            hardware.get('cpu', ''),
            hardware.get('disk', ''),
            hardware.get('motherboard', ''),
            hardware.get('mac', ''),
            hardware.get('bios_uuid', '')
        ]

        # 过滤空值
        components = [c for c in components if c and c != '未知']

        if not components:
            fingerprint = hashlib.sha256(hardware['mac'].encode()).hexdigest()[:32]
        else:
            combined = '|'.join(components)
            fingerprint = hashlib.sha256(combined.encode()).hexdigest()[:32]

        self._cached_hw_fingerprint = fingerprint
        return fingerprint




    @staticmethod
    def _is_ipv6(ip_str):
        """更准确的IPv6判断"""
        # 去掉方括号包裹的IPv6格式 [::1]
        if ip_str.startswith('['):
            return True
        # 纯IPv6包含多个冒号（至少2个），且不包含点
        return ip_str.count(':') >= 2 and '.' not in ip_str

    @staticmethod
    def _validate_username(name):
        """用户名本地校验，规则与服务端保持一致。

        返回 (是否通过, 失败原因)。服务端还会再校验一次，
        这里只是为了提前给用户反馈、少一次连接往返。
        """
        if not isinstance(name, str):
            return False, "格式无效"
        name = name.strip()
        if not name:
            return False, "不能为空"
        if len(name) > 30:
            return False, "长度不能超过30个字符"
        illegal = '\n\r\t|/\\:()'
        for c in illegal:
            if c in name:
                return False, f"不能包含字符 {c!r}"
        for c in name:
            if ord(c) < 32 or ord(c) == 127:
                return False, "不能包含控制字符"
        return True, ""

    @staticmethod
    def _pure_username(display):
        """从服务器下发的显示名中提取纯用户名。

        显示名形如 "alice (AA:BB:CC:DD:EE:FF) [指纹:xxx]" 或
        "alice (3)"（未读计数）。服务端已禁止用户名含 '(' ')'，
        因此按第一个 ' (' 切分是安全的。
        """
        if not isinstance(display, str):
            return ''
        return display.split(' (')[0].strip()

    # ---------- 服务器发现 ----------
    @staticmethod
    def discover_servers(timeout=3):
        """广播发现局域网内的聊天服务器"""
        discovered = []
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(timeout)
            sock.sendto(DISCOVERY_MAGIC, (DISCOVERY_BROADCAST, DISCOVERY_PORT))
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    break
                except OSError:
                    break
                try:
                    response = json.loads(data.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(response, dict) or response.get('type') != 'chat_server':
                    continue
                port = response.get('port', SERVER_PORT)
                ips = response.get('ips', [])
                if not isinstance(ips, list):
                    ips = []
                for ip in ips:
                    if isinstance(ip, str) and ip:
                        discovered.append((ip, port))
                if not ips:
                    discovered.append((addr[0], port))
        except OSError as e:
            print(f"发现服务器失败: {e}")
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        # 去重并保持顺序
        seen = set()
        unique = []
        for s in discovered:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return unique

    def setup_login_ui(self):
        self.login_frame = tk.Frame(self.root)
        self.login_frame.pack(padx=20, pady=20)

        tk.Label(self.login_frame, text="🎉 局域网聊天室",
                 font=("Microsoft YaHei", 16, "bold")).grid(row=0, columnspan=3, pady=(0, 20))
        tk.Label(self.login_frame, text="用户名：", font=("Microsoft YaHei", 10)).grid(row=1, column=0, sticky='w', pady=5)
        self.name_entry = tk.Entry(self.login_frame, width=30, font=("Microsoft YaHei", 10))
        self.name_entry.grid(row=1, column=1, columnspan=2, padx=10, pady=5)
        self.name_entry.focus()
        self.name_entry.bind('<Return>', lambda e: self.login())

        tk.Label(self.login_frame, text="服务器IP：", font=("Microsoft YaHei", 10)).grid(row=2, column=0, sticky='w', pady=5)
        self.ip_entry = tk.Entry(self.login_frame, width=30, font=("Microsoft YaHei", 10))
        self.ip_entry.insert(0, "127.0.0.1")
        self.ip_entry.grid(row=2, column=1, columnspan=2, padx=10, pady=5)
        self.ip_entry.bind('<Return>', lambda e: self.login())

        # IP 模式选择
        tk.Label(self.login_frame, text="协议模式：", font=("Microsoft YaHei", 10)).grid(row=3, column=0, sticky='w', pady=5)
        mode_frame = tk.Frame(self.login_frame)
        mode_frame.grid(row=3, column=1, columnspan=2, sticky='w', padx=10, pady=5)
        tk.Radiobutton(mode_frame, text="自动", variable=self.ip_mode_var,
                       value="auto", font=("Microsoft YaHei", 9)).pack(side='left', padx=5)
        tk.Radiobutton(mode_frame, text="IPv4", variable=self.ip_mode_var,
                       value="ipv4", font=("Microsoft YaHei", 9)).pack(side='left', padx=5)
        tk.Radiobutton(mode_frame, text="IPv6", variable=self.ip_mode_var,
                       value="ipv6", font=("Microsoft YaHei", 9)).pack(side='left', padx=5)

        # 扫描按钮
        tk.Button(self.login_frame, text="🔍 扫描局域网服务器",
                  command=self.scan_servers, width=18, height=1,
                  font=("Microsoft YaHei", 10),
                  bg="#2196F3", fg="white").grid(row=4, columnspan=3, pady=(5, 0))

        tk.Label(self.login_frame, text="发现的服务器：",
                 font=("Microsoft YaHei", 10)).grid(row=5, column=0, sticky='w', pady=(10, 0))
        self.server_listbox = tk.Listbox(self.login_frame, height=5,
                                         font=("Microsoft YaHei", 10))
        self.server_listbox.grid(row=6, column=0, columnspan=3, sticky='ew', padx=5, pady=5)
        self.server_listbox.bind('<Double-Button-1>', self._select_discovered_server)
        self.scan_status_label = tk.Label(self.login_frame, text="",
                                          font=("Microsoft YaHei", 9), fg="#666")
        self.scan_status_label.grid(row=7, columnspan=3, pady=(0, 5))

        tk.Button(self.login_frame, text="登录连接", width=20, height=2,
                  command=self.login, font=("Microsoft YaHei", 10, "bold"),
                  bg="#4CAF50", fg="white").grid(row=8, columnspan=3, pady=20)

    def scan_servers(self):
        self.server_listbox.delete(0, tk.END)
        self.server_listbox.insert(tk.END, "正在扫描局域网...")
        self.scan_status_label.config(text="扫描中，请稍候...")
        self.root.update()

        def scan_thread():
            try:
                servers = self.discover_servers(timeout=3)
                self.root.after(0, self._update_server_list, servers)
            except Exception as e:
                self.root.after(0, self._update_server_list, [])

        threading.Thread(target=scan_thread, daemon=True).start()

    def _update_server_list(self, servers):
        self.server_listbox.delete(0, tk.END)
        if servers:
            for ip, port in servers:
                self.server_listbox.insert(tk.END, f"{ip}:{port}")
            self.scan_status_label.config(text=f"发现 {len(servers)} 个服务器")
        else:
            self.server_listbox.insert(tk.END, "未发现服务器")
            self.scan_status_label.config(text="未发现服务器（可能隐藏模式或网络问题）")

    def _select_discovered_server(self, event):
        sel = self.server_listbox.curselection()
        if sel:
            text = self.server_listbox.get(sel[0])
            if ':' in text:
                ip = text.split(':')[0]
                self.ip_entry.delete(0, tk.END)
                self.ip_entry.insert(0, ip)

    # ---------- 聊天UI ----------
    def setup_chat_ui(self):
        self.chat_frame = tk.Frame(self.root)
        self.chat_frame.grid_rowconfigure(0, weight=1)
        self.chat_frame.grid_rowconfigure(1, weight=0)
        self.chat_frame.grid_columnconfigure(0, weight=3)
        self.chat_frame.grid_columnconfigure(1, weight=1)

        chat_left_frame = tk.Frame(self.chat_frame)
        chat_left_frame.grid(row=0, column=0, padx=(5, 2), pady=5, sticky='nsew')
        chat_left_frame.grid_rowconfigure(0, weight=0)
        chat_left_frame.grid_rowconfigure(1, weight=1)
        chat_left_frame.grid_columnconfigure(0, weight=1)

        title_frame = tk.Frame(chat_left_frame)
        title_frame.grid(row=0, column=0, sticky='ew', pady=(0, 5))

        # 连接状态指示器
        self.status_canvas = tk.Canvas(title_frame, width=12, height=12, highlightthickness=0)
        self.status_canvas.pack(side='left', padx=(0, 5))
        self.status_label = tk.Label(title_frame, text="已连接", font=("Microsoft YaHei", 9))
        self.status_label.pack(side='left')

        self.group_title_label = tk.Label(title_frame, text="💬 群聊",
                                          font=("Microsoft YaHei", 10, "bold"), bg="#f0f0f0")
        self.group_title_label.pack(side='left', padx=(10, 0))

        self.private_unread_label = tk.Label(title_frame, text="💬 私聊: 0",
                                             font=("Microsoft YaHei", 9), fg="#666666")
        self.private_unread_label.pack(side='left', padx=(20, 0))

        self.muted_label = tk.Label(title_frame, text="",
                                    font=("Microsoft YaHei", 9), fg="#FF5722")
        self.muted_label.pack(side='left', padx=(20, 0))

        tk.Button(title_frame, text="📁 共享文件", command=self.open_shared_files_window,
                  font=("Microsoft YaHei", 9), bg="#4CAF50", fg="white").pack(side='right', padx=(5, 0))

        self.chat_area = scrolledtext.ScrolledText(chat_left_frame, state='disabled',
                                                   font=("Microsoft YaHei", 10))
        self.chat_area.grid(row=1, column=0, sticky='nsew')
        self.chat_area.bind('<Button-1>', lambda e: self.clear_group_unread())
        self.chat_area.tag_config('system', foreground='#666666', font=("Microsoft YaHei", 9, "italic"))
        self.chat_area.tag_config('self', foreground='#2196F3', font=("Microsoft YaHei", 10, "bold"))
        self.chat_area.tag_config('other', foreground='#333333', font=("Microsoft YaHei", 10))
        self.chat_area.tag_config('admin', foreground='#FF5722', font=("Microsoft YaHei", 10, "bold"))
        self.chat_area.tag_config('broadcast', foreground='#D32F2F',
                                  font=("Microsoft YaHei", 12, "bold"))
        # 右键菜单（引用/复制）
        self.chat_menu = tk.Menu(self.root, tearoff=0)
        self.chat_menu.add_command(label="💬 引用回复", command=self._quote_reply)
        self.chat_menu.add_command(label="📋 复制", command=self._copy_selected)
        self.chat_area.bind('<Button-3>', self._show_chat_menu)

        # 用户列表面板
        user_frame = tk.Frame(self.chat_frame)
        user_frame.grid(row=0, column=1, padx=(2, 5), pady=5, sticky='nsew')
        user_frame.grid_rowconfigure(0, weight=0)
        user_frame.grid_rowconfigure(1, weight=1)
        user_frame.grid_rowconfigure(2, weight=0)
        user_frame.grid_columnconfigure(0, weight=1)

        user_title_frame = tk.Frame(user_frame)
        user_title_frame.grid(row=0, column=0, sticky='ew', pady=(0, 5))
        tk.Label(user_title_frame, text="👥 在线用户", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f0f0f0").pack(side='left')

        self.user_listbox = tk.Listbox(user_frame, font=("Microsoft YaHei", 10))
        self.user_listbox.grid(row=1, column=0, sticky='nsew')
        self.user_listbox.bind('<Double-Button-1>', self.open_private_chat)

        user_btn_frame = tk.Frame(user_frame)
        user_btn_frame.grid(row=2, column=0, sticky='ew', pady=(5, 0))
        tk.Button(user_btn_frame, text="💬 私聊", command=self.private_chat_selected,
                  font=("Microsoft YaHei", 9)).pack(side='left', fill='x', expand=True, padx=(0, 2))
        tk.Button(user_btn_frame, text="📎 发送文件", command=self.send_file_to_selected,
                  font=("Microsoft YaHei", 9)).pack(side='right', fill='x', expand=True, padx=(2, 0))

        # 输入区域
        input_frame = tk.Frame(self.chat_frame)
        input_frame.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky='ew')
        input_frame.grid_columnconfigure(0, weight=1)
        input_frame.grid_columnconfigure(1, weight=0)
        input_frame.grid_columnconfigure(2, weight=0)

        # 引用回复标签
        self.reply_label = tk.Label(input_frame, text="", fg="#666666",
                                    font=("Microsoft YaHei", 9), anchor='w')
        self.reply_label.grid(row=0, column=0, sticky='w', padx=5)

        self.input_text = tk.Text(input_frame, height=4, font=("Microsoft YaHei", 10))
        self.input_text.grid(row=1, column=0, sticky='ew', padx=(0, 5))
        self.input_text.bind('<Return>', self._on_input_return)

        btn_frame = tk.Frame(input_frame)
        btn_frame.grid(row=1, column=1, padx=(0, 5))
        tk.Button(btn_frame, text="发送 (Enter)", width=10, height=2,
                  command=self.send_public, font=("Microsoft YaHei", 9, "bold"),
                  bg="#2196F3", fg="white").pack(fill='x')

        btn_frame2 = tk.Frame(input_frame)
        btn_frame2.grid(row=1, column=2)
        tk.Button(btn_frame2, text="📎 文件", width=10, height=2,
                  command=self.send_file_to_selected, font=("Microsoft YaHei", 9),
                  bg="#FF9800", fg="white").pack(fill='x')

        # 加载草稿
        self._load_draft()

        # 更新连接状态
        self.update_connection_status()

    # ---------- 连接状态指示器 ----------
    def update_connection_status(self):
        if self.connected:
            self.status_canvas.delete("all")
            self.status_canvas.create_oval(2, 2, 10, 10, fill="#4CAF50", outline="")
            self.status_label.config(text="已连接", fg="#4CAF50")
        else:
            self.status_canvas.delete("all")
            self.status_canvas.create_oval(2, 2, 10, 10, fill="#FF5722", outline="")
            self.status_label.config(text="未连接", fg="#FF5722")

    # ---------- 草稿自动保存 ----------
    def _load_draft(self):
        if os.path.exists(self.draft_file):
            try:
                with open(self.draft_file, 'r', encoding='utf-8') as f:
                    draft = f.read()
                if draft and hasattr(self, 'input_text'):
                    self.input_text.insert('1.0', draft)
            except:
                pass

    def _save_draft(self):
        if not hasattr(self, 'input_text'):
            return
        try:
            content = self.input_text.get('1.0', tk.END).strip()
            if content:
                with open(self.draft_file, 'w', encoding='utf-8') as f:
                    f.write(content)
            elif os.path.exists(self.draft_file):
                os.remove(self.draft_file)
        except:
            pass

    def _auto_save_draft(self):
        self._save_draft()
        # 每30秒保存一次
        self.root.after(30000, self._auto_save_draft)

    # ---------- 引用回复 ----------
    def _show_chat_menu(self, event):
        try:
            self.chat_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.chat_menu.grab_release()

    def _quote_reply(self):
        try:
            selected = self.chat_area.get(tk.SEL_FIRST, tk.SEL_LAST)
            if selected:
                self.reply_to = selected[:50] + "..." if len(selected) > 50 else selected
                self.reply_label.config(text=f"回复: {self.reply_to}")
        except tk.TclError:
            pass

    def _copy_selected(self):
        try:
            selected = self.chat_area.get(tk.SEL_FIRST, tk.SEL_LAST)
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
        except tk.TclError:
            pass

    def _on_input_return(self, event):
        if event.state & 0x4:  # Ctrl+Enter 换行
            self.input_text.insert(tk.INSERT, '\n')
            return "break"
        else:
            self.send_public()
            return "break"

    def _stop_flash(self):
        if TASKBAR_FLASH_SUPPORT:
            stop_flash_taskbar(self.root)

    def _start_flash(self):
        # 已有未读时才闪烁，避免在已读状态下反复触发
        with self.unread_lock:
            group_unread = self.unread_count
        if TASKBAR_FLASH_SUPPORT and (group_unread or self.private_unread_total()):
            flash_taskbar(self.root)

    def on_focus_in(self, event):
        """主窗口获得焦点：清零群聊未读并停止任务栏闪烁"""
        with self.unread_lock:
            changed = self.unread_count > 0
            self.unread_count = 0
        if changed:
            self.update_window_title()
        self._stop_flash()

    def clear_group_unread(self):
        with self.unread_lock:
            changed = self.unread_count > 0
            self.unread_count = 0
        if changed:
            self.update_window_title()

    def _inc_unread(self) -> None:
        """群聊未读 +1（线程安全）"""
        with self.unread_lock:
            self.unread_count += 1

    def private_unread_total(self):
        with self.private_unread_lock:
            return sum(self.private_unread.values())

    def update_window_title(self):
        with self.unread_lock:
            group_unread = self.unread_count
        private_total = self.private_unread_total()
        total_unread = group_unread + private_total

        parts = []
        if group_unread > 0:
            parts.append(f"群聊({group_unread})")
        if private_total > 0:
            parts.append(f"私聊({private_total})")
        if self.muted:
            parts.append("禁言中")

        title = ""
        if parts:
            title = "(" + " | ".join(parts) + ") "
        title += "局域网聊天室客户端"
        self.root.title(title)

        if group_unread > 0:
            self.group_title_label.config(text=f"💬 群聊 ({group_unread})")
        else:
            self.group_title_label.config(text="💬 群聊")

        if private_total > 0:
            self.private_unread_label.config(text=f"💬 私聊: {private_total}", fg="#FF5722")
        else:
            self.private_unread_label.config(text="💬 私聊: 0", fg="#666666")

        if self.muted:
            remaining = max(0, int((self.muted_until - time.time()) // 60))
            if remaining > 0:
                self.muted_label.config(text=f"🔇 禁言中 ({remaining}分钟)")
            else:
                self.muted_label.config(text="🔇 禁言中")
        else:
            self.muted_label.config(text="")

        self.refresh_user_list_display()

        if total_unread > 0:
            self._start_flash()

    def refresh_user_list_display(self):
        self.user_listbox.delete(0, tk.END)
        with self.users_lock:
            with self.private_unread_lock:
                for u in self.online_users:
                    # 服务器发送的用户名已经包含MAC和指纹，直接显示
                    display_name = u
                    # 检查是否有私聊未读
                    pure_name = self._pure_username(u)
                    if pure_name in self.private_unread and self.private_unread[pure_name] > 0:
                        self.user_listbox.insert(tk.END, f"{display_name} ({self.private_unread[pure_name]})")
                    else:
                        self.user_listbox.insert(tk.END, display_name)

    # ============ 登录 ============
    def login(self, attempt_reconnect=False):
        uname = self.name_entry.get().strip()
        server_ip = self.ip_entry.get().strip()
        if not uname:
            messagebox.showerror("提示", "用户名不能为空！")
            return
        if not server_ip:
            messagebox.showerror("提示", "服务器IP不能为空！")
            return
        if not self._login_silent(attempt_reconnect=False):
            messagebox.showerror("登录失败", "无法连接到服务器，请检查IP地址和网络")
    # ============ 网络收发 ============
    def send_msg(self, msg_dict):
        with self.send_lock:
            sock = self.sock
        if not sock:
            return False
        try:
            raw = json.dumps(msg_dict, ensure_ascii=False).encode('utf-8')
            if len(raw) > self.MAX_BODY_SIZE:
                return False
            header = struct.pack('>I', len(raw))
            with self.send_lock:
                sock.sendall(header + raw)
            return True
        except (socket.error, BrokenPipeError, ConnectionResetError):
            return False
        except Exception:
            return False

    def recv_exact(self, n):
        with self.send_lock:
            sock = self.sock
        if not sock:
            return None
        if n <= 0:
            return b''
        buf = bytearray()
        timeout_count = 0
        # 整体截止时间 + 连续超时次数双保险：
        # 慢速连接不会因反复重置计数而永久挂住，静默死连接也能及时判定失效
        deadline = time.monotonic() + n / 1024 + self.RECV_TIMEOUT
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
                if timeout_count > self.MAX_RECV_TIMEOUT_COUNT:
                    return None
                continue
            except (socket.error, ConnectionResetError):
                return None
            except Exception:
                return None
        return bytes(buf)

    def recv_msg(self):
        with self.send_lock:
            sock = self.sock
        if not sock:
            return None
        try:
            head = self.recv_exact(4)
            if not head:
                return None
            body_len = struct.unpack('>I', head)[0]
            # 下限校验：拒绝 0 长度与超长报文
            if body_len <= 0 or body_len > self.MAX_BODY_SIZE:
                return None
            body = self.recv_exact(body_len)
            if body is None:
                return None
            parsed = json.loads(body.decode('utf-8'))
            # 后续处理全部按 dict 访问，非 dict 直接丢弃
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
        except Exception:
            return None

    # ============ 后台循环 ============
    def receive_loop(self):
        while self.connected and self.sock:
            try:
                msg = self.recv_msg()
                if msg is None:
                    break
                if self.msg_queue.full():
                    try:
                        self.msg_queue.get_nowait()
                    except queue.Empty:
                        pass
                self.msg_queue.put(msg)
            except Exception:
                break
        self.connected = False
        self.root.after(0, self.update_connection_status)
        if self.username:
            self.root.after(0, self._attempt_reconnect)
        else:
            try:
                self.msg_queue.put({'type': 'disconnected'})
            except:
                pass

    def heartbeat_loop(self):
        while self.connected and self.sock:
            try:
                time.sleep(self.HEARTBEAT_INTERVAL)
                if not self.send_msg({'type': 'heartbeat'}):
                    break
            except Exception:
                break
        if self.connected:
            self.connected = False
            self.root.after(0, self.update_connection_status)
            try:
                self.msg_queue.put({'type': 'disconnected'})
            except:
                pass

    # ============ GUI消息处理 ============
    def process_queue(self):
        batch_count = 0
        while not self.msg_queue.empty() and batch_count < self.QUEUE_BATCH_SIZE:
            try:
                data = self.msg_queue.get_nowait()
                self.handle_message(data)
                batch_count += 1
            except queue.Empty:
                break
            except Exception as e:
                print(f"处理消息异常: {e}")
                traceback.print_exc()
        self.root.after(self.GUI_UPDATE_INTERVAL, self.process_queue)

    def handle_message(self, msg):
        mtype = msg.get('type')
        if mtype == 'shared_file_start':
            self._handle_shared_file_start(msg)
            return
        elif mtype == 'shared_file_chunk':
            self._handle_shared_file_chunk(msg)
            return
        elif mtype == 'shared_file_end':
            self._handle_shared_file_end(msg)
            return
        if mtype == 'heartbeat_ack':
            return
        elif mtype == 'system_message':
            content = msg.get('content', '')
            is_broadcast = msg.get('is_broadcast', False)
            if is_broadcast:
                self._append_tagged_msg(f"[系统] {content}", 'broadcast')
            else:
                self._append_tagged_msg(f"[系统] {content}", 'system')
            if 'user_list' in msg:
                with self.users_lock:
                    self.online_users = msg['user_list']
                # 刷新用户列表显示
                self.refresh_user_list_display()
            self._start_flash()
            self.update_window_title()
        elif mtype == 'public_message':
            sender = msg.get('from', '未知')
            content = msg.get('content', '')
            is_admin = msg.get('is_admin', False)
            if sender == self.username:
                self._append_tagged_msg(f"[我] {content}", 'self')
            else:
                if is_admin:
                    self._append_tagged_msg(f"[管理员] {content}", 'admin')
                else:
                    self._append_tagged_msg(f"[{sender}] {content}", 'other')
                self._inc_unread()
                self.update_window_title()
        elif mtype == 'private_message':
            self.handle_private_message(msg)
        elif mtype == 'file_request':
            self.handle_file_req(msg)
        elif mtype == 'file_response':
            self.handle_file_resp(msg)
        elif mtype == 'file_chunk':
            self.handle_chunk(msg)
        elif mtype == 'file_end':
            self.handle_file_finish(msg)
        elif mtype == 'file_error':
            self.handle_file_error(msg)
        elif mtype == 'kicked':
            reason = msg.get('reason', '您已被管理员踢出聊天室')
            messagebox.showwarning("提示", reason)
            self.on_close()
        elif mtype == 'muted':
            self.muted = True
            self.muted_until = time.time() + msg.get('duration', 0) * 60
            self._append_tagged_msg(f"[系统] {msg.get('reason', '您已被禁言')}", 'system')
            self.update_window_title()
        elif mtype == 'unmuted':
            self.muted = False
            self.muted_until = 0
            self._append_tagged_msg(f"[系统] {msg.get('reason', '您已被解除禁言')}", 'system')
            self.update_window_title()
        elif mtype == 'message_history':
            messages = msg.get('messages', [])
            for m in messages:
                sender = m.get('from', '未知')
                content = m.get('content', '')
                is_admin = m.get('is_admin', False)
                if is_admin:
                    self._append_tagged_msg(f"[管理员] {content}", 'admin')
                else:
                    self._append_tagged_msg(f"[{sender}] {content}", 'other')
        elif mtype == 'disconnected':
            messagebox.showerror("断开连接", "与服务器连接中断！")
            self.on_close()
        elif mtype == 'error':
            messagebox.showerror("❌ 错误", msg.get('reason', '未知错误'))
        elif mtype == 'shared_file_list':
            with self.shared_files_lock:
                self.shared_files = msg.get('files', [])
            if self.shared_files_window:
                self.update_shared_files_list()
        elif mtype == 'upload_accepted':
            self.append_group_msg(f"📤 服务器已接收上传: {msg.get('filename', '')}", system=True)
        elif mtype == 'shared_file_data':
            self.handle_shared_file_download(msg)
        elif mtype == '_download_complete':
            self._handle_download_complete(msg)
        elif mtype == '_download_error':
            self._handle_download_error(msg)

    def handle_private_message(self, msg):
        sender = msg.get('from', '未知')
        content = msg.get('content', '')
        if not isinstance(sender, str) or not sender:
            return
        if not isinstance(content, str):
            content = str(content)

        with self.private_messages_lock:
            if sender not in self.private_messages:
                self.private_messages[sender] = []
            if len(self.private_messages[sender]) >= self.max_private_messages:
                self.private_messages[sender].pop(0)
            self.private_messages[sender].append({
                'time': datetime.now().strftime('%H:%M:%S'),
                'sender': sender,
                'content': content
            })

        self._start_flash()

        # 锁顺序约定：private_messages_lock → private_windows_lock → private_unread_lock
        # 这里先在锁内取窗口引用，随即释放，再操作窗口，避免跨锁嵌套导致死锁。
        win = None
        with self.private_windows_lock:
            entry = self.private_windows.get(sender)
            if entry is not None:
                try:
                    if entry.window.winfo_exists():
                        win = entry
                    else:
                        # 窗口已被销毁但记录未清理，顺手清掉
                        self.private_windows.pop(sender, None)
                except Exception:
                    self.private_windows.pop(sender, None)

        if win is not None:
            win.add_msg(sender, content, from_server=True)
            return

        with self.private_unread_lock:
            self.private_unread[sender] = self.private_unread.get(sender, 0) + 1
        self.update_window_title()

    # ============ UI辅助方法 ============
    def _append_tagged_msg(self, text, tag):
        try:
            self.chat_area.config(state='normal')
            self.chat_area.insert(tk.END, text + '\n', tag)
            self.chat_area.see(tk.END)
            self.chat_area.config(state='disabled')
        except Exception:
            pass

    def append_group_msg(self, text, system=False, self_msg=False):
        if system:
            self._append_tagged_msg(text, 'system')
        elif self_msg:
            self._append_tagged_msg(text, 'self')
        else:
            self._append_tagged_msg(text, 'other')

    def send_public(self, event=None):
        if not self.connected:
            messagebox.showwarning("提示", "未连接到服务器")
            return "break"

        if self.muted:
            remaining = max(0, int((self.muted_until - time.time()) // 60))
            if remaining > 0:
                messagebox.showwarning("禁言中", f"您当前处于禁言状态，剩余 {remaining} 分钟")
            else:
                messagebox.showwarning("禁言中", "您当前处于禁言状态")
            return "break"

        txt = self.input_text.get('1.0', tk.END).strip()
        if txt:
            # 处理引用回复
            if self.reply_to:
                txt = f"> {self.reply_to}\n{txt}"
                self.reply_to = None
                self.reply_label.config(text="")

            # 本地先做长度校验，避免发出去才被服务端拒绝
            if len(txt) > MAX_MESSAGE_LENGTH:
                messagebox.showwarning(
                    "提示", f"消息过长（{len(txt)} 字符），上限为 {MAX_MESSAGE_LENGTH} 字符")
                return "break"

            if self.send_msg({'type': 'public_message', 'content': txt}):
                self.input_text.delete('1.0', tk.END)
                # 清除草稿
                try:
                    if os.path.exists(self.draft_file):
                        os.remove(self.draft_file)
                except:
                    pass
            else:
                messagebox.showerror("发送失败", "消息发送失败，请检查网络连接")
        return "break"

    # ============ 发送文件 ============
    def send_file_to_selected(self):
        sel = self.user_listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先在在线用户列表中选择接收文件的用户")
            return
        target_raw = self.user_listbox.get(sel[0])
        target = self._pure_username(target_raw)
        if target == self.username:
            messagebox.showinfo("提示", "不能给自己传文件")
            return
        self.send_file_to_user(target)

    def send_file_to_user(self, target):
        path = filedialog.askopenfilename(title="选择要发送的文件")
        if not path:
            return
        fsize = os.path.getsize(path)
        fname = os.path.basename(path)
        tid = f"{self.username}_{target}_{int(time.time())}"
        with self.file_transfers_lock:
            self.file_transfers[tid] = {
                'type': 'send', 'target': target, 'path': path, 'name': fname,
                'size': fsize, 'chunk': self.CHUNK_SIZE, 'sent_chunks': 0,
                'total_chunks': (fsize + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE,
                'active': False
            }
        if self.send_msg({
            'type': 'file_request', 'to': target, 'from': self.username,
            'filename': fname, 'size': fsize, 'transfer_id': tid
        }):
            self.append_group_msg(f"📎 [文件] 向 {target} 请求发送 {fname} ({fsize}字节)，等待对方确认...", system=True)
        else:
            with self.file_transfers_lock:
                if tid in self.file_transfers:
                    del self.file_transfers[tid]
            messagebox.showerror("发送失败", "文件请求发送失败")

    def _attempt_reconnect(self):
        """尝试重新连接（静默重连，失败后给出提示）"""
        if not self.connected and self.username:
            self.reconnect_attempts += 1
            if self.reconnect_attempts <= self.max_reconnect_attempts:
                delay = min(self.reconnect_delay * (2 ** (self.reconnect_attempts - 1)), 60)
                self.append_group_msg(f"尝试重新连接 ({self.reconnect_attempts}/{self.max_reconnect_attempts})...",
                                      system=True)
                self.root.after(int(delay * 1000), self._do_reconnect)
            else:
                self.append_group_msg("重连失败，请手动重新登录。", system=True)
                self.username = None

    def _do_reconnect(self):
        """执行重连（静默模式，不弹出错误对话框）"""
        if not self.connected:
            self._login_silent(attempt_reconnect=True)

    def _login_silent(self, attempt_reconnect=False):
        """静默登录/重连，失败时不弹窗；返回是否成功"""
        uname = self.username if attempt_reconnect else self.name_entry.get().strip()
        server_ip = self.ip_entry.get().strip()
        if not uname or not server_ip:
            return False

        # 本地先做与服务端一致的校验，避免无谓的连接往返
        ok, reason = self._validate_username(uname)
        if not ok:
            self.append_group_msg(f"用户名无效：{reason}", system=True)
            return False

        try:
            mode = self.ip_mode_var.get()
            if mode == "ipv6":
                family = socket.AF_INET6
            elif mode == "ipv4":
                family = socket.AF_INET
            else:
                family = socket.AF_INET6 if self._is_ipv6(server_ip) else socket.AF_INET

            self.sock = socket.socket(family, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect((server_ip, SERVER_PORT))

            mac = self._get_local_mac()
            hw_fingerprint = self._get_hardware_fingerprint()

            self.send_msg({
                'type': 'login',
                'username': uname,
                'device_id': self.device_id,
                'mac': mac,
                'hardware_fingerprint': hw_fingerprint,
                'hardware_info': self._get_hardware_ids()
            })
            resp = self.recv_msg()

            if resp and resp.get('type') == 'login_success':
                self.username = uname
                self.connected = True
                self.reconnect_attempts = 0
                if 'device_id' in resp:
                    self.device_id = resp['device_id']
                    try:
                        with open('client_device_id.txt', 'w') as f:
                            f.write(self.device_id)
                    except:
                        pass
                if not attempt_reconnect:
                    self.login_frame.pack_forget()
                    self.chat_frame.pack(fill='both', expand=True)
                threading.Thread(target=self.receive_loop, daemon=True).start()
                threading.Thread(target=self.heartbeat_loop, daemon=True).start()
                self.append_group_msg(f"🎉 欢迎 {self.username} 进入聊天室！", system=True)
                self.request_shared_files()
                self.update_window_title()
                self.update_connection_status()
                return True
            else:
                reason = resp.get('reason', '登录失败') if resp else '服务器无响应'
                self.append_group_msg(f"登录失败: {reason}", system=True)
                if self.sock:
                    try:
                        self.sock.close()
                    except:
                        pass
                    self.sock = None
                return False
        except Exception as e:
            if self.sock:
                try:
                    self.sock.close()
                except:
                    pass
                self.sock = None
            self.append_group_msg(f"连接失败: {str(e)}", system=True)
            return False


    # ============ 共享文件 ============
    def request_shared_files(self):
        if self.connected:
            self.send_msg({'type': 'shared_file_list'})

    def open_shared_files_window(self):
        if self.shared_files_window and self.shared_files_window.winfo_exists():
            self.shared_files_window.lift()
            return
        window = tk.Toplevel(self.root)
        window.title("📁 共享文件")
        window.geometry("550x450")
        window.minsize(450, 350)
        window.grid_rowconfigure(0, weight=0)
        window.grid_rowconfigure(1, weight=1)
        window.grid_rowconfigure(2, weight=0)
        window.grid_columnconfigure(0, weight=1)

        title_frame = tk.Frame(window)
        title_frame.grid(row=0, column=0, sticky='ew', padx=10, pady=10)
        title_frame.grid_columnconfigure(0, weight=1)
        title_frame.grid_columnconfigure(1, weight=0)
        title_frame.grid_columnconfigure(2, weight=0)
        tk.Label(title_frame, text="📁 共享文件", font=("Microsoft YaHei", 12, "bold")).grid(row=0, column=0, sticky='w')
        tk.Button(title_frame, text="🔄 刷新", command=self.request_shared_files,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=1, padx=5)
        tk.Button(title_frame, text="📤 上传文件", command=self.upload_shared_file,
                  font=("Microsoft YaHei", 9), bg="#4CAF50", fg="white").grid(row=0, column=2, padx=5)

        list_frame = tk.Frame(window)
        list_frame.grid(row=1, column=0, sticky='nsew', padx=10, pady=5)
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)
        columns = ('文件名', '大小')
        self.file_tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=15)
        self.file_tree.heading('文件名', text='文件名')
        self.file_tree.heading('大小', text='大小 (字节)')
        self.file_tree.column('文件名', width=350)
        self.file_tree.column('大小', width=120)
        self.file_tree.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky='ns')

        btn_frame = tk.Frame(window)
        btn_frame.grid(row=2, column=0, sticky='ew', pady=10)
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)
        btn_frame.grid_columnconfigure(2, weight=1)
        tk.Button(btn_frame, text="📥 下载选中", command=self.download_selected_file,
                  font=("Microsoft YaHei", 9), bg="#2196F3", fg="white").grid(row=0, column=0, padx=5, sticky='ew')
        tk.Button(btn_frame, text="📤 上传文件", command=self.upload_shared_file,
                  font=("Microsoft YaHei", 9), bg="#4CAF50", fg="white").grid(row=0, column=1, padx=5, sticky='ew')
        tk.Button(btn_frame, text="关闭", command=window.destroy,
                  font=("Microsoft YaHei", 9)).grid(row=0, column=2, padx=5, sticky='ew')

        self.shared_files_window = window
        window.protocol("WM_DELETE_WINDOW", self.close_shared_files_window)
        self.update_shared_files_list()

    def close_shared_files_window(self):
        if self.shared_files_window:
            try:
                self.shared_files_window.destroy()
            except:
                pass
            self.shared_files_window = None

    def update_shared_files_list(self):
        if hasattr(self, 'file_tree') and self.file_tree:
            try:
                for item in self.file_tree.get_children():
                    self.file_tree.delete(item)
                with self.shared_files_lock:
                    for finfo in self.shared_files:
                        if isinstance(finfo, dict):
                            name = finfo.get('name', '')
                            size = finfo.get('size', 0)
                            size_str = f"{size} 字节" if size else "未知"
                        else:
                            name = finfo
                            size_str = "未知"
                        self.file_tree.insert('', 'end', values=(name, size_str))
            except:
                pass

    def upload_shared_file(self):
        if not self.connected:
            messagebox.showwarning("提示", "未连接到服务器")
            return
        path = filedialog.askopenfilename(title="选择要上传的共享文件")
        if not path:
            return
        try:
            fsize = os.path.getsize(path)
        except OSError as e:
            messagebox.showerror("上传失败", f"无法读取文件: {e}")
            return
        fname = os.path.basename(path)

        if fsize <= 0:
            messagebox.showwarning("提示", "不能上传空文件")
            return
        if fsize > self.MAX_SHARED_FILE_SIZE:
            messagebox.showwarning(
                "提示",
                f"文件过大（{fsize / 1024 / 1024:.1f}MB），上限为 "
                f"{self.MAX_SHARED_FILE_SIZE // (1024 * 1024)}MB"
            )
            return
        if not messagebox.askyesno("确认上传",
                                   f"将上传文件到服务器共享：\n文件名：{fname}\n大小：{fsize / 1024:.2f}KB\n\n确定继续吗？"):
            return
        # 分块上传放到后台线程，避免大文件卡死界面
        threading.Thread(target=self._upload_shared_file_worker,
                         args=(path, fname, fsize), daemon=True).start()

    def _upload_shared_file_worker(self, path, fname, fsize):
        """后台线程分块上传共享文件"""
        if not self.send_msg({
            'type': 'upload_shared_file_start',
            'filename': fname,
            'total_size': fsize
        }):
            self._ui_msg("上传失败", "上传请求发送失败")
            return

        sent = 0
        index = 0
        hasher = hashlib.sha256()
        try:
            with open(path, 'rb') as f:
                while True:
                    chunk = f.read(self.FILE_CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    if not self.send_msg({
                        'type': 'upload_shared_file_chunk',
                        'filename': fname,
                        'index': index,
                        'data': base64.b64encode(chunk).decode('ascii')
                    }):
                        self._ui_msg("上传失败", "网络中断，上传已中止")
                        self.send_msg({'type': 'upload_shared_file_end', 'filename': fname, 'abort': True})
                        return
                    sent += len(chunk)
                    index += 1
        except OSError as e:
            self._ui_msg("上传失败", f"读取文件失败: {e}")
            return

        if sent != fsize:
            self._ui_msg("上传失败", f"文件读取不完整（{sent}/{fsize} 字节）")
            return

        if self.send_msg({'type': 'upload_shared_file_end', 'filename': fname,
                          'total_size': fsize, 'sha256': hasher.hexdigest()}):
            self._ui_msg(None, f"📤 共享文件上传完成: {fname} ({fsize} 字节)")
        else:
            self._ui_msg("上传失败", "上传结束包发送失败")

    def download_selected_file(self):
        if not hasattr(self, 'file_tree') or not self.file_tree:
            return
        selection = self.file_tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选择要下载的文件")
            return
        item = self.file_tree.item(selection[0])
        fname = item['values'][0]
        save_path = filedialog.asksaveasfilename(title="保存文件", initialfile=fname)
        if not save_path:
            return
        # 以文件名为键登记保存路径，服务端分块下发时直接复用，不再二次弹窗
        with self.pending_downloads_lock:
            self.pending_shared_downloads[fname] = save_path
        if self.send_msg({'type': 'download_shared_file', 'filename': fname}):
            self.append_group_msg(f"📥 正在下载文件: {fname}...", system=True)
        else:
            with self.pending_downloads_lock:
                self.pending_shared_downloads.pop(fname, None)
            messagebox.showerror("下载失败", "下载请求发送失败")

    def _ui_msg(self, title, text):
        """从后台线程安全地提示用户"""
        try:
            self.root.after(0, lambda: messagebox.showwarning(title, text) if title
                            else self.append_group_msg(text, system=True))
        except Exception:
            pass

    def handle_shared_file_download(self, msg):
        """旧协议：整文件 base64 下载（保留兼容旧服务器）"""
        fname = msg.get('filename')
        data = msg.get('data')
        size = msg.get('size', 0)
        with self.pending_downloads_lock:
            save_path = self.pending_shared_downloads.pop(fname, None)
        if not save_path:
            self.append_group_msg(f"⚠️ 收到未请求的文件 {fname}，已忽略", system=True)
            return

        def save_in_background():
            try:
                binary_data = base64.b64decode(data)
                with open(save_path, 'wb') as f:
                    f.write(binary_data)
                self.msg_queue.put({
                    'type': '_download_complete', 'fname': fname,
                    'size': size, 'save_path': save_path
                })
            except Exception as e:
                self.msg_queue.put({'type': '_download_error', 'error': str(e)})

        threading.Thread(target=save_in_background, daemon=True).start()

    def _handle_download_complete(self, msg):
        fname = msg['fname']
        size = msg['size']
        save_path = msg['save_path']
        self.append_group_msg(f"✅ 文件 {fname} 下载完成！({size}字节)", system=True)
        messagebox.showinfo("下载完成", f"文件 {fname} 已保存到：\n{save_path}")

    def _handle_download_error(self, msg):
        messagebox.showerror("下载失败", msg['error'])

    def _handle_shared_file_start(self, msg):
        """开始接收共享文件（分块下载）"""
        fname = msg.get('filename', '')
        total_size = msg.get('total_size', 0)

        # 保存路径必须由用户在主线程预先选择（download_selected_file）
        with self.pending_downloads_lock:
            save_path = self.pending_shared_downloads.pop(fname, None)

        if not save_path:
            # 非主动下载（或已取消）：丢弃本次传输，不弹窗、不落盘
            self.append_group_msg(f"⚠️ 收到未请求的文件 {fname}，已忽略", system=True)
            return

        # 同名传输未结束时先清理旧句柄
        old = self._shared_download.pop(fname, None)
        if old:
            try:
                old['fp'].close()
            except Exception:
                pass

        try:
            fp = open(save_path, 'wb')
        except OSError as e:
            self.append_group_msg(f"❌ 创建文件失败: {e}", system=True)
            return

        self._shared_download[fname] = {
            'fp': fp,
            'total_size': total_size,
            'received': 0,
            'next_index': 0,
            'last_reported': -1,
            'save_path': save_path,
            'hasher': hashlib.sha256(),
            'expected_hash': (msg.get('sha256') or '')
        }
        self.append_group_msg(f"开始下载 {fname} ({total_size} 字节)", system=True)

    def _handle_shared_file_chunk(self, msg):
        """接收共享文件分块（校验顺序与完整性）"""
        fname = msg.get('filename', '')
        info = self._shared_download.get(fname)
        if not info:
            return
        try:
            index = msg.get('index', info['next_index'])
            if index != info['next_index']:
                raise ValueError(f"分块序号错乱（期望 {info['next_index']}，收到 {index}）")

            chunk_data = base64.b64decode(msg.get('data', ''))
            info['fp'].write(chunk_data)
            info['received'] += len(chunk_data)
            info['next_index'] += 1
            info['hasher'].update(chunk_data)

            if info['total_size'] > 0:
                percent = int(info['received'] * 100 / info['total_size'])
                if percent // 10 > info['last_reported'] // 10:
                    info['last_reported'] = percent
                    self.append_group_msg(f"下载进度: {percent}%", system=True)
        except Exception as e:
            self.append_group_msg(f"❌ 接收分块失败: {e}", system=True)
            self._abort_shared_download(fname)

    def _handle_shared_file_end(self, msg):
        """共享文件下载完成：校验大小后交付"""
        fname = msg.get('filename', '')
        info = self._shared_download.pop(fname, None)
        if not info:
            return
        try:
            info['fp'].close()
        except Exception:
            pass

        expected = msg.get('total_size', info['total_size'])
        if expected and info['received'] != expected:
            self.append_group_msg(
                f"❌ 文件 {fname} 不完整（收到 {info['received']} / 期望 {expected} 字节）", system=True)
            try:
                os.remove(info['save_path'])
            except OSError:
                pass
            self._ui_msg("下载失败", f"文件 {fname} 传输不完整，已删除")
            return

        # 内容完整性校验：与服务端/源文件比对 SHA-256
        expected_hash = msg.get('sha256') or info.get('expected_hash') or ''
        if expected_hash:
            actual = info['hasher'].hexdigest()
            if actual != expected_hash.lower():
                self.append_group_msg(f"❌ 文件 {fname} 校验失败（哈希不匹配），已删除", system=True)
                try:
                    os.remove(info['save_path'])
                except OSError:
                    pass
                self._ui_msg("下载失败", f"文件 {fname} 内容校验失败，可能已损坏，已删除")
                return

        self.append_group_msg(f"✅ 共享文件 {fname} 下载完成！({info['received']} 字节)", system=True)
        self._ui_msg("下载完成", f"文件 {fname} 已保存到：\n{info['save_path']}")

    def _abort_shared_download(self, fname):
        """中断并清理某个共享文件下载会话"""
        info = self._shared_download.pop(fname, None)
        if not info:
            return
        try:
            info['fp'].close()
        except Exception:
            pass

    # ============ 私聊 ============
    def private_chat_selected(self):
        sel = self.user_listbox.curselection()
        if sel:
            target_raw = self.user_listbox.get(sel[0])
            # 提取纯用户名（去掉MAC/未读计数等后缀）
            target = self._pure_username(target_raw)
            if target == self.username:
                messagebox.showinfo("提示", "不能和自己聊天")
                return
            self.open_private_chat_by_name(target)
        else:
            messagebox.showinfo("提示", "请先选择要私聊的用户")

    def open_private_chat(self, event):
        self.private_chat_selected()

    def open_private_chat_by_name(self, target):
        # 确保target是纯用户名
        pure_target = self._pure_username(target)
        if not pure_target:
            return

        # 锁顺序：private_windows_lock → private_unread_lock
        # 先在锁内判断窗口是否可用并取出引用，随即释放锁再操作窗口。
        existing = None
        need_create = False
        with self.private_windows_lock:
            entry = self.private_windows.get(pure_target)
            if entry is not None:
                try:
                    if entry.window.winfo_exists():
                        existing = entry
                    else:
                        self.private_windows.pop(pure_target, None)
                        need_create = True
                except Exception:
                    self.private_windows.pop(pure_target, None)
                    need_create = True
            else:
                need_create = True

        if existing is not None:
            try:
                existing.window.lift()
                existing.load_history()
            except Exception:
                pass

        with self.private_unread_lock:
            if pure_target in self.private_unread:
                self.private_unread[pure_target] = 0
        self.update_window_title()

        if need_create:
            with self.private_windows_lock:
                # 双重检查：避免并发下重复创建
                if pure_target in self.private_windows:
                    return
                win = tk.Toplevel(self.root)
                self.private_windows[pure_target] = PrivateChatWindow(win, pure_target, self)
    # ============ 文件传输 ============
    def handle_file_req(self, msg):
        from_u = msg.get('from', '未知')
        fname = msg.get('filename', '未知文件')
        fsize = msg.get('size', 0)
        tid = msg.get('transfer_id', '')
        if not tid:
            return
        with self.file_transfers_lock:
            if tid in self.file_transfers:
                self.send_msg({
                    'type': 'file_response', 'to': from_u, 'transfer_id': tid,
                    'accepted': False, 'from': self.username
                })
                return
        ok = messagebox.askyesno("文件接收请求",
                                 f"{from_u} 发送文件：\n文件名：{fname}\n大小：{fsize}字节 ({fsize / 1024:.2f}KB)\n是否接收？")
        save_path = ""
        if ok:
            save_path = filedialog.asksaveasfilename(title="保存文件", initialfile=fname)
            if not save_path:
                ok = False
        self.send_msg({
            'type': 'file_response', 'to': from_u, 'transfer_id': tid,
            'accepted': ok, 'from': self.username
        })
        if ok:
            try:
                fp = open(save_path, 'wb')
                with self.file_transfers_lock:
                    self.file_transfers[tid] = {
                        'type': 'recv', 'from': from_u, 'save': save_path,
                        'fp': fp, 'received_chunks': 0, 'active': True,
                        'expected_index': 0, 'failed': False,
                        'expected_total': msg.get('total_chunks', 0),
                        'name': fname,
                        'hasher': hashlib.sha256()
                    }
                self.append_group_msg(f"📥 [文件] 开始接收 {from_u} 的文件 {fname}", system=True)
            except Exception as e:
                with self.file_transfers_lock:
                    if tid in self.file_transfers:
                        del self.file_transfers[tid]
                self.append_group_msg(f"❌ [文件] 接收文件失败：{str(e)}", system=True)

    def handle_file_resp(self, msg):
        tid = msg.get('transfer_id', '')
        if not tid:
            return
        accept = msg.get('accepted', False)
        from_u = msg.get('from', '未知')
        with self.file_transfers_lock:
            info = self.file_transfers.get(tid)
            if not info or info['type'] != 'send':
                return
            if accept:
                info['active'] = True
                self.append_group_msg(f"📤 [文件] {from_u} 同意接收，正在发送...", system=True)
                threading.Thread(target=self.send_file_chunk_task, args=(tid,), daemon=True).start()
            else:
                self.append_group_msg(f"❌ [文件] {from_u} 拒绝接收文件", system=True)
                del self.file_transfers[tid]

    def send_file_chunk_task(self, tid):
        with self.file_transfers_lock:
            info = self.file_transfers.get(tid)
            if not info or info['type'] != 'send':
                return
            path = info['path']
            target = info['target']
            chunk_size = info['chunk']
            total_chunks = info['total_chunks']
            fname = info.get('name', '')
            hasher = hashlib.sha256()
        try:
            with open(path, 'rb') as f:
                idx = 0
                while True:
                    data = f.read(chunk_size)
                    if not data:
                        break
                    hasher.update(data)
                    b64 = base64.b64encode(data).decode('utf-8')
                    success = self.send_msg({
                        'type': 'file_chunk', 'to': target, 'transfer_id': tid,
                        'chunk_index': idx, 'total_chunks': total_chunks,
                        'data': b64, 'from': self.username
                    })
                    if not success:
                        raise Exception("发送分片失败，网络连接中断")
                    idx += 1
                    if idx % 10 == 0:
                        progress = (idx / total_chunks) * 100
                        self.append_group_msg(f"📤 [文件] 发送进度: {progress:.1f}%", system=True)
                    if idx % 5 == 0:
                        time.sleep(0.01)
                self.send_msg({
                    'type': 'file_end', 'to': target, 'transfer_id': tid,
                    'total_chunks': idx, 'sha256': hasher.hexdigest(),
                    'from': self.username
                })
                self.append_group_msg(f"✅ [文件] {fname} 发送完成！({idx}个分片)", system=True)
        except Exception as e:
            self.append_group_msg(f"❌ [文件] 发送失败：{str(e)}", system=True)
            # 通知接收端中止，避免对方一直等一个永远不会到的 file_end
            self.send_msg({
                'type': 'file_error', 'to': target, 'transfer_id': tid,
                'reason': f'发送端出错：{e}'
            })
        finally:
            with self.file_transfers_lock:
                if tid in self.file_transfers:
                    del self.file_transfers[tid]

    def handle_chunk(self, msg):
        tid = msg.get('transfer_id', '')
        with self.file_transfers_lock:
            info = self.file_transfers.get(tid)
            if not info or info['type'] != 'recv' or not info.get('active', False):
                return
            fp = info.get('fp')
            if not fp:
                return
            try:
                idx = msg.get('chunk_index', info.get('expected_index', 0))
                if idx != info.get('expected_index', 0):
                    raise ValueError(
                        f"分片序号错乱（期望 {info.get('expected_index', 0)}，收到 {idx}）")
                binary = base64.b64decode(msg['data'])
                fp.write(binary)
                info['received_chunks'] = info.get('received_chunks', 0) + 1
                info['expected_index'] = idx + 1
                info['hasher'].update(binary)
            except Exception as e:
                self.append_group_msg(f"❌ [文件] 接收分片失败：{str(e)}", system=True)
                info['active'] = False
                info['failed'] = True
                try:
                    fp.close()
                except Exception:
                    pass
                info['fp'] = None

    def handle_file_finish(self, msg):
        tid = msg.get('transfer_id', '')
        with self.file_transfers_lock:
            info = self.file_transfers.get(tid)
            if not info or info['type'] != 'recv':
                return
            try:
                fp = info.get('fp')
                if fp:
                    fp.close()
                    info['fp'] = None

                # 中途中断的传输：告知发送方并清理半截文件
                if info.get('failed'):
                    self.send_msg({
                        'type': 'file_error', 'to': info.get('from'),
                        'transfer_id': tid, 'reason': '接收端校验失败，已中止'
                    })
                    try:
                        if os.path.exists(info['save']):
                            os.remove(info['save'])
                    except OSError:
                        pass
                    self.append_group_msg(f"❌ [文件] 接收中断，已删除不完整文件 {info.get('name', '')}",
                                          system=True)
                    return

                # 分片数量校验
                declared = msg.get('total_chunks', 0)
                got = info.get('received_chunks', 0)
                if declared and got != declared:
                    self.send_msg({
                        'type': 'file_error', 'to': info.get('from'),
                        'transfer_id': tid, 'reason': f'分片数量不符（{got}/{declared}）'
                    })
                    try:
                        if os.path.exists(info['save']):
                            os.remove(info['save'])
                    except OSError:
                        pass
                    self.append_group_msg(
                        f"❌ [文件] 分片数量不符（{got}/{declared}），已删除不完整文件", system=True)
                    return

                # 内容完整性校验（可选：旧版发送方不带哈希时跳过）
                declared_hash = msg.get('sha256', '')
                if declared_hash:
                    actual = info['hasher'].hexdigest()
                    if actual != declared_hash.lower():
                        self.send_msg({
                            'type': 'file_error', 'to': info.get('from'),
                            'transfer_id': tid, 'reason': '内容校验失败（哈希不匹配）'
                        })
                        try:
                            if os.path.exists(info['save']):
                                os.remove(info['save'])
                        except OSError:
                            pass
                        self.append_group_msg(f"❌ [文件] 内容校验失败，已删除 {info.get('name', '')}",
                                              system=True)
                        return

                file_size = os.path.getsize(info['save'])
                self.append_group_msg(f"✅ [文件] 文件接收完毕！大小：{file_size}字节", system=True)
                messagebox.showinfo("文件接收完成", f"文件已保存到：\n{info['save']}")
            except Exception as e:
                self.append_group_msg(f"❌ [文件] 完成处理失败：{str(e)}", system=True)
            finally:
                with self.file_transfers_lock:
                    if tid in self.file_transfers:
                        del self.file_transfers[tid]

    def handle_file_error(self, msg):
        """对端报告传输失败：中止本地传输并清理半截文件"""
        tid = msg.get('transfer_id', '')
        reason = msg.get('reason', '对端中止了传输')
        if not tid:
            return

        with self.file_transfers_lock:
            info = self.file_transfers.get(tid)
            if not info:
                return
            fp = info.get('fp')
            if fp:
                try:
                    fp.close()
                except Exception:
                    pass
                info['fp'] = None
            info['active'] = False
            save_path = info.get('save')
            fname = info.get('name', '')
            kind = info.get('type')

        if kind == 'recv' and save_path:
            try:
                if os.path.exists(save_path):
                    os.remove(save_path)
            except OSError:
                pass
            self.append_group_msg(f"❌ [文件] 传输被中止：{reason}（已删除不完整文件）", system=True)
        else:
            self.append_group_msg(f"❌ [文件] 传输被中止：{reason}", system=True)

        with self.file_transfers_lock:
            if tid in self.file_transfers:
                del self.file_transfers[tid]

    # ============ 关闭 ============
    def on_close(self):
        self.connected = False
        self.username = None  # 阻止退出期间触发重连
        self._save_draft()

        # 关闭所有私聊窗口
        with self.private_windows_lock:
            for win in list(self.private_windows.values()):
                try:
                    if win.window.winfo_exists():
                        win.window.destroy()
                except Exception:
                    pass
            self.private_windows.clear()

        # 关闭共享文件窗口
        if self.shared_files_window:
            try:
                if self.shared_files_window.winfo_exists():
                    self.shared_files_window.destroy()
            except Exception:
                pass
            self.shared_files_window = None

        self._stop_flash()

        # 关闭文件传输句柄
        with self.file_transfers_lock:
            for info in self.file_transfers.values():
                fp = info.get('fp')
                if fp:
                    try:
                        fp.close()
                    except Exception:
                        pass
            self.file_transfers.clear()

        # 关闭未完成的共享文件下载句柄
        for fname in list(self._shared_download.keys()):
            self._abort_shared_download(fname)
        with self.pending_downloads_lock:
            self.pending_shared_downloads.clear()

        # 关闭 socket（获取 send_lock 防止与 send_msg 竞态）
        with self.send_lock:
            sock = self.sock
            self.sock = None
        if sock:
            try:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                sock.close()
            except Exception:
                pass

        self.root.destroy()


class PrivateChatWindow:
    def __init__(self, window, target, client):
        self.window = window
        self.target = target
        self.client = client
        self.window.title(f"💬 {target}")
        self.window.geometry("450x550")
        self.window.minsize(400, 450)
        self.pop_on_message = tk.BooleanVar(value=True)
        self.setup_ui()
        self.load_history()
        self.window.bind('<FocusIn>', self.on_focus_in)
        self.window.protocol("WM_DELETE_WINDOW", self.close_win)

    def setup_ui(self):
        self.window.grid_rowconfigure(0, weight=0)
        self.window.grid_rowconfigure(1, weight=1)
        self.window.grid_rowconfigure(2, weight=0)
        self.window.grid_columnconfigure(0, weight=1)
        top_frame = tk.Frame(self.window)
        top_frame.grid(row=0, column=0, sticky='ew', padx=5, pady=5)
        top_frame.grid_columnconfigure(0, weight=1)
        top_frame.grid_columnconfigure(1, weight=0)
        tk.Label(top_frame, text=f"💬 与 {self.target} 私聊中",
                 font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, sticky='w')
        tk.Checkbutton(top_frame, text="来消息时弹出到最前",
                       variable=self.pop_on_message,
                       font=("Microsoft YaHei", 9)).grid(row=0, column=1, sticky='e')
        self.chat_box = scrolledtext.ScrolledText(self.window, state='disabled',
                                                  font=("Microsoft YaHei", 10))
        self.chat_box.grid(row=1, column=0, sticky='nsew', padx=5, pady=5)
        self.chat_box.tag_config('self', foreground='#2196F3', font=("Microsoft YaHei", 10, "bold"))
        self.chat_box.tag_config('other', foreground='#333333', font=("Microsoft YaHei", 10))
        input_frame = tk.Frame(self.window)
        input_frame.grid(row=2, column=0, sticky='ew', padx=5, pady=5)
        input_frame.grid_columnconfigure(0, weight=1)
        input_frame.grid_columnconfigure(1, weight=0)
        self.input = tk.Text(input_frame, height=4, font=("Microsoft YaHei", 10))
        self.input.grid(row=0, column=0, sticky='ew', padx=(0, 5))
        self.input.bind('<Return>', self._on_input_return)
        tk.Button(input_frame, text="发送", width=8, height=2,
                  command=self.send, font=("Microsoft YaHei", 9, "bold"),
                  bg="#4CAF50", fg="white").grid(row=0, column=1, sticky='nsew')

    def _on_input_return(self, event):
        if event.state & 0x4:
            self.input.insert(tk.INSERT, '\n')
            return "break"
        else:
            self.send()
            return "break"

    def on_focus_in(self, event):
        # 窗口获得焦点时清零未读，同时停止任务栏闪烁
        cleared = False
        with self.client.private_unread_lock:
            if self.client.private_unread.get(self.target, 0) > 0:
                self.client.private_unread[self.target] = 0
                cleared = True
        if cleared:
            self.client.update_window_title()
        if not self.client.private_unread_total():
            self.client._stop_flash()

    def load_history(self):
        try:
            self.chat_box.config(state='normal')
            self.chat_box.delete(1.0, tk.END)
            with self.client.private_messages_lock:
                if self.target in self.client.private_messages:
                    for msg in self.client.private_messages[self.target]:
                        sender = msg['sender']
                        content = msg['content']
                        timestamp = msg.get('time', '')
                        if sender == self.client.username:
                            self.chat_box.insert(tk.END, f"[{timestamp}] [我] {content}\n", 'self')
                        else:
                            self.chat_box.insert(tk.END, f"[{timestamp}] [{sender}] {content}\n", 'other')
                    self.chat_box.see(tk.END)
            self.chat_box.config(state='disabled')
        except Exception:
            pass

    def pop_to_front(self):
        try:
            if self.window.state() == 'iconic':
                self.window.deiconify()
            self.window.lift()
            self.window.attributes('-topmost', True)
            self.window.after(200, lambda: self.window.attributes('-topmost', False))
        except:
            pass

    def add_msg(self, sender, text, from_server=False):
        timestamp = datetime.now().strftime('%H:%M:%S')
        with self.client.private_messages_lock:
            if self.target not in self.client.private_messages:
                self.client.private_messages[self.target] = []
            if len(self.client.private_messages[self.target]) >= self.client.max_private_messages:
                self.client.private_messages[self.target].pop(0)
            self.client.private_messages[self.target].append({
                'time': timestamp, 'sender': sender, 'content': text
            })
        try:
            self.chat_box.config(state='normal')
            if sender == self.client.username:
                self.chat_box.insert(tk.END, f"[{timestamp}] [我] {text}\n", 'self')
            else:
                self.chat_box.insert(tk.END, f"[{timestamp}] [{sender}] {text}\n", 'other')
                if from_server and self.pop_on_message.get():
                    self.pop_to_front()
            self.chat_box.see(tk.END)
            self.chat_box.config(state='disabled')
        except Exception:
            pass

    def send(self, event=None):
        if not self.client.connected:
            messagebox.showwarning("提示", "未连接到服务器")
            return "break"

        if self.client.muted:
            remaining = max(0, int((self.client.muted_until - time.time()) // 60))
            if remaining > 0:
                messagebox.showwarning("禁言中", f"您当前处于禁言状态，剩余 {remaining} 分钟")
            else:
                messagebox.showwarning("禁言中", "您当前处于禁言状态")
            return "break"

        txt = self.input.get('1.0', tk.END).strip()
        if txt:
            if len(txt) > MAX_MESSAGE_LENGTH:
                messagebox.showwarning(
                    "提示", f"消息过长（{len(txt)} 字符），上限为 {MAX_MESSAGE_LENGTH} 字符")
                return "break"
            if self.client.send_msg({
                'type': 'private_message', 'to': self.target, 'content': txt
            }):
                self.add_msg(self.client.username, txt, from_server=False)
                self.input.delete('1.0', tk.END)
            else:
                messagebox.showerror("发送失败", "消息发送失败，请检查网络连接")
        return "break"

    def close_win(self):
        # 锁顺序与 handle_private_message / open_private_chat_by_name 保持一致：
        # private_windows_lock → private_unread_lock，且不跨锁嵌套
        with self.client.private_windows_lock:
            self.client.private_windows.pop(self.target, None)
        with self.client.private_unread_lock:
            if self.target in self.client.private_unread:
                self.client.private_unread[self.target] = 0
        self.client.update_window_title()
        try:
            self.window.destroy()
        except Exception:
            pass


if __name__ == '__main__':
    root = tk.Tk()
    app = Client(root)
    root.mainloop()