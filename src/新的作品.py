import os
from datetime import datetime, timedelta

# ====================== 配置区（自行修改）======================
scan_path = r"C:\\"          # 扫描盘符
recent_hours = 6             # 查找几小时内新建的文件
min_size_mb = 500           # 只显示大于该MB的文件
skip_folders = [            # 跳过系统保护目录，防止报错、提速
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\Recovery",
    r"C:\Boot"
]
# ==============================================================

min_size_byte = min_size_mb * 1024 * 1024
time_threshold = datetime.now() - timedelta(hours=recent_hours)
result_list = []

def is_skip(path):
    """判断是否需要跳过该目录"""
    for skip_dir in skip_folders:
        if path.startswith(skip_dir):
            return True
    return False

def scan_dir(root):
    """递归扫描文件夹"""
    if is_skip(root):
        return
    try:
        for entry in os.scandir(root):
            if entry.is_dir(follow_symlinks=False):
                scan_dir(entry.path)
            else:
                try:
                    stat_info = entry.stat()
                    create_time = datetime.fromtimestamp(stat_info.st_ctime)
                    file_size = stat_info.st_size
                    # 判断时间+大小条件
                    if create_time >= time_threshold and file_size >= min_size_byte:
                        size_gb = round(file_size / (1024**3), 2)
                        result_list.append((size_gb, entry.path, create_time))
                except Exception:
                    continue
    except PermissionError:
        return
    except Exception:
        return

if __name__ == "__main__":
    print(f"开始扫描路径：{scan_path}")
    print(f"筛选条件：{recent_hours}小时内新建、大于{min_size_mb}MB文件")
    print("正在扫描，耗时较久请等待...\n")
    scan_dir(scan_path)

    # 按文件大小倒序排序
    result_list.sort(reverse=True, key=lambda x: x[0])

    print(f"共找到 {len(result_list)} 个符合条件的大文件：")
    print("-" * 80)
    print(f"{'大小(GB)':<10}{'创建时间':<22}{'文件路径'}")
    print("-" * 80)
    for size, path, ctime in result_list:
        print(f"{size:<10}{ctime.strftime('%Y-%m-%d %H:%M:%S'):<22}{path}")