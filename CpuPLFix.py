# -*- coding: utf-8 -*-
"""
CpuPLFix —— 修复开机 / 睡眠唤醒后 CPU 被锁在 0.55GHz 的问题,
            按 AC / 电池自动切换功耗墙(插电高性能 / 电池平衡),
            另带 EC 风扇曲线 与 过热保护(防止摸到固件 _HOT 阈值被强制休眠)

原理
----
Intel 平台的封装功耗墙(PL1/PL2)同时存在于两处, CPU 实际执行的是两者的 min:
  1) MSR 0x610  (PKG_POWER_LIMIT)         —— CPU 内部, 操作系统/ThrottleStop 用这一份
  2) MCHBAR+0x59A0 (MMIO 镜像)            —— 主板/EC 固件用的那一份, bit63 是锁定位

本机实测(故障状态): 固件会在开机/唤醒时把 MMIO 那份打成 5~21W, CPU 于是被钉在最低频。

两种工作模式(mmio_mode):
  disable  —— 开机把 MMIO "清零 + 锁死"(等于把固件侧永久禁用), 之后所有档位只写 MSR。
               实测本机 MSR 0x610 可写。改档/改配置/拔插电都不需要重启。
  value    —— 老方案: 把目标值写进 MMIO 再锁。改配置需重启; MSR 不可写的机器用它兜底。

默认按当前电源状态选档: 插电 -> pl1_w/pl2_w (高性能); 电池 -> pl1_w_batt/pl2_w_batt (平衡)。
守护服务(CpuPLFixSvc)在开机 / 睡眠唤醒 / 拔插电时 1 秒内对齐目标状态, 顺带管风扇与过热保护。

风扇: 通过同一个驱动的端口 I/O 读写 EC, 按 config 里的温度曲线写 HFSP 档位(原生自动曲线到不了满速)。
过热保护: 本机固件 _TZ.THM0 的 _HOT=401K(≈127.9°C), 摸到就强制休眠(S4)。查本机 DSDT 反汇编
          DSDT 里 \\_TZ.THM0._TMP 的反汇编后确认: 真正触发它的是 **EC 自己的过热标志** 0x89/0x8A/0x8B ——
          这三者低 7 位有任一非零, _TMP 就直接返回 _HOT; 而温度寄存器 0x78 只是平时报给 OS
          的读数, 它的量程根本够不到 _HOT(0x80 还是"无效"哨兵值, AML 会当成 48°C)。
          所以本工具两条线一起看:
            1) 温度 0x78 分三级, 提前砍功耗墙 + 风扇拉满(预防, 别等固件动手);
            2) 固件标志一置位 -> 最高级(说明固件已经在报 _HOT, 可选立刻主动优雅休眠)。

底层通过 ThrottleStop 自带的内核驱动访问硬件(该驱动已由作者签名, 本机原本就能加载),
所以不需要额外安装任何第三方驱动。
"""

import ctypes
import json
import os
import struct
import subprocess
import sys
import time
import datetime
from ctypes import wintypes as w

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, 'config.json')
LOG_DIR = os.path.join(BASE, 'logs')
SYS_SRC = os.path.join(BASE, 'TSCtl.sys')
SYS_DST = os.path.join(BASE, 'TSCtl.sys')
SVC = 'TSCtl'
SVC_TOOL = 'CpuPLFixSvc'
TASK_NAME = 'CpuPLFix'
BOOT_WATCH_SEC = 300         # 开机/唤醒后看守多久(秒)
LOG_MAX_BYTES = 2 * 1024 * 1024   # 日志超过 2MB 就滚动成 fix.log.1(只留一份)
MIN_PL_W = 5.0               # 手动指定功耗墙时的安全下限, 低于它机器会慢到没法用
MCHBAR_REG = 0x48
MMIO_PL_OFF = 0x59A0
MSR_PKG_POWER_LIMIT = 0x610
MSR_PERF_STATUS = 0x198

# ThrottleStop 驱动的 IOCTL 码(由驱动分发表逆向得到)
IOCTL_VERSION = 0x80006494
IOCTL_RDMSR = 0x80006448
IOCTL_WRMSR = 0x8000644C
IOCTL_READ_PHYS = 0x80006498
IOCTL_WRITE_PHYS = 0x8000649C
IOCTL_PCI_READ = 0x800064A0

DEFAULT_CFG = {
    "pl1_w": 0,
    "pl2_w": 0,
    "pl1_w_batt": 0,
    "pl2_w_batt": 0,
    "pl1_time": 35,
    "pl2_time": 66,
    "min_pl1_w": 15.0,
    "lock_mmio": True,
    "mmio_mode": "disable",
    # ---- 轮询节奏: EC 是慢速总线(实测还会串位/超时), 能少打扰就少打扰 ----
    # 主循环只对齐功耗墙/电源档(MSR/MMIO, 不碰 EC), 所以可以快; EC 读(温度+风扇档位)
    # 放到 15 秒一次 —— 温度是慢变量, 风扇本身响应也是几十秒级, 15 秒足够。
    # 开机/唤醒后前 5 分钟、过热保护生效期间、温度高过 guard.flag_hot_c 时都会自动加密。
    "poll_s": 5.0,               # 主循环间隔(秒)
    "ec_poll_s": 15.0,           # EC 读间隔(秒): 温度 + 风扇档位
    "flag_poll_s": 30.0,         # 固件过热标志(0x89/0x8A/0x8B)读间隔(秒)
    # ---- 风扇(可选, 本机实测有效; 其他机型没有 EC 寄存器 0x2F 就自动跳过) ----
    "fan": {
        "mode": "curve",        # curve = 按温度曲线自动 / max = 满速 / off = 交还 EC 自动 / level = 固定档
        "level": 0,             # mode=level 时用: 1~7 是 EC 固定档, 64 是满速
        # 三档: ≤62°C 静音(2 档) / 63~75°C 平衡(6 档) / ≥76°C 强冷(满速)
        "curve": [[63, 2], [76, 6], [999, 64]],
        "hyst": 5.0,            # 降档回差(°C), 防止温度在档位边界来回抖
        # ★降档最短驻留(秒, 2026-10-19 加): 只压"往低档退", 升档照旧立即生效;
        #   温度掉到"目标段边界 - 10°C"以下时立即放行(待机该静音还是静音)。0 = 关。
        "min_dwell_s": 60.0,
        # ★负载前馈(2026-10-19 加): 只按 EC 温度升档会"慢半拍" —— 0x78 是另一路传感器,
        #   比核心真实温度滞后, 实测 100% 负载时风扇还停在 6 档, 等 EC 报 98°C 才拉满,
        #   然后温度砸下去、风扇停、再冲上来。改为: 持续高占用就把风扇提前拉起来。
        "load_boost_pct": 70.0,     # 占用 >= 它算"高负载"
        "load_boost_s": 20.0,       # 连续高负载满这么久才拉(躲开短时突发)
        "load_hold_s": 30.0,        # 负载落下后再多吹这么久(温度还没下来)
        "load_boost_level": 64      # 拉到的档位(64=满速); 0 = 关掉前馈
    },
    # ---- 过热保护: 防止跑到固件的 _HOT 阈值被强制休眠 ----
    "guard": {
        "enabled": True,
        "warn_c": 95.0,         # EC 0x78 到它就压功耗墙(预防档)
        "crit_c": 98.0,         # EC 0x78 到它压到最低档
        "escape_c": 105.0,      # 逃生阈值: 到它就(可选)主动休眠
        "recover_c": 78.0,      # 降到它以下才解除保护
        # 温控档的功耗 = 插电档的 50% / 25%(PL1 与 PL2 同比例缩), 两条线保持一套。
        # 95/98 的依据: 本机 40W 满载+满速风平台 ≈92°C; CPU 自己 TjMax=100°C, 实测约 99°C
        # 撞温度墙。95 只比正常平台高 3°C(环境热/堵风道才碰), 98 卡在 CPU 自己动手前面。
        # 93 太低 —— 会跟正常满载打架(持续满载早晚被压到 20W)。
        "warn_pl1_w": 20.0,
        "warn_pl2_w": 28.0,
        "crit_pl1_w": 10.0,
        "crit_pl2_w": 14.0,
        "dts_crit_c": 97.0,     # CPU DTS 单独触发"严重"档(仅 EC 温度读不到时兜底)
        "flag_hot_c": 85.0,     # EC 温度到它就恢复"每次 EC 轮询都读固件标志"(平时 30 秒一次)
        "force_fan_max": True,  # 保护期间强制风扇满速
        "trip_flags": True,     # EC 0x89/0x8A/0x8B 一置位就是最高级(固件真判据)
        # ★2026-10-19: 标志置位**不等于现在热** —— 实测 11:04 时 0x89=0x01 而 EC 58°C/
        #   DTS 62°C/倍频 43(4.3GHz)/占用 10%; 01:03~04:28 那次在 36~51°C 挂了 3 小时。
        #   它是固件"记录过一次过热"的锁存位。所以温度正常时不再砍到 crit(10W):
        #     flag_pl1_w/flag_pl2_w = 标志置位但温度正常时用的档(默认 = warn 档 20/28W)+风扇满速
        #     trip_hold_s          = 这么凉还挂着满这么多秒 -> 判"锁存", 放开功耗墙
        #     min_hold_s           = 进入保护后至少撑这么久(防 40W↔10W 十几秒来回切)
        #     release_dwell_s      = 温度要连续 <= recover_c 这么久才真解除
        "flag_pl1_w": 20.0,
        "flag_pl2_w": 28.0,
        "trip_hold_s": 45.0,
        "min_hold_s": 30.0,
        "release_dwell_s": 10.0,
        "escape_hibernate": False
    },
    "comment": "pl1_w/pl2_w = 插电(高性能)档, pl1_w_batt/pl2_w_batt = 电池(平衡)档。mmio_mode: disable = MMIO清零+锁后只写MSR(免重启切档, 实测本机MSR可写时推荐); value = 写目标值进MMIO+锁(老方案, 改配置要重启)。poll_s/ec_poll_s/flag_poll_s = 主循环/EC读/固件标志读的间隔秒数(EC 是慢总线, 默认 15/30 秒)。fan = 风扇模式/温度曲线; guard = 过热保护阈值(本机 _TZ.THM0 _HOT=401K 会强制休眠, 所以要这条线)。"
}

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.CreateFileW.restype = w.HANDLE
k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
k32.DeviceIoControl.restype = w.BOOL
k32.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, w.DWORD,
                                ctypes.POINTER(w.DWORD), w.LPVOID]
k32.GetConsoleWindow.restype = w.HWND
k32.GetTickCount64.restype = ctypes.c_ulonglong

_T_IMPORT = time.time()          # 解释器启动到能执行到这里的时间


def uptime():
    """系统已运行秒数(用于判断任务到底有多晚才被触发)"""
    try:
        return k32.GetTickCount64() / 1000.0
    except Exception:
        return -1.0


def unbiased_uptime():
    """系统"清醒"运行秒数(不含睡眠) —— QueryUnbiasedInterruptTime 的官方口径。

    GetTickCount64 在睡眠期间到底走不走, 各 Windows 版本/机型不一致, 所以只靠它
    分不清"睡醒"和"计划任务 5 分钟一次的重复"。拿不到这个 API 就返回 None。
    """
    try:
        v = ctypes.c_ulonglong(0)
        if not k32.QueryUnbiasedInterruptTime(ctypes.byref(v)):
            return None
        return v.value / 1e7
    except Exception:
        return None


BOOT_STATE = os.path.join(LOG_DIR, 'boot_last.txt')   # 上一次 boot 时的 (墙钟, 清醒秒)


def boot_trigger(up):
    """判断这次 cmd_boot 是被哪种情况叫起来的 -> (标签, 是否"安静运行")。

    计划任务在开机后 30 分钟内会每 5 分钟重复叫一次，同一个入口也服务"睡醒"，
    所以必须分清：'开机' / '开机后复查' / '睡眠/休眠唤醒后' / '定时复查'。
    判据: 两次运行之间"墙钟走的秒数 − 清醒走的秒数" ≈ 睡掉的秒数
          （清醒秒数用 QueryUnbiasedInterruptTime，官方口径不含睡眠）。

    每次调用都会把 (墙钟, 清醒秒) 落到 BOOT_STATE —— 包括开机那次，
    否则开机后的第一个"5 分钟重复"会拿到上一次开机的旧值，判不准。
    返回的 quiet=True 表示"重复触发且没睡过" —— 这种没事就不用往日志里刷两行。
    """
    now_w, now_u = time.time(), unbiased_uptime()
    last_w = last_u = None
    try:
        with open(BOOT_STATE, encoding='utf-8') as f:
            last_w, last_u = [float(x) for x in f.read().split()]
    except Exception:
        pass
    if now_u is not None:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(BOOT_STATE, 'w', encoding='utf-8') as f:
                f.write('%.3f %.3f' % (now_w, now_u))
        except Exception:
            pass

    tag, quiet = ('开机' if up < 600 else '唤醒/复查'), False
    if last_w is not None and now_u is not None and last_u is not None:
        d_u = now_u - last_u
        # QueryUnbiasedInterruptTime 只有 ~15 ms 粒度, 两次挨得很近时 d_u 可能是负的,
        # 所以允许一点负抖动; 真正重启会让 d_u 变成很大的负数(那时 up 也 < 600)。
        if d_u > -2.0:
            slept = (now_w - last_w) - max(0.0, d_u)
            if slept > 60:                      # 真睡过
                tag = '睡眠/休眠唤醒后, 睡掉 %.0f 秒' % slept
            elif up < 600:                      # 开机后计划任务的 5 分钟重复
                tag, quiet = '开机后复查', True
            else:
                tag, quiet = '定时复查', True
    return tag, quiet


_no_log = False          # True = 只打屏幕、不进日志缓冲（体检的输出太啰嗦，不该灌进 fix.log）

_lines = []


def out(msg=''):
    try:
        if sys.stdout is not None:      # pythonw.exe 下 stdout 为 None
            print(msg)
    except Exception:
        pass
    if not _no_log:
        _lines.append(msg)


def _log_line(text, tag='check'):
    """直接往 fix.log 追加一条记录（绕过 _lines 缓冲）。
    给那些"只上屏幕"的命令留个痕迹 —— 否则日志里完全看不出你什么时候体检过。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        p = os.path.join(LOG_DIR, 'fix.log')
        _rotate_log(p)
        with open(p, 'a', encoding='utf-8') as f:
            f.write('\n===== %s %s =====\n%s\n'
                    % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), tag, text))
    except Exception:
        pass


def _rotate_log(p):
    """fix.log 超过 LOG_MAX_BYTES 就改名成 fix.log.1(旧的覆盖掉), 免得无限变大。
    服务/watcher/bat 可能同时写同一个文件, 所以用 .lock 做互斥; 任何失败都只是
    这轮不滚, 不影响写日志本身。"""
    try:
        if os.path.getsize(p) < LOG_MAX_BYTES:
            return
    except OSError:
        return
    lock = p + '.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except OSError:
        # 拿到锁的进程正在滚; 万一是上次崩溃留下的僵尸锁, 超过 60 秒就清掉
        try:
            if time.time() - os.path.getmtime(lock) > 60:
                os.remove(lock)
        except OSError:
            pass
        return
    try:
        if os.path.getsize(p) >= LOG_MAX_BYTES:   # 拿锁后复查, 别人可能刚滚过
            os.replace(p, p + '.1')
    except OSError:
        pass
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def flush_log(tag='run'):
    """把当前缓冲写盘并清空 —— 这样看守期间也能随时看到已发生的事"""
    if not _lines:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        p = os.path.join(LOG_DIR, 'fix.log')
        _rotate_log(p)
        with open(p, 'a', encoding='utf-8') as f:
            f.write('\n===== %s %s =====\n' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), tag))
            f.write('\n'.join(_lines) + '\n')
        del _lines[:]
    except Exception:
        pass


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate():
    """以管理员身份重新启动自己"""
    if is_admin():
        return True
    exe = sys.executable
    if exe.lower().endswith('python.exe'):
        cand = exe[:-len('python.exe')] + 'pythonw.exe'
        if os.path.exists(cand):
            pass  # 保留控制台, 不换成 pythonw
    args = '"%s" %s' % (os.path.abspath(__file__), ' '.join(sys.argv[1:]))
    r = ctypes.windll.shell32.ShellExecuteW(None, 'runas', '"%s"' % exe, args, BASE, 1)
    return r > 32


# ---------------------------------------------------------------- 驱动
_handle = None


def _extract_driver_from_exe(exe_path, out_path):
    """从 ThrottleStop.exe 里抠出 x64 驱动镜像(含签名), 返回是否成功。

    ThrottleStop 把驱动作为资源内嵌在 exe 里, 运行时解压到 %TEMP% 再加载。
    这里按 PE 结构定位 x64 镜像, 并连同 Authenticode 签名一起取出, 保证签名有效。
    """
    try:
        with open(exe_path, 'rb') as f:
            d = f.read()
    except Exception:
        return False

    i = 0
    while True:
        i = d.find(b'MZ', i)
        if i < 0 or i + 0x40 > len(d):
            return False
        try:
            e = struct.unpack_from('<I', d, i + 0x3c)[0]
            if 0 < e < 0x400 and d[i + e:i + e + 4] == b'PE\x00\x00':
                machine = struct.unpack_from('<H', d, i + e + 4)[0]
                if machine == 0x8664:                      # x64
                    opt = i + e + 24
                    magic = struct.unpack_from('<H', d, opt)[0]
                    dd = opt + (0x70 if magic == 0x20b else 0x60)
                    sec_off, sec_sz = struct.unpack_from('<II', d, dd + 4 * 8)
                    total = sec_off + sec_sz
                    if total <= 0x200:
                        nsec = struct.unpack_from('<H', d, i + e + 6)[0]
                        optsz = struct.unpack_from('<H', d, i + e + 20)[0]
                        sect = i + e + 24 + optsz
                        total = 0
                        for k in range(nsec):
                            o = sect + k * 40
                            rs, ro = struct.unpack_from('<II', d, o + 16)
                            total = max(total, ro + rs)
                    if 0x1000 < total < 0x80000 and i + total <= len(d):
                        with open(out_path, 'wb') as f:
                            f.write(d[i:i + total])
                        return True
        except Exception:
            pass
        i += 2


def ensure_driver_file():
    """确保 TSCtl.sys 存在; 没有就从本机 ThrottleStop.exe 里提取"""
    if os.path.exists(SYS_DST):
        return True

    import glob
    cands = [os.path.join(BASE, 'ThrottleStop.exe')]
    roots = [os.path.join(os.path.expanduser('~'), 'Desktop'),
             os.path.join(os.path.expanduser('~'), 'Downloads'),
             os.path.join(os.path.expanduser('~'), 'Documents'),
             r'C:\Program Files', r'C:\Program Files (x86)', 'C:\\']
    for r in roots:
        try:
            cands += glob.glob(os.path.join(r, '*', 'ThrottleStop.exe'))
            cands += glob.glob(os.path.join(r, '*', '*', 'ThrottleStop.exe'))
        except Exception:
            pass

    for c in cands:
        if c and os.path.exists(c):
            if _extract_driver_from_exe(c, SYS_DST):
                out('[*] 已从 %s 提取驱动' % c)
                return True

    out('[X] 找不到驱动文件 TSCtl.sys')
    out('    请先下载 ThrottleStop(https://www.techpowerup.com/download/techpowerup-throttlestop/)')
    out('    解压后把 ThrottleStop.exe 放到本工具目录下, 然后重新运行。')
    return False


def _open_dev():
    h = k32.CreateFileW('\\\\.\\%s' % SVC, 0xC0000000, 3, None, 3, 0, None)
    if h in (None, w.HANDLE(-1).value, 0xFFFFFFFFFFFFFFFF, -1):
        return None
    return h


def _sc(*args):
    try:
        p = subprocess.run(['C:\\Windows\\System32\\sc.exe'] + list(args),
                           capture_output=True, text=True, errors='replace',
                           creationflags=0x08000000)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except Exception as e:
        return -1, str(e)


def _binpath():
    """sc.exe 的 binPath 参数: 路径带空格时必须整体加引号"""
    return '"%s"' % SYS_DST if ' ' in SYS_DST else SYS_DST


# ---- 直接走 SCM API, 不 spawn sc.exe -------------------------------------
# 0.4GHz 下 spawn 一个进程要好几秒, 开机抢时间时这一步的代价很大。
adv = ctypes.WinDLL('advapi32', use_last_error=True)
adv.OpenSCManagerW.restype = w.HANDLE
adv.OpenSCManagerW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
adv.OpenServiceW.restype = w.HANDLE
adv.OpenServiceW.argtypes = [w.HANDLE, w.LPCWSTR, w.DWORD]
adv.StartServiceW.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.LPCWSTR)]
adv.QueryServiceStatus.argtypes = [w.HANDLE, ctypes.c_void_p]
adv.CloseServiceHandle.argtypes = [w.HANDLE]

SC_MANAGER_CONNECT = 0x0001
SC_MANAGER_CREATE_SERVICE = 0x0002
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SERVICE_RUNNING = 4
SERVICE_ALL_ACCESS = 0xF01FF
DELETE = 0x00010000
SERVICE_KERNEL_DRIVER = 0x00000001
SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_AUTO_START = 0x00000002
SERVICE_DEMAND_START = 0x00000003
SERVICE_ERROR_IGNORE = 0x00000000
SERVICE_ERROR_NORMAL = 0x00000001
SERVICE_CONTROL_STOP = 0x00000001

adv.CreateServiceW.restype = w.HANDLE
adv.CreateServiceW.argtypes = [w.HANDLE, w.LPCWSTR, w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD,
                               w.DWORD, w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p,
                               w.LPCWSTR, w.LPCWSTR, w.LPCWSTR]
adv.DeleteService.argtypes = [w.HANDLE]
adv.ControlService.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p]


def svc_create(name, binpath, display, svc_type, start_type, error_control):
    """创建服务(内核驱动或用户态服务通用)。返回 (成功, 错误信息)"""
    hscm = adv.OpenSCManagerW(None, None, SC_MANAGER_CREATE_SERVICE | SC_MANAGER_CONNECT)
    if not hscm:
        return False, 'OpenSCManager 失败 %d' % ctypes.get_last_error()
    try:
        hs = adv.CreateServiceW(hscm, name, display or name, SERVICE_ALL_ACCESS,
                                svc_type, start_type, error_control,
                                binpath, None, None, None, None, None)
        if not hs:
            err = ctypes.get_last_error()
            if err == 1073:                      # 已存在
                hs = adv.OpenServiceW(hscm, name, DELETE | SERVICE_START | SERVICE_QUERY_STATUS)
                if hs:
                    adv.DeleteService(hs)
                    adv.CloseServiceHandle(hs)
                    time.sleep(0.4)
                    hs = adv.CreateServiceW(hscm, name, display or name, SERVICE_ALL_ACCESS,
                                            svc_type, start_type, error_control,
                                            binpath, None, None, None, None, None)
            if not hs:
                return False, 'CreateService 失败 %d' % ctypes.get_last_error()
        adv.CloseServiceHandle(hs)
        return True, ''
    finally:
        adv.CloseServiceHandle(hscm)


def svc_delete(name):
    hscm = adv.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not hscm:
        return False
    try:
        hs = adv.OpenServiceW(hscm, name, DELETE | SERVICE_STOP | SERVICE_QUERY_STATUS)
        if not hs:
            return False
        try:
            buf = ctypes.create_string_buffer(28)
            if adv.QueryServiceStatus(hs, ctypes.byref(buf)):
                if ctypes.cast(buf, ctypes.POINTER(w.DWORD))[1] != 1:      # 非 STOPPED
                    adv.ControlService(hs, SERVICE_CONTROL_STOP, ctypes.byref(buf))
                    time.sleep(0.5)
            return bool(adv.DeleteService(hs))
        finally:
            adv.CloseServiceHandle(hs)
    finally:
        adv.CloseServiceHandle(hscm)


def svc_state(name):
    """返回 (服务是否存在, 是否已运行)。不 spawn 任何进程。"""
    hscm = adv.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not hscm:
        return None, None
    try:
        hs = adv.OpenServiceW(hscm, name, SERVICE_QUERY_STATUS | SERVICE_START)
        if not hs:
            return False, False
        try:
            buf = ctypes.create_string_buffer(28)      # SERVICE_STATUS
            ok = adv.QueryServiceStatus(hs, ctypes.byref(buf))
            if not ok:
                return True, False
            state = ctypes.cast(buf, ctypes.POINTER(w.DWORD))[1]
            return True, state == SERVICE_RUNNING
        finally:
            adv.CloseServiceHandle(hs)
    finally:
        adv.CloseServiceHandle(hscm)


def svc_start(name):
    hscm = adv.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not hscm:
        return False
    try:
        hs = adv.OpenServiceW(hscm, name, SERVICE_START | SERVICE_QUERY_STATUS)
        if not hs:
            return False
        try:
            if adv.StartServiceW(hs, 0, None):
                return True
            return ctypes.get_last_error() == 1056     # 已在运行
        finally:
            adv.CloseServiceHandle(hs)
    finally:
        adv.CloseServiceHandle(hscm)


def ensure_driver(verbose=True):
    """确保驱动服务已加载, 返回设备句柄"""
    global _handle
    if _handle:
        return _handle

    h = _open_dev()
    if h:
        _handle = h
        return h

    exists, _running = svc_state(SVC)
    if exists is False:                 # 服务不存在 -> 创建
        if not ensure_driver_file():
            return None
        ok, msg = svc_create(SVC, '\\??\\' + SYS_DST, 'TSCtl', SERVICE_KERNEL_DRIVER,
                             SERVICE_DEMAND_START, SERVICE_ERROR_IGNORE)
        if not ok:                      # API 不行再退回 sc.exe
            rc, txt = _sc('create', SVC, 'type=', 'kernel', 'start=', 'demand',
                          'error=', 'ignore', 'binPath=', _binpath())
            ok, msg = (rc == 0), txt.strip()[:120]
        if verbose:
            out('[*] 创建驱动服务: %s' % ('成功' if ok else msg))

    svc_start(SVC)

    for _ in range(40):                 # 开机早期可能还没准备好, 最多等 20 秒
        h = _open_dev()
        if h:
            _handle = h
            return h
        time.sleep(0.5)
    return None


def ioctl(code, inbuf, outlen):
    global _handle
    o = ctypes.create_string_buffer(max(outlen, 1))
    ret = w.DWORD(0)
    ib = ctypes.create_string_buffer(inbuf if inbuf else b'\x00', max(len(inbuf), 1))
    ok = k32.DeviceIoControl(_handle, code, ib, len(inbuf), o, outlen, ctypes.byref(ret), None)
    if not ok:
        # 句柄可能已失效(驱动被卸载 / 设备被移除 / 睡眠后重载) —— 清掉缓存,
        # 下一次 ensure_driver() 会自动重新 CreateFile, 而不是一直拿着死句柄重试。
        _handle = None
    return bool(ok), o.raw


def rdmsr(reg):
    ok, b = ioctl(IOCTL_RDMSR, struct.pack('<I', reg), 8)
    return struct.unpack('<Q', b[:8])[0] if ok else None


def wrmsr(reg, val):
    ok, _ = ioctl(IOCTL_WRMSR, struct.pack('<IQ', reg, val), 0)
    return ok


def rdphys(addr, size=4):
    ok, b = ioctl(IOCTL_READ_PHYS, struct.pack('<Q', addr), size)
    return int.from_bytes(b[:size], 'little') if ok else None


def wrphys(addr, value, size=4):
    ok, _ = ioctl(IOCTL_WRITE_PHYS, struct.pack('<Q', addr) + int(value).to_bytes(size, 'little'), 0)
    return ok


def rdpci(pciaddr, off, size=4):
    ok, b = ioctl(IOCTL_PCI_READ, struct.pack('<II', pciaddr, off), size)
    return int.from_bytes(b[:size], 'little') if ok else None


def mchbar():
    lo = rdpci(0, MCHBAR_REG)
    if not lo:
        return None
    hi = rdpci(0, MCHBAR_REG + 4) or 0
    return (lo & 0xFFFFF000) | (hi << 32)


# ---------------------------------------------------------------- 解析
def decode_pl(v):
    return {
        'pl1_w': (v & 0x7FFF) / 8.0, 'pl1_en': (v >> 15) & 1, 'pl1_t': (v >> 16) & 0xFF,
        'pl2_w': ((v >> 32) & 0x7FFF) / 8.0, 'pl2_en': (v >> 47) & 1, 'pl2_t': (v >> 48) & 0xFF,
        'lock': (v >> 63) & 1,
    }


def rated_tdp():
    """从 MSR 0x614 (PKG_POWER_INFO) 读 CPU 标称 TDP(瓦)"""
    v = rdmsr(0x614)
    if not v:
        return None
    tdp = (v & 0x7FFF) / 8.0
    return tdp if 3.0 <= tdp <= 150.0 else None


def build_pl(pl1, pl2, t1, t2, lock):
    low = (int(round(pl1 * 8)) & 0x7FFF) | (1 << 15) | ((t1 & 0xFF) << 16)
    high = (int(round(pl2 * 8)) & 0x7FFF) | (1 << 15) | ((t2 & 0xFF) << 16)
    v = low | (high << 32)
    if lock:
        v |= 1 << 63
    return v


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_cfg(cfg):
    with open(CFG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ================================================================ EC: 温度 / 风扇
#
# 风扇转速只由 EC 寄存器 HFSP(0x2F) 决定:
#   0x00~0x07 = EC 固定档, 0x40 = 真满速(约 5000 RPM), 0x80 = 交还 EC 自动。
# 原生自动曲线实测几乎是平的(54°C->3861 RPM, 99°C->3896 RPM), 到不了满速,
# 所以这里自己按温度写档位。
#
# 底层走驱动的"端口读写"IOCTL(从驱动分发逆向得到, 与 RDMSR 同一个驱动):
#   读端口 0x80006430   入参 u16 port                        出参 1 字节
#   写端口 0x80006434   入参 u16 port + u16 填充 + u8 value   出参 空
# 注意写入必须是 5 字节(值在第 5 字节), 传 3 字节会被驱动拒绝。
# 之后按 ACPI EC 标准协议访问 EC 寄存器: 命令口 0x66, 数据口 0x62,
# 状态位 IBF=0x02(输入缓冲满) / OBF=0x01(输出缓冲有数据), 命令 0x80=读 0x81=写。
IOCTL_READ_PORT = 0x80006430
IOCTL_WRITE_PORT = 0x80006434
EC_CTRL, EC_DATA = 0x66, 0x62
EC_HFSP, EC_TEMP, EC_RPM_LO, EC_RPM_HI = 0x2F, 0x78, 0x84, 0x85
EC_FSEL_LEGACY = 0x31      # 老 IBM/X60 那代用它选"主/副风扇"(bit0); 本机不是, 见 ec_rpm()
EC_TEMP_EXTRA = (0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0xC9)   # TTS1/TTS6/TTS2/TTS3/TTS4/HWAT
FAN_AUTO, FAN_MAX = 0x80, 0x40
FAN_LEVEL_RPM = {1: 2480, 2: 2610, 3: 2720, 4: 3190, 5: 3185, 6: 3490, 7: 3880, 64: 5000}

# 固件"热到要强制休眠"的真实判据(来自本机 DSDT 里 \_TZ.THM0._TMP 的反汇编):
#   这三个寄存器任一的低 7 位非零 -> _TMP 立刻返回 _HOT(=TCRT-1=401K≈127.9°C),
#   于是固件/系统判定热区到顶, 强制休眠(S4)。
#   注意: 它跟温度寄存器 0x78 的读数无关 —— 0x78 只是给 OS 看的温度(而且 0x80 是
#   "无效"哨兵值, AML 会把它当成 48°C)。2026-10-18 那次强制休眠就是这几个标志触发的。
#   ⚠️ 只有这三个! 0x88(TSL0/TSR0) **不**参与 _TMP 的判据(它只被 _WAK/_Q40 用来挑热表),
#      2026-10-18 实测本机 0x88 常驻 0x40(bit6), 属正常, 别当成"过热"。
EC_TRIP_FLAGS = (0x89, 0x8A, 0x8B)
EC_TEMP_SENTINEL = 0x80        # 0x78 读到这个值 = 传感器给了无效码


def _port_call(code, inbuf, outlen):
    """端口 IOCTL。失败时句柄多半已失效(睡眠后驱动重载等) —— 重开一次再试。
    (CpuPLFix.ioctl 只要失败就会把全局 _handle 清空, 不重开就再也发不出去了)"""
    ok, out = ioctl(code, inbuf, outlen)
    if not ok:
        ensure_driver(verbose=False)
        ok, out = ioctl(code, inbuf, outlen)
    return ok, out


def rd_port(port):
    ok, b = _port_call(IOCTL_READ_PORT, struct.pack('<H', port), 1)
    return b[0] if ok else None


def wr_port(port, val):
    ok, _unused = _port_call(IOCTL_WRITE_PORT, struct.pack('<HHB', port, 0, val & 0xFF), 0)
    return ok


def _ec_drain():
    """把 EC 输出缓冲里可能残留的字节读掉(最多 8 个)"""
    for _ in range(8):
        if not (rd_port(EC_CTRL) or 0) & 0x01:
            return
        rd_port(EC_DATA)
        time.sleep(0.001)


def _ec_wait(clear_obf, timeout=1.0):
    """等 EC 就绪: IBF(0x02) 必须为 0; clear_obf=True 时还要求 OBF(0x01) 为 0。

    注意: 这里只能等 IBF, 不能等 OBF 置位 —— TSCtl 驱动的端口读会把 OBF 自己吃掉,
    靠 OBF 判断会把每一次读都判成超时(实测踩过)。顺序照抄已验证可用的实现。
    """
    end = time.time() + timeout
    while time.time() < end:
        st = rd_port(EC_CTRL)
        if st is None:
            return False
        if (st & 0x02) == 0 and (not clear_obf or (st & 0x01) == 0):
            return True
        time.sleep(0.001)
    return False


EC_MUTEX_NAME = 'Global\\CpuPLFix-EC'
_ec_mx = None


def _ec_lock(timeout_ms=3000):
    """把 EC 端口事务跨进程串行化(看守服务 / 命令行 / 扫描工具都用这一把)。

    实测教训: 两个进程同时做那套"写命令 0x80 -> 写偏移 -> 读数据"的四步事务时, 双方会
    互相插队, 应答错位, 读出来的是对方那次事务的字节(0x78 读成 46 而实际 82)。
    用命名互斥体(Global\\ 才能跨会话 —— 服务在 session 0, 命令行在用户会话)。
    拿不到就退化成不加锁(尽力而为), 锁本身不会让功能失效。
    """
    global _ec_mx
    if _ec_mx is None:
        try:
            k32.CreateMutexW.restype = w.HANDLE
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
            h = k32.CreateMutexW(None, False, EC_MUTEX_NAME)
            if not h:
                k32.OpenMutexW.restype = w.HANDLE
                k32.OpenMutexW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
                h = k32.OpenMutexW(0x00100000 | 0x0001, False, EC_MUTEX_NAME)  # MUTEX_MODIFY_STATE|SYNCHRONIZE
            _ec_mx = h or False
        except Exception:
            _ec_mx = False
    if not _ec_mx:
        return False
    try:
        r = k32.WaitForSingleObject(_ec_mx, timeout_ms)
        return r in (0x00000000, 0x00000080)      # WAIT_OBJECT_0 / WAIT_ABANDONED(上一个持有者死了)
    except Exception:
        return False


def _ec_unlock(locked):
    if locked and _ec_mx:
        try:
            k32.ReleaseMutex(_ec_mx)
        except Exception:
            pass


def ec_read(off, _locked=False, delay=0.003):
    """读一个 EC 寄存器(失败返回 None)。delay = 写完偏移后等 EC 应答的时间(秒)"""
    locked = _ec_lock() if not _locked else True
    try:
        _ec_drain()
        if not _ec_wait(True):
            return None
        wr_port(EC_CTRL, 0x80)
        if not _ec_wait(False):
            return None
        wr_port(EC_DATA, off)
        if not _ec_wait(False):
            return None
        # 等 EC 把结果放进输出缓冲。EC 忙(满载时它自己在管风扇/热区)时不会立刻应答,
        # 不等就直接读, 读到的往往是上一次留在缓冲里的残留字节 —— 实测满载扫描时
        # 一次"读 0x8A"读到的是 0x7A 的值(46), 而且连读两次都是这个残留值。
        # 等待时间取"调用方给的"与"自适应值"的较大者(串位多了会自动变大)。
        time.sleep(max(delay, ec_bus_settle()))
        v = rd_port(EC_DATA)
        if v is None:
            ec_bus_stat(True)
        return v
    finally:
        if not _locked:
            _ec_unlock(locked)


def ec_read_ok(off, tries=3):
    """带一致性校验的读: 连续两次读到同一个值才算数, 否则加大等待重试; 都不一致就返回 None。

    两道保险一起用:
      1) 读之前等一会儿(第一次 3ms, 重试 8/20ms) —— 防"抢在 EC 应答之前读"拿到残留字节;
      2) 连读两次比对 —— 防两个进程的事务互相插队时读到对方的应答。
    校验不过就宁可判成"这次没读到"(沿用上一次的判断), 也不拿可疑值去改保护等级。
    """
    prev = None
    delays = (0.003, 0.008, 0.020)
    for i in range(max(2, tries)):
        v = ec_read(off, delay=delays[min(i, len(delays) - 1)])
        if v is not None and prev is not None and v == prev:
            return v
        prev = v
    ec_bus_stat(True)       # 读到不一致/读不出: 记一次总线异常(自适应等待会加大)
    return None


# ---------------------------------------------------------------- EC 总线加固
# 工程机(ES)上 EC 与系统 ACPI EC 驱动共用 0x62/0x66, 串位比零售机常见: 实测抓到过
# "读 0x8B 得到 0x3B"(0x3B 正是那一刻 0x78 的温度值)、"读 0x8A 得到 0x7A 的值(46)"。
# 三道对策(成本极低, 只在关键读上多花 1~2 次事务):
#   1) 哨兵: 0xC9(HWAT) 本机实测恒 65(见 05 §2), 断电复位也不变 —— 关键读的前后各读一次,
#      对不上就说明这次事务序列错位了, 整批判废重来(错位时"连读两次一致"也会一致地错);
#   2) 自适应等待: 检出串位就把"写完偏移后的等待"翻倍(上限 20ms), 连续 20 次干净再退回去;
#   3) 计数: 串位/事务数攒起来, 每 120 次事务报一行, 回头看工程机的总线到底有多糟。
EC_CANARY = (0xC9, 65)          # (偏移, 期望值): HWAT, 本机常量
_ECBUS = {'ok': 0, 'bad': 0, 'settle': 0.003, 'clean': 0, 'pending': False}
ECBUS_REPORT_EVERY = 120


def ec_bus_settle():
    """当前"写完偏移后等 EC 应答"的等待(秒) —— 检出串位会自动加大。"""
    return _ECBUS['settle']


def ec_bus_stat(hit_bad):
    """记一次事务结果 + 自适应等待; 攒够了置 pending(由 ec_bus_report 取走)。"""
    if hit_bad:
        _ECBUS['bad'] += 1
        _ECBUS['clean'] = 0
        _ECBUS['settle'] = min(0.020, _ECBUS['settle'] * 2)
    else:
        _ECBUS['ok'] += 1
        _ECBUS['clean'] += 1
        if _ECBUS['clean'] >= 20 and _ECBUS['settle'] > 0.003:
            _ECBUS['settle'] = max(0.003, _ECBUS['settle'] / 2.0)
    if _ECBUS['ok'] + _ECBUS['bad'] >= ECBUS_REPORT_EVERY:
        _ECBUS['pending'] = True


def ec_bus_stats():
    """(事务数, 串位数, 等待毫秒) —— 给 2-check 显示。"""
    return _ECBUS['ok'] + _ECBUS['bad'], _ECBUS['bad'], _ECBUS['settle'] * 1000.0


def ec_bus_report():
    """攒够事务数且出过串位就返回一行; 否则 None(并把窗口清零)。"""
    if not _ECBUS['pending']:
        return None
    n_ok, n_bad = _ECBUS['ok'], _ECBUS['bad']
    _ECBUS['ok'] = _ECBUS['bad'] = 0
    _ECBUS['pending'] = False
    if not n_bad:
        return None
    return ('EC 总线体检: 最近 %d 次事务里 %d 次串位(%.1f%%), 等待已自适应到 %.1f ms '
            '—— 工程机 + 系统 ACPI EC 驱动抢端口, 属已知现象(见 03 §4), 可疑读数已被丢弃'
            % (n_ok + n_bad, n_bad, 100.0 * n_bad / max(1, n_ok + n_bad), _ECBUS['settle'] * 1000.0))


def ec_read_strict(off, tries=3):
    """关键读(会触发动作的): 哨兵 + 两次一致, 任一不过整批判废重试, 都不成就返回 None。

    比 ec_read_ok 多两道: 每次读的前后夹一次哨兵 0xC9=65; 判废时把等待加大再来一遍。
    给"固件过热标志 0x89/0x8A/0x8B"这种一置位就要动手的寄存器用 —— 误读的代价是白压功耗。
    """
    prev = None
    delays = (0.003, 0.008, 0.020)
    for i in range(max(2, tries)):
        d = max(delays[min(i, len(delays) - 1)], ec_bus_settle())
        c1 = ec_read(EC_CANARY[0], delay=d)
        v = ec_read(off, delay=d)
        c2 = ec_read(EC_CANARY[0], delay=d)
        good = (c1 == EC_CANARY[1] and c2 == EC_CANARY[1] and v is not None)
        if not good:
            ec_bus_stat(True)
            time.sleep(0.010)
            prev = None
            continue
        if prev is not None and v == prev:
            ec_bus_stat(False)
            return v
        # 哨兵对、但两次读出来的值不一样 —— 也是串位(实测就是这么表现的: 读 0x8B 一次拿到
        # 0x3B(那一刻的温度值)、下一次才对)。按异常计数 + 加大等待, 再来一遍。
        ec_bus_stat(prev is not None)
        prev = v
    return None


def ec_write(off, val, _locked=False):
    """写一个 EC 寄存器"""
    locked = _ec_lock() if not _locked else True
    try:
        _ec_drain()
        if not _ec_wait(True):
            return False
        wr_port(EC_CTRL, 0x81)
        if not _ec_wait(False):
            return False
        wr_port(EC_DATA, off)
        if not _ec_wait(False):
            return False
        wr_port(EC_DATA, val)
        if not _ec_wait(False):
            return False
        return True
    finally:
        if not _locked:
            _ec_unlock(locked)


def ec_hfsp():
    return ec_read_ok(EC_HFSP)


def ec_temp():
    """EC 温度寄存器(°C)。有效范围 0~127（0x80 是固件的"无效"哨兵值, AML 会把它当成 48°C）。
    本机实测: 空闲 49~57, 40W 满载+满速风峰值 ~92。出界当读失败。"""
    v = ec_read_ok(EC_TEMP)
    return v if (v is not None and 0 <= v <= 127) else None


def ec_trip_flags(tmp=None):
    """读固件判"过热强制休眠"用的标志寄存器, 返回 {offset: value}。

    2026-10-19 起用 ec_read_strict(哨兵 0xC9=65 + 两次一致): 这三个寄存器一置位就要压功耗,
    实测抓到过串位假值(0x8B 读到 0x3B, 而那正是当时的温度值), 白压一次代价是几十秒卡顿。
    tmp = 当前 EC 温度(可选): 标志合法值是位图, 恰好等于温度字节的极罕见 —— 遇到就判读错。
    """
    d = {}
    for h in EC_TRIP_FLAGS:
        v = ec_read_strict(h)
        if v is None:
            continue
        if tmp is not None and v == tmp:
            ec_bus_stat(True)
            continue
        d[h] = v
    return d


def ec_tripped(flags):
    """有标志置位(低 7 位非零)就返回 [(offset, value), ...]"""
    return sorted((h, v) for h, v in (flags or {}).items() if v & 0x7F)


def ec_rpm():
    """风扇转速。先读 0x84 再读 0x85 —— 顺序不能反(thinkpad_acpi 记载的固件 bug)。

    2026-10-18 实测: **不需要**先写 0x31。
      · 本机 DSDT 里 0x31 的位叫 HUWB(bit2)/VPON(bit6)/VRST(bit7), bit0/1 是保留位;
        "写 0x31=0 选主风扇" 是老 IBM / X60 那代(固件 TP-7M)的做法, 抄过来是错的。
      · 本机 0x31 实测常驻 0x00, 而且"写"和"不写"读到的转速完全一样(3888/3889 RPM)。
      · 原来每次读转速都多写一次 EC —— 那三个位语义没确认, 少碰为好。
    只有"两次读都失败"时才退回老做法试一次(兼容别的机型)。
    """
    locked = _ec_lock()
    try:
        lo = ec_read(EC_RPM_LO, _locked=True)
        hi = ec_read(EC_RPM_HI, _locked=True)
        if lo is None or hi is None:
            ec_write(EC_FSEL_LEGACY, 0, _locked=True)     # 兜底: 别的机型可能要选一下
            lo = ec_read(EC_RPM_LO, _locked=True)
            hi = ec_read(EC_RPM_HI, _locked=True)
    finally:
        _ec_unlock(locked)
    if lo is None or hi is None:
        return None
    return (hi << 8) | lo


def ec_extra_temps():
    """本机 EC 里另外几个温度寄存器(不给保护逻辑用, 只看)。

    来源: DSDT 的 EC Field 名 —— 0x7A TTS1 / 0x7B TTS6 / 0x7C TTS2 / 0x7D TTS3 /
    0x7E TTS4 / 0xC9 HWAT。0x79 是 GPU 温度(本机无独显 = 0x80)。

    实测(2026-10-18):
      · TTS3/TTS4 跟负载动, 但比 0x78 慢得多(空闲 59/54, 满载 73/64; 同期 0x78 是 59→85)。
        所以它们**不是**"最热的那个", 只是另一组传感器(大概靠主板/风道)。
      · 0xC9 HWAT 在"空闲"和"满载"两个差很大的状态下**都恰好 65** → 它不是温度读数,
        是个常量(含义还没解出来), 别当温度用。
    """
    d = {}
    for off in EC_TEMP_EXTRA:
        v = ec_read_ok(off)
        if v is not None and 0 <= v <= 127:
            d[off] = v
    return d


def set_fan(v):
    """写风扇档位, 返回回读值(None = 失败)"""
    ec_write(EC_HFSP, v)
    return ec_hfsp()


def fan_name(v):
    if v is None:
        return '读取失败'
    if v == FAN_AUTO:
        return 'EC 自动'
    if v >= FAN_MAX:
        return '满速'
    if 1 <= v <= 7:
        return '%d 档(约 %d RPM)' % (v, FAN_LEVEL_RPM.get(v, 0))
    return '0x%02X' % v


def fan_cfg(cfg):
    d = dict(DEFAULT_CFG['fan'])
    d.update(cfg.get('fan') or {})
    return d


_FAN = {'idx': None, 'lv': None, 't': 0.0, 'hi_since': None, 'boost_until': 0.0}


def _fan_ok(v):
    """EC 回读的档位是否合法编码(0x00~0x07 / 0x40 满速 / 0x80 交还 EC)。

    2026-10-19 实测日志里出现过 "风扇: 0x24 -> 满速" 这种行 —— 那是 EC 事务串位读到
    别的寄存器的值。垃圾值不能当档位比较(否则每轮都重写、日志全是假变化), 但也不能
    因此就不写目标档。见 fan_tick()。
    """
    return v is not None and (v <= 0x07 or v == FAN_MAX or v == FAN_AUTO)


def fan_level_of(temp, curve, prev_idx=None, hyst=5.0):
    """按温度曲线取 (档位, 段号)。curve 是 [[温度上限, 档位], ...] 从低到高。

    升温立即升档; 降温要低过"当前这一段的**下边界** - 回差"才降。
    下边界 = curve[prev_idx - 1][0] (上一段的上限), 不是 curve[prev_idx][0] ——
    用后者的边界是 66°C, 57°C 也能通过, 于是 57/58 来回跳档(实测踩过)。
    """
    idx = len(curve) - 1
    for i, (hi, _lv) in enumerate(curve):
        if temp < hi:
            idx = i
            break
    if prev_idx is not None and idx < prev_idx:
        lower = curve[prev_idx - 1][0] if prev_idx >= 1 else curve[0][0]
        if temp >= lower - hyst:
            idx = prev_idx
    return curve[idx][1], idx


def fan_tick(cfg, temp=None, force_max=False, load=None):
    """按配置写一次风扇档位。返回变化描述; 没有变化返回 None。

    2026-10-19 加两处:
      · 回读到的档位先校验 —— EC 与系统 ACPI 驱动共用 0x62/0x66, 串位会读到 0x2D/0x24
        这类垃圾值。垃圾值不参与比较, 但目标档照写; 提示只记一行, 不再每轮刷假变化。
      · 降档最短驻留 fan.min_dwell_s: 只压"往低档退", 升档照旧立即; 温度掉到
        "目标段边界 - 10°C" 以下立即放行, 所以待机该安静还是安静。
    """
    f = fan_cfg(cfg)
    mode = str(f.get('mode') or 'curve').lower()
    boosted = False
    if temp is None:
        temp = ec_temp()
    idx = None
    if force_max:
        want = FAN_MAX
    elif mode == 'max':
        want = FAN_MAX
    elif mode in ('off', 'auto', 'ec'):
        want = FAN_AUTO
    elif mode == 'level':
        want = int(f.get('level') or 0) & 0xFF
    else:
        if temp is None:
            return None
        want, idx = fan_level_of(temp, f['curve'], _FAN['idx'], float(f.get('hyst') or 5.0))
        # ---- 降档最短驻留(只作用于"降温降档") ----
        dwell = float(f.get('min_dwell_s') or 0.0)
        prev_lv = _FAN.get('lv')
        if (dwell > 0 and idx is not None and prev_lv is not None and want < prev_lv
                and temp > f['curve'][idx][0] - 10.0
                and (time.time() - (_FAN.get('t') or 0.0)) < dwell):
            want, idx = prev_lv, _FAN.get('idx')
        # ---- ★负载前馈(2026-10-19): 持续高占用就把风扇提前拉起来 ----
        # 只按 EC 温度升档会慢半拍(0x78 比核心真实温度滞后): 实测 100% 负载已跑起来时风扇
        # 还停在 6 档, 等 EC 报 98°C 才拉满, 接着温度砸下去、风扇停、再冲上来 —— 就是那套
        # "风扇没及时反馈 + 往复振荡"。这里用 Windows 的占用率做前馈, 不花 EC 事务。
        lb = float(f.get('load_boost_pct') or 0.0)
        lvl = int(f.get('load_boost_level') or 0)
        if lb > 0 and lvl > 0 and load is not None:
            if load >= lb:
                if _FAN.get('hi_since') is None:
                    _FAN['hi_since'] = time.time()
                if time.time() - _FAN['hi_since'] >= float(f.get('load_boost_s') or 20.0):
                    _FAN['boost_until'] = time.time() + float(f.get('load_hold_s') or 30.0)
            else:
                _FAN['hi_since'] = None
            if _FAN.get('boost_until') and time.time() < _FAN['boost_until']:
                if lvl > want:
                    want, boosted = lvl, True
    cur = ec_hfsp()
    if cur is None:
        return None
    if not _fan_ok(cur):
        got = set_fan(want)                 # 回读不可信, 目标档照样写下去
        _FAN['idx'] = idx
        if got is None:
            return '风扇: 写 0x%02X 失败(EC 无响应)' % want
        return _note_once('fan_junk',
                          '风扇: 档位回读异常(0x%02X, EC 事务串位) -> 已按目标重写 %s'
                          % (cur, fan_name(got)))
    if cur == want:
        _FAN['idx'] = idx
        _FAN['lv'] = cur
        return None
    t0 = time.time()
    got = set_fan(want)
    _FAN['idx'] = idx
    if got is None:
        return '风扇: 写 0x%02X 失败(EC 无响应)' % want
    if _FAN.get('lv') != got:
        _FAN['t'] = t0                      # 档位真的变了 -> 驻留计时从这里起算
    _FAN['lv'] = got
    why = (' (过热保护)' if force_max and mode != 'max' else
           (' (负载前馈 %.0f%%)' % load if boosted and load is not None else
            (' (%d°C)' % temp if (idx is not None and temp is not None) else '')))
    return '风扇: %s -> %s%s' % (fan_name(cur), fan_name(got), why)


# ================================================================ 过热保护
#
# 本机固件设了 ACPI 热区 \_TZ.THM0 的 _HOT = 401K (≈127.9°C): 热区摸到它, 固件
# 直接强制休眠(S4)。2026-10-18 12:43 跑满载时实测触发过一次(事件 ID 88 "严重的热事件")。
#
# 扳 DSDT 反汇编(本机 \_TZ.THM0._TMP)后发现的关键事实:
#   * _TMP 平时返回的是 EC 0x78 换算来的温度(TC2K: x*10 + 2732, 单位 0.1K);
#   * 但 0x78 这台机器上最高只到 ~99, 量程上够不到 _HOT(127.9°C); 而且 0x80 是
#     "无效"哨兵值(AML 会把它当成 48°C);
#   * 真正让 _TMP 返回 _HOT 的, 是 EC 自己的过热标志 0x89 / 0x8A / 0x8B (低 7 位非零),
#     _TMP 一旦看到它就直接 Return (TCRT - 1) = 401K —— 这才是那次强制休眠的扳机。
# 所以保护分两条线:
#   1) 温度线(预防): EC 0x78 到 warn/crit 就砍功耗墙 + 风扇拉满;
#   2) 固件标志线(最高级): 0x89/0x8A/0x8B 一置位 == 固件已经在报 _HOT,
#      立刻压到最低功耗 + 风扇满速, 可选主动优雅休眠。
# 诚实说明: 我们看不到 EC 内部那颗真正决定它什么时候置标志的传感器, 所以第 1 条只是
# "提前降温", 阈值是凭经验定的, 不是精确标定。
_PL_OVERRIDE = None


def set_pl_override(pl1, pl2=None):
    """看守服务用: 临时把功耗墙压到 pl1/pl2(过热保护); 传 None 解除。
    只影响本进程(服务), 不写 config.json。"""
    global _PL_OVERRIDE
    _PL_OVERRIDE = None if not pl1 else (float(pl1), float(pl2 or pl1))


GUARD_MAX_JUMP = 15.0      # °C/轮: 相邻两次真读跳这么多就当天可疑读数, 下一轮确认
EC_JUMP_RETRY_S = 2.0      # ★可疑跳变时把下一轮 EC 读提前到这么多秒(见 temp_jump_pending)
_G = {'level': None, 'since': 0.0, 'hibernate': False, 'last_et': None, 'trip_hits': 0,
      'et_jump': False, 'et_hold': False, 'last_flags': {},
      'trip_since': None, 'trip_latched': False, 'cool_since': None}


def guard_cfg(cfg):
    d = dict(DEFAULT_CFG['guard'])
    d.update(cfg.get('guard') or {})
    return d


def flags_desc(flags):
    """把固件过热标志寄存器显示成 '0x89=0x00 0x8A=0x00 0x8B=0x00'(读失败的显示 --)"""
    flags = flags or {}
    return ' '.join('0x%02X=%s' % (h, '--' if flags.get(h) is None else '0x%02X' % flags[h])
                    for h in EC_TRIP_FLAGS)


def guard_apply(cfg, et, dts=None, flags=None, flags_fresh=True):
    """定保护等级; 返回等级变化的描述(无变化返回 None)。

    最高优先级看**固件自己的过热标志**: EC 0x89/0x8A/0x8B 的低 7 位。这是本机 DSDT 里
    \\_TZ.THM0._TMP 用来"直接返回 _HOT(401K)"的判据 —— 也就是 2026-10-18 那次强制休眠
    的真实触发者。温度寄存器 0x78 只是 _TMP 平时返回给 OS 的读数, 它本身够不到 _HOT
    (见文件顶部 EC_TRIP_FLAGS 的说明)。

    flags_fresh=True 表示 flags 是本轮真读出来的; False 表示沿用了上一轮的缓存值
    (EC 读间隔拉长后会出现), 此时**不拿它当新的证据** —— 否则同一个读数会被反复计数,
    "连续两轮确认"就退化成"一轮读数确认两 tick"。
    """
    g = guard_cfg(cfg)
    if not g.get('enabled', True):
        if _G['level']:
            _G['level'] = None
            set_pl_override(None)
            return '过热保护: 配置里已关闭, 功耗墙恢复原档'
        return None
    hit = ec_tripped(flags) if g.get('trip_flags', True) else []   # 标志置位 -> 最高级
    if flags_fresh:
        # 必须连续两轮"真读"都读到才认 —— 单轮的垃圾读数(两个进程插队)不能让我们误判
        # "固件翻脸了"。沿用的缓存值既不算命中、也不清零: 它根本不是一轮读数, 要是让
        # 它清零, 标志读降到 30 秒一次后就得 60 秒才确认得了。
        _G['trip_hits'] = (_G.get('trip_hits', 0) + 1) if hit else 0
        if hit and _G['trip_hits'] == 1:
            return ('注意: 固件过热标志读到 %s —— 再确认一轮(下一轮仍读到就动作)'
                    % ', '.join('0x%02X=0x%02X' % (h, v) for h, v in hit))

    # ---- ★锁存出口(2026-10-19 加) ------------------------------------------------
    # 实测: EC 0x89 的 bit0 会被固件"锁存"成 1, 温度回到 36~51°C 也不清零; 旧逻辑要
    # "标志清零 + 温度正常"才解除 -> PL1 被钉在 10W(倍频 10~17), 工具自己造出锁频。
    # 现在: 标志置位、且 EC 一直 <= recover_c 满 trip_hold_s 秒 -> 判"固件锁存",
    # 恢复功耗墙并进入 latched 态; 标志清零前不再按标志降档, 但温度线(95/98)照旧。
    hold = float(g.get('trip_hold_s') or 0.0)
    desc = ', '.join('0x%02X=0x%02X' % (h, v) for h, v in hit)
    if not hit:
        _G['trip_since'] = None
        if _G.get('trip_latched'):
            _G['trip_latched'] = False
            if _G['level'] == 'trip':
                _G['level'] = None
                set_pl_override(None)
            return '固件过热标志已清零, 解除"锁存"判定(下次置位仍会正常保护)'
    elif _G.get('trip_hits', 0) >= 2:
        if _G.get('trip_since') is None:
            _G['trip_since'] = time.time()
        if (hold > 0 and et is not None and et <= g['recover_c']
                and time.time() - _G['trip_since'] >= hold):
            _G['trip_latched'] = True
            _G['trip_since'] = time.time()
            _G['level'] = None
            set_pl_override(None)
            return ('固件过热标志 %s 已挂 %.0f 秒, 而 EC 只有 %d°C(≤ %.0f°C) —— 判定为'
                    '**固件把标志锁住了**, 不是真过热。已恢复正常功耗墙, 标志清零前不再'
                    '据此降档(温度线 %g/%g 仍然有效)。彻底清掉它要断电重启。'
                    % (desc, hold, et, g['recover_c'], g['warn_c'], g['crit_c']))
        if _G.get('trip_latched'):
            hit = []            # 锁存期间: 标志不再参与定级

    lv = None
    if hit and _G.get('trip_hits', 0) >= 2:
        # ★2026-10-19: 标志置位**不等于现在热**。实测 11:04 时 0x89=0x01 而 EC 58°C /
        #   DTS 62°C / 倍频 43(4.3GHz) / 占用 10%; 01:03~04:28 那次在 36~51°C 挂了 3 小时;
        #   11:01 还在 55~74°C 的凉机上白压了两次 10W。它是固件"记录过一次过热"的锁存位。
        #   所以按温度分档:
        #     温度也到 warn_c(或 DTS 到 dts_crit_c) -> 'trip' = 最高级, 砍 crit_pl(10W)
        #     温度正常                              -> 'flag' = 只风扇满速 + 收到 flag_pl(20/28W)
        hot = ((et is not None and et >= g['warn_c'])
               or (dts is not None and dts >= g.get('dts_crit_c', 97.0)))
        lv = 'trip' if hot else 'flag'
    elif et is not None:                # 读不到 EC 温度就不按温度动, 免得误判
        if et >= g['escape_c']:
            lv = 'escape'
        elif et >= g['crit_c']:
            lv = 'crit'
        elif et >= g['warn_c']:
            lv = 'warn'
    elif dts is not None and dts >= g.get('dts_crit_c', 97.0):
        # EC 读不到时才拿 CPU DTS 兜底。本机 DTS(MSR 0x1B1) 满载时读数会乱走,
        # 不能跟 EC 并列当触发条件 —— 否则会"压 10W -> 下一轮读回正常 -> 解除"地来回抖。
        lv = 'crit'
    if lv is None:
        if _G['level'] and et is not None and et <= g['recover_c']:
            # ★解除也要驻留(2026-10-19): 以前一碰 recover_c 就放开 -> 实测 10:59 压 10W、
            #   11:01 放开、11:01 又压、11:04 又放开, 每 2~3 分钟来回一次(温度全程 55~74°C)。
            now = time.time()
            if now - _G.get('since', 0.0) < float(g.get('min_hold_s') or 0.0):
                return None
            if _G.get('cool_since') is None:
                _G['cool_since'] = now
                return None
            if now - _G['cool_since'] < float(g.get('release_dwell_s') or 0.0):
                return None
            old = _G['level']
            _G['level'] = None
            _G['cool_since'] = None
            set_pl_override(None)
            return ('过热保护解除(EC %d°C ≤ %.0f°C 且稳住 %.0f 秒): 功耗墙恢复 [%s], '
                    '原保护等级 %s' % (et, g['recover_c'],
                                       float(g.get('release_dwell_s') or 0.0),
                                       _pick_profile(cfg)[2], old))
        _G['cool_since'] = None
        return None
    if lv == _G['level']:
        return None
    _G['level'] = lv
    _G['since'] = time.time()
    _G['cool_since'] = None
    if lv == 'trip':
        set_pl_override(g['crit_pl1_w'], g['crit_pl2_w'])
        desc = ', '.join('0x%02X=0x%02X' % (h, v) for h, v in hit)
        if g.get('escape_hibernate'):
            _G['hibernate'] = True
            return ('过热保护[固件标志]: EC %s —— 固件已把热区报成 _HOT(401K), '
                    '不等它强制断电, 立即主动休眠(S4)' % desc)
        return ('过热保护[固件标志]: EC %s —— 这是固件判"过热"的真实判据(与 0x78 读数无关), '
                '功耗墙压到 PL1=%.0fW/PL2=%.0fW, 风扇满速(已连续两轮确认)'
                % (desc, g['crit_pl1_w'], g['crit_pl2_w']))
    if lv == 'escape':
        set_pl_override(g['crit_pl1_w'], g['crit_pl2_w'])
        if g.get('escape_hibernate'):
            _G['hibernate'] = True
            return '过热保护[逃生]: EC %d°C ≥ %.0f°C —— 主动休眠(避免固件 401K 强制断电)' % (et, g['escape_c'])
        return ('过热保护[逃生]: EC %d°C ≥ %.0f°C —— 功耗墙已压到最低, 风扇满速'
                '(想让它主动休眠就把 config 的 guard.escape_hibernate 改成 true)'
                % (et, g['escape_c']))
    if lv == 'crit':
        set_pl_override(g['crit_pl1_w'], g['crit_pl2_w'])
        if et is None:
            return ('过热保护[严重]: EC 温度读不到, 用 CPU DTS %d°C 兜底 —— '
                    '功耗墙压到 PL1=%.0fW/PL2=%.0fW, 风扇满速'
                    % (dts, g['crit_pl1_w'], g['crit_pl2_w']))
        return ('过热保护[严重]: EC %d°C ≥ %.0f°C%s —— 功耗墙压到 PL1=%.0fW/PL2=%.0fW, 风扇满速'
                % (et, g['crit_c'], ('/DTS %d°C' % dts) if dts is not None else '',
                   g['crit_pl1_w'], g['crit_pl2_w']))
    if lv == 'flag':
        set_pl_override(g.get('flag_pl1_w', g['warn_pl1_w']),
                        g.get('flag_pl2_w', g['warn_pl2_w']))
        return ('过热保护[固件标志-降温]: EC %s 置位, 但温度正常(EC %s, DTS %s) —— 只把风扇'
                '拉满 + 功耗墙收到 PL1=%.0fW/PL2=%.0fW(不砍到 %.0fW); 满 %.0f 秒还这么凉就判'
                '"锁存"放开(温度线 %g/%g 全程有效)'
                % (desc, ('%d°C' % et) if et is not None else '读不到',
                   ('%d°C' % dts) if dts is not None else '--',
                   g.get('flag_pl1_w', 20.0), g.get('flag_pl2_w', 28.0),
                   g['crit_pl1_w'], hold, g['warn_c'], g['crit_c']))
    set_pl_override(g['warn_pl1_w'], g['warn_pl2_w'])
    return ('过热保护[预警]: EC %d°C ≥ %.0f°C —— 功耗墙压到 PL1=%.0fW/PL2=%.0fW, 风扇满速'
            % (et, g['warn_c'], g['warn_pl1_w'], g['warn_pl2_w']))


def do_hibernate():
    """主动优雅休眠(S4): 落盘后断电, 醒来会话还在 —— 比固件的强制断电温和"""
    out('[*] 过热保护: 立即主动休眠')
    flush_log('guard')
    try:
        ctypes.windll.powrprof.SetSuspendState(1, 0, 0)
        return True
    except Exception as e:
        out('[X] 主动休眠失败: %s' % e)
        return False


def tick(cfg=None, ec=True, read_flags=True):
    """看守服务/计划任务每轮调一次: (读 EC) -> 过热保护 -> 功耗墙 -> 风扇曲线。
    返回本轮变化描述列表(调用方负责记日志)。

    ec=False         本轮完全不碰 EC(降频节奏): 温度/风扇档位沿用上一轮的值,
                     fan_tick 也跳过。功耗墙/电源档 + CPU DTS 这条线只走 MSR/MMIO, 照常跑。
    read_flags=False 本轮不读固件过热标志(缓存值只当显示用, 不当新证据 —— 见 guard_apply)。
    """
    cfg = cfg if cfg is not None else load_cfg()
    notes = []
    if not ensure_driver(verbose=False):
        return notes
    prev = _G.get('last_et')
    et = prev                            # ec=False 时也走这个初始值
    if ec:
        et = None
        try:
            et = ec_temp()
        except Exception:
            et = None
        if et is None and prev is not None:
            et = prev                    # 偶发读失败沿用上次值, 免得保护被莫名解除
        elif et is not None:
            # 实测 EC 端口事务偶尔会串位(读出来是别的寄存器的值: 满载时 0x78 读出 46, 实际 82)。
            # 温度是慢变量, 两轮之间不可能跳 15°C 以上 —— 跳了就先当"可疑值", 本轮沿用上一次,
            # 下一轮还是这样才认。宁可多保护一轮, 也不能拿可疑的低值去解除保护。
            if prev is not None and abs(et - prev) > GUARD_MAX_JUMP:
                if _G.get('et_jump'):
                    _G['et_jump'] = False    # 连续两轮都跳 -> 是真的在急升/急降, 认了
                    _G['et_hold'] = False
                else:
                    _G['et_jump'] = True
                    _G['et_hold'] = True     # 本轮沿用了上一轮的值 -> 让调用方下轮快点再读
                    et = prev
            else:
                _G['et_jump'] = False
                _G['et_hold'] = False
            _G['last_et'] = et
    dts = None
    try:
        tv = cpf_read_thermal()
        dts = tv['temp'] if tv else None
    except Exception:
        dts = None
    load = cpu_load_pct()          # 每轮都采(不碰 EC/不开句柄), 给风扇前馈用
    flags = _G.get('last_flags') or {}
    fresh = bool(ec and read_flags)
    if fresh:
        try:
            flags = ec_trip_flags(_G.get('last_et'))
            _G['last_flags'] = flags
        except Exception:
            flags = _G.get('last_flags') or {}
            fresh = False
    if ec and et == EC_TEMP_SENTINEL:
        notes.append('注意: EC 温度寄存器(0x78)读到无效码 0x80 —— 固件的 ACPI 热区'
                     '会把它当成 48°C(0x30), 属于传感器异常, 别当真温度')
    n = guard_apply(cfg, et, dts, flags, flags_fresh=fresh)
    if n:
        notes.append(n)
    n = ensure_profile(cfg)
    if n:
        notes.append(n)
    if ec:
        g = guard_cfg(cfg)
        force = bool(g.get('force_fan_max', True)) and _G['level'] in ('warn', 'crit', 'trip',
                                                                       'escape', 'flag')
        try:
            n = fan_tick(cfg, et, force_max=force, load=load)
            if n:
                notes.append(n)
        except Exception as e:
            notes.append('风扇写入异常: %s' % e)
        rep = ec_bus_report()
        if rep:
            notes.append(rep)
    # ---- ★电池链路看门狗(2026-10-19 加) -----------------------------------------
    # 那天 ACPI 电池数据全变成 0xFFFFFFFF(设计/满充 = -1 mWh), 工具全程无感, 是用户
    # 自己发现"电池显示 -1"。GetSystemPowerStatus 不碰 EC、不花代价, 所以每轮都查。
    b = battery_flags()
    if b is not None:
        ac_line, bflag, pct = b
        if bflag in (128, 255) or pct == 255:
            m = _note_once('battery_gone',
                           '注意: 电池读数异常(ACPI BatteryFlag=%s, 电量=%s%%) —— 典型的 EC/电池'
                           '链路失联(电池报告会显示 -1 mWh)。别拔适配器; 处置: 关机 -> 拔适配器'
                           '-> 长按电源键 30 秒 -> 插回开机(手册 03 §9)。' % (bflag, pct))
            if m:
                notes.append(m)
    if _G['hibernate']:
        _G['hibernate'] = False
        do_hibernate()
    return notes


# ---------------------------------------------------------------- 轮询节奏
# EC 是慢速总线, 本机实测偶尔还会串位/超时(ACPI 事件 ID 15/13), 所以能少打扰就少打扰:
# 主循环只碰 MSR/MMIO(功耗墙 + CPU DTS), 保持 5 秒; 温度/风扇/固件标志走 EC, 15/30 秒一次。
# 温度是慢变量(44°C 到 95°C 要几分钟), 风扇本身响应也是几十秒级, 所以 15 秒不误事。
def guard_level():
    """当前过热保护等级: None / 'warn' / 'crit' / 'trip' / 'escape'"""
    return _G.get('level')


def last_ec_temp():
    """最近一次读到的 EC 温度(可能有点旧; 判断要不要加密标志轮询用)"""
    return _G.get('last_et')


def temp_jump_pending():
    """上一轮 EC 温度跳变太大、被当成可疑值沿用了吗? -> 调用方应把下一轮 EC 读提前。

    2026-10-19: ec_poll_s=15 的下, 一次真升温就能超过 GUARD_MAX_JUMP(实测 8 线程 15 秒
    涨 ~40°C), 光靠"沿用上一轮"会让风扇白等一整个 ec_poll_s 才升档 —— 现场因此摸到过
    99°C。现在跳变被压住的下一轮按 EC_JUMP_RETRY_S(2 秒)再读一次, 连续两轮都跳就认。
    """
    return bool(_G.get('et_hold'))


def flag_hot_c(cfg):
    """EC 温度到它就恢复"每次 EC 轮询都读固件标志"的阈值"""
    return float(guard_cfg(cfg).get('flag_hot_c') or 85.0)


class _FILETIME64(ctypes.Structure):
    _fields_ = [('dwLowDateTime', w.DWORD), ('dwHighDateTime', w.DWORD)]


def _ft64(v):
    return (v.dwHighDateTime << 32) | v.dwLowDateTime


_LOAD = {'last': None, 'ema': None}


def cpu_load_pct():
    """系统 CPU 占用率(%): GetSystemTimes 的时间差, 二次调用起才有值(第一次返回 None)。

    给风扇前馈用: 不碰 EC、不读 MSR、不打开句柄, 纯 Windows API, 代价可以忽略。
    指数平滑(0.6/0.4)压掉瞬时毛刺 —— 前馈要的是"持续高负载", 不是某一帧的尖峰。
    """
    try:
        idle, kern, user = _FILETIME64(), _FILETIME64(), _FILETIME64()
        if not k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                  ctypes.byref(user)):
            return None
        i, k, u = _ft64(idle), _ft64(kern), _ft64(user)
        last = _LOAD['last']
        _LOAD['last'] = (i, k, u)
        if last is None:
            return None
        di = i - last[0]
        tot = (k - last[1]) + (u - last[2])
        if tot <= 0:
            return _LOAD['ema']
        busy = max(0.0, min(1.0, (tot - di) / float(tot))) * 100.0
        _LOAD['ema'] = busy if _LOAD['ema'] is None else (_LOAD['ema'] * 0.6 + busy * 0.4)
        return _LOAD['ema']
    except Exception:
        return None


def poll_plan(cfg, fast=False, guard=False, hot=False, jump=False):
    """统一算三个轮询间隔(秒) -> (主循环, EC 读, 固件标志读)。

      fast  —— 开机/唤醒后前几分钟: 档位和风扇要尽快收敛, 三个都加密;
      guard —— 过热保护生效期间: 温度和标志都得盯紧, EC 读提到跟主循环同频;
      hot   —— EC 温度高于 flag_hot_c: 标志改跟 EC 读同频(平时 30 秒一次);
      jump  —— ★上一轮 EC 温度跳变被当可疑值压住了(temp_jump_pending): EC 读提前到
               EC_JUMP_RETRY_S, 赶紧再确认一次, 别让风扇白等一个 ec_poll_s。
    """
    d = DEFAULT_CFG
    poll = float(cfg.get('poll_s') or d['poll_s'])
    ec = float(cfg.get('ec_poll_s') or d['ec_poll_s'])
    fl = float(cfg.get('flag_poll_s') or d['flag_poll_s'])
    if fast:
        poll, ec, fl = min(poll, 2.0), min(ec, 5.0), min(fl, 10.0)
    if jump:
        ec, fl = min(ec, EC_JUMP_RETRY_S), min(fl, EC_JUMP_RETRY_S)
    if guard:
        ec, fl = min(ec, poll), min(fl, poll)
    elif hot:
        fl = min(fl, ec)
    return max(1.0, poll), max(2.0, ec), max(2.0, fl)


# ---------------------------------------------------------------- 命令
def cmd_check():
    global _no_log
    _no_log = True      # 体检输出只上屏幕、不进 fix.log（以前每跑一次就往日志里灌一整屏）
    if not ensure_driver():
        out('[X] 驱动加载失败, 无法访问硬件寄存器')
        _log_line('[X] 手动体检: 驱动加载失败')
        return 1
    out('驱动版本: %s' % struct.unpack('<I', ioctl(IOCTL_VERSION, b'', 4)[1][:4])[0])

    cfg = load_cfg()
    mb = mchbar()
    msr = rdmsr(MSR_PKG_POWER_LIMIT)
    mmio = rdphys(mb + MMIO_PL_OFF, 8) if mb else None
    d_m = decode_pl(msr) if msr is not None else None
    d_o = decode_pl(mmio) if mmio is not None else None
    mode = mmio_mode(cfg)

    ratio = (rdmsr(MSR_PERF_STATUS) or 0) >> 8 & 0xFF

    out('')
    out('CPU 当前倍频      : %d  (约 %.2f GHz)' % (ratio, ratio / 10.0))
    out('MCHBAR            : %s' % (('0x%X' % mb) if mb else '读取失败'))
    out('当前电源          : %s' % ('AC 适配器' if on_ac() else '电池'))
    print_battery_check(out)
    print_version_check(out)
    if d_m:
        out('生效档位          : [%s] MSR PL1=%.2fW PL2=%.2fW (mmio_mode=%s)'
            % (_pick_profile(cfg)[2], d_m['pl1_w'], d_m['pl2_w'], mode))
    out('')
    out('%-26s %-22s %s' % ('', 'MSR 0x610 (系统侧)', 'MMIO +0x59A0 (固件侧)'))
    if d_m and d_o:
        if mode == 'disable':
            mmio_desc = ('清零+锁(已禁用)' if mmio == MMIO_DISABLED_LOCKED
                         else 'PL1=%.2fW(未禁用!)' % d_o['pl1_w'])
            out('%-26s %-22s %s' % ('PL1 长时功耗墙', '%.2f W' % d_m['pl1_w'], mmio_desc))
            out('%-26s %-22s %s' % ('PL2 短时功耗墙', '%.2f W' % d_m['pl2_w'], '—'))
            out('%-26s %-22s %s' % ('锁定位 bit63', d_m['lock'], d_o['lock']))
            out('')
            if mmio == MMIO_DISABLED_LOCKED:
                out('生效值 = MSR 侧 = %.2fW (MMIO 已清零禁用, 不参与 min)' % d_m['pl1_w'])
            else:
                out('生效值 = min(MSR, MMIO) = %.2fW' % min(d_m['pl1_w'], d_o['pl1_w']))
            if mmio == MMIO_DISABLED_LOCKED:
                out('[√] 当前状态正常 (固件侧已禁用, 功耗墙由 MSR 控制)')
            else:
                out('[!] MMIO 未处于"清零+锁"终态 —— disable 模式需重启后生效')
                out('    (或把 config 的 mmio_mode 改回 value, 老方案)')
        else:
            out('%-26s %-22s %s' % ('PL1 长时功耗墙', '%.2f W' % d_m['pl1_w'], '%.2f W' % d_o['pl1_w']))
            out('%-26s %-22s %s' % ('PL2 短时功耗墙', '%.2f W' % d_m['pl2_w'], '%.2f W' % d_o['pl2_w']))
            out('%-26s %-22s %s' % ('锁定位 bit63', d_m['lock'], d_o['lock']))
            out('')
            if d_m['pl1_w'] >= d_o['pl1_w'] - 0.5 and d_o['lock']:
                out('[√] 当前状态正常')
            else:
                if abs(d_m['pl1_w'] - d_o['pl1_w']) > 0.5:
                    out('[!] 两侧 PL1 不一致 —— 这正是降频的根源')
                if not d_o['lock']:
                    out('[!] MMIO 未锁定 —— 固件随时可以再把它改低')
    else:
        out('[X] 寄存器读取失败')

    # ---- 降频自查: 温度(注意 DTS 换算公式) ----
    out('')
    out('%-26s %s' % ('', '降频自查'))
    tv = cpf_read_thermal()
    if tv:
        if tv['temp'] >= 90:
            # 实测教训(2026-10-18 23:12): 同一时刻 DTS=95°C, 而倍频还有 4.30GHz、EC 0x78 只有 85°C。
            # DTS 是"TjMax 减读数"的估算口径, 满载会失真 —— 光看它就喊"温度墙限频"会误导。
            # 所以这里要结合倍频一起判。
            ecs = ec_temp()
            if ratio <= 20:
                out('CPU 温度           : %d °C  [!] 高, 且倍频只有 %.1fGHz —— 像被温度墙压住了'
                    % (tv['temp'], ratio / 10.0))
                out('                     (治本: 清灰换硅脂; 或把 pl1_w 降到 35~40)')
            else:
                out('CPU 温度           : %d °C  (DTS 口径; 但倍频 %.1fGHz 正常 → 多半只是满载失真, 没被限住)'
                    % (tv['temp'], ratio / 10.0))
                if ecs is not None:
                    out('%s %d °C  ← 过热保护看的是这个 (预警 %.0f°C)'
                        % (_pad('EC 温度 (0x78)', 26), ecs, guard_cfg(cfg)['warn_c']))
                    out('                     注: 本机满载时 DTS 比 EC 0x78 高约 10°C, 两个刻度别混着看')
        else:
            out('CPU 温度           : %d °C  (TjMax=%d, 公式 TjMax-DTS; 仅空闲可信, 满载会失真)'
                % (tv['temp'], tv['tjmax']))
            if ratio <= 8:
                out('[!] 倍频只有 %.1fGHz —— 空闲态确实异常, 看上面的功耗墙状态' % (ratio / 10.0))

    # ---- 风扇 / 过热保护 ----
    out('')
    out('%-26s %s' % ('', '风扇 / 过热保护'))
    cur = t = r = None
    fl, hit = {}, []
    try:
        f = fan_cfg(cfg)
        g = guard_cfg(cfg)
        cur = ec_hfsp()
        t = ec_temp()
        r = ec_rpm()
        fl = ec_trip_flags(t)
        hit = ec_tripped(fl)
        out('%s %s' % (_pad('风扇档位 HFSP', 26),
                       ('%s (0x%02X)' % (fan_name(cur), cur)) if cur is not None else '读取失败'))
        out('%s %s' % (_pad('风扇转速', 26), ('%d RPM' % r) if r is not None else '读取失败'))
        out('%s %s' % (_pad('EC 温度 (0x78)', 26), ('%d °C' % t) if t is not None else '读取失败'))
        et = ec_extra_temps()
        if et:
            out('%s %s' % (_pad('其它 EC 温度', 26),
                           '  '.join('%s=0x%02X/%d°C' % ({0x7A: 'TTS1', 0x7B: 'TTS6', 0x7C: 'TTS2',
                                                          0x7D: 'TTS3', 0x7E: 'TTS4', 0xC9: 'HWAT'}.get(k, '?'),
                                                         k, v) for k, v in sorted(et.items()))
                           + '   (另一组传感器, 只记录, 不参与逻辑)'))
        if fl:
            _s = flags_desc(fl)
            if hit:
                _nowt = ec_temp()
                _hot = (_nowt is not None and _nowt >= g['warn_c'])
                _s += ('   [!] 已置位 —— ' + ('且温度也到 %.0f°C, 按最高级处理'
                                              % g['warn_c'] if _hot else
                                              '但温度正常: 这是固件"记录过一次过热"的**锁存位**, '
                                              '不是现在就热 → 只拉风扇降温, 不砍到 10W'))
            else:
                _s += '   (正常)'
        else:
            _s = '读取失败'
        out('%s %s' % (_pad('固件过热标志', 26), _s))
        _l1 = cpu_load_pct()
        time.sleep(0.5)
        _l2 = cpu_load_pct()
        if _l2 is not None:
            _lb = fan_cfg(cfg)
            out('%s %.1f%%   (风扇负载前馈: >=%.0f%% 满 %.0f 秒 -> %d 档)'
                % (_pad('CPU 占用', 26), _l2, _lb.get('load_boost_pct', 70.0),
                   _lb.get('load_boost_s', 20.0), _lb.get('load_boost_level', 64)))
        _n, _b, _ms = ec_bus_stats()
        out('%s 本进程事务 %d 次 / 串位 %d 次, 当前自适应等待 %.1f ms'
            % (_pad('EC 总线', 26), _n, _b, _ms))
        out('%s %s' % (_pad('风扇模式', 26), '%s   (config.json -> fan.mode)' % f.get('mode')))
        if str(f.get('mode')).lower() == 'curve':
            out('%s %s' % (_pad('温度曲线', 26),
                           ' '.join('≤%d°C->%s' % (hi, '满速' if lv >= FAN_MAX else '%d档' % lv)
                                    for hi, lv in f['curve'][:-1]) + '  更高->满速'))
        out('%s %s' % (_pad('过热保护', 26),
                       ('已开启  预警 %.0f°C->PL1 %.0fW  严重 %.0f°C->PL1 %.0fW  逃生 %.0f°C%s'
                        % (g['warn_c'], g['warn_pl1_w'], g['crit_c'], g['crit_pl1_w'], g['escape_c'],
                           '(主动休眠)' if g.get('escape_hibernate') else ''))
                       if g.get('enabled') else '已关闭'))
        last = _last_guard_line()
        if last:
            out('%s %s' % (_pad('最近一次保护记录', 26), last))
    except Exception as e:
        out('风扇/过热保护状态读取失败: %s' % e)

    # ---- 值守状态: 确认"开机 / 睡眠唤醒"真的有人管 ----
    out('')
    out('%-26s %s' % ('', '值守状态'))
    e, running = svc_state(SVC_TOOL)
    if e and running:
        s = '运行中  (开机自动启动, 唤醒/拔插电 1 秒内响应)'
    elif e:
        s = '已注册但没在运行  [!] 开机不会自动修'
    else:
        s = '未安装  [!] 只有计划任务在管'
    out('%s %s' % (_pad('看守服务 CpuPLFixSvc', 26), s))
    out('%s %s' % (_pad('日志最近一次记录', 26), _last_log_head()))

    # ---- 给日志留一行摘要（体检本身只上屏幕）----
    try:
        _log_line('[*] 手动体检: %s  MSR PL1=%s PL2=%s  倍频 %d  EC温度 %s  DTS %s  风扇 %s %s  固件标志 %s'
                  % ('AC' if on_ac() else '电池',
                     ('%.2fW' % d_m['pl1_w']) if d_m else '?',
                     ('%.2fW' % d_m['pl2_w']) if d_m else '?',
                     ratio,
                     ('%d°C' % t) if t is not None else '--',
                     ('%d°C' % tv['temp']) if tv else '--',
                     fan_name(cur),
                     ('%dRPM' % r) if r is not None else '--',
                     flags_desc(fl) if fl else '--'))
    except Exception:
        pass
    return 0


def _last_guard_line():
    """从日志里找最近一条"过热保护"动作记录 —— 保护等级是服务进程里的状态, check 看不到。
    只认带方括号的等级行(过热保护[预警]/[严重]/[逃生]/解除), 免得匹配到风扇状态里的
    "过热保护 : 已开启"。"""
    try:
        p = os.path.join(LOG_DIR, 'fix.log')
        if not os.path.exists(p):
            return None
        with open(p, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()[-400:]
        for s in reversed(lines):
            s = s.strip()
            if '过热保护[' in s or '过热保护解除' in s:
                return s[:100]
    except Exception:
        pass
    return None


def _pad(s, width):
    """按显示宽度补空格(中文算 2 格), 让中文标签也能对齐"""
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in s)
    return s + ' ' * max(1, width - w)


def _last_log_head():
    """返回日志里最后一条分隔头, 例如 '2026-10-17 20:46:07 svc'"""
    try:
        with open(os.path.join(LOG_DIR, 'fix.log'), 'r', encoding='utf-8', errors='replace') as f:
            hdr = [ln.strip() for ln in f if ln.startswith('=====')]
        if not hdr:
            return '无记录'
        return hdr[-1].strip('= ').strip()
    except Exception as e:
        return '读取失败 (%s)' % e


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [('ACLineStatus', ctypes.c_byte),
                ('BatteryFlag', ctypes.c_byte),
                ('BatteryLifePercent', ctypes.c_byte),
                ('SystemStatusFlag', ctypes.c_byte),
                ('BatteryLifeTime', ctypes.c_ulong),
                ('BatteryFullLifeTime', ctypes.c_ulong)]


def on_ac():
    """True = 接着适配器(AC), False = 电池供电。读不到时按 AC 处理(宁可费电不可变慢)。"""
    try:
        s = _SYSTEM_POWER_STATUS()
        if k32.GetSystemPowerStatus(ctypes.byref(s)):
            return s.ACLineStatus == 1
    except Exception:
        pass
    return True


def print_version_check(out):
    """部署漂移检查: 现在跑的 CpuPLFix.py 与工具仓库 %APPDATA%\\_repo 里那份是不是同一份。

    2026-10-19 踩过: 跑着的代码不是磁盘上的代码(别的 agent 打了补丁又整体回滚),
    排查时白绕了一大圈。以后 2-check 第一屏就有这一行。
    """
    import hashlib

    def h(p):
        try:
            with open(p, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()[:12]
        except Exception:
            return None

    me = os.path.abspath(__file__)
    repo = os.path.join(os.path.dirname(os.path.dirname(me)), '_repo', 'CpuPLFix.py')
    a, b = h(me), h(repo)
    if b is None:
        out('版本一致性       : %s (仓库 %s 不在, 不做对比)' % (a, repo))
    elif a == b:
        out('版本一致性       : [√] 与 _repo 一致 (%s)' % a)
    else:
        out('版本一致性       : [!] 与 _repo 不一致 (%s vs %s) —— 跑的这份不是仓库里那份'
            % (a, b))


def battery_flags():
    """(ACLineStatus, BatteryFlag, BatteryLifePercent); BatteryFlag: 1=高 2=低 4=危急
    8=充电中 128=没有系统电池 255=未知(读数无效)。取不到返回 None。

    2026-10-19 那次 EC/电池链路失联时就是 255 —— 这就是"电池显示 -1"的机器侧判据。
    """
    try:
        s = _SYSTEM_POWER_STATUS()
        if k32.GetSystemPowerStatus(ctypes.byref(s)):
            return s.ACLineStatus, s.BatteryFlag, s.BatteryLifePercent
    except Exception:
        pass
    return None


def battery_ec_state():
    """从 EC 侧看电池: (0x38 HB0S/HB0A, 0xB0 起充, 0xB1 停充)。只读, 失败给 None。"""
    return ec_read_ok(0x38), ec_read_ok(0xB0), ec_read_ok(0xB1)


def print_battery_check(out):
    """2-check 里那几行电池状态(Windows 侧 + EC 侧), 外加链路结论。"""
    b = battery_flags()
    if b:
        ac_line, bflag, pct = b
        out('电池(系统侧)     : %s  BatteryFlag=%s 电量=%s%%'
            % ('AC 适配器' if ac_line == 1 else '电池供电', bflag,
               '--' if pct == 255 else pct))
    else:
        out('电池(系统侧)     : 读不到(GetSystemPowerStatus 失败)')
    hb0, bsc, bec = battery_ec_state()
    if hb0 is None:
        out('电池(EC 0x38)    : 读取失败')
    else:
        out('电池(EC 0x38)    : 0x%02X  HB0S=%d HB0A=%d%s'
            % (hb0, hb0 & 0x7F, (hb0 >> 7) & 1,
               '  [X] EC 认为**没有电池**(HB0S&7==7) —— 查电池排线/电池包' if (hb0 & 7) == 7 else ''))
    if bsc is None or bec is None:
        out('电池充电阈值     : 读取失败')
    else:
        bad = not (1 <= bsc <= 100 and 1 <= bec <= 100 and bec > bsc)
        out('电池充电阈值     : 0xB0=0x%02X(%d%%) 起充 / 0xB1=0x%02X(%d%%) 停充%s'
            % (bsc, bsc, bec, bec,
               '  [X] 阈值不合法(要 1..100 且 停充>起充)' if bad
               else ('  (0/0 = 不限制)' if (bsc == 0 and bec == 0) else '')))
    if (b and (b[1] in (128, 255) or b[2] == 255)) or (hb0 is not None and (hb0 & 7) == 7):
        out('  [X] 电池链路异常 —— 界面会显示 -1 / 没电池。处置: 关机 -> 拔适配器 -> '
            '长按电源键 30 秒 -> 插回开机(EC 断电复位, 手册 03 §9)')
    else:
        out('  [√] 电池链路看起来正常')


MMIO_DISABLED_LOCKED = 1 << 63     # MMIO 清零 + 锁: 禁用固件侧, MSR 全权控制


def mmio_mode(cfg):
    """固件侧策略:
    'value'   = 把目标 PL 写进 MMIO 再锁(老方案, 锁死后档位不可变, 改配置要重启)
    'disable' = MMIO 清零 + 锁(焊死"禁用"状态), 之后所有档位只写 MSR, 免重启
    实测本机 MSR 0x610 可写, 'disable' 是默认推荐。
    """
    return 'disable' if str(cfg.get('mmio_mode', 'disable')).lower() == 'disable' else 'value'


_NOTED = set()          # 已经提示过的状态, 避免看守循环里反复刷同一句话


def _note_once(key, msg):
    """同一条提示一个进程只出一次; 返回 None 表示已提示过"""
    if key in _NOTED:
        return None
    _NOTED.add(key)
    return msg


def cpf_read_thermal():
    """按 Intel SDM 正确公式读 CPU 温度: 温度 = TjMax - DTS。

    MSR 0x1B1 bits 22:16 是"距 TjMax 的余量"而不是摄氏度(早期脚本直接当温度读,
    得出过"满载 0°C"的荒谬值)。TjMax 从 0x1A2 读。
    返回 {'temp', 'tjmax', 'thermal_status'(=1 表示已到节流点)}; 失败返回 None。
    注意: DTS 空闲时可信; 满载时该寄存器行为异常(实测读数反而走低), 别迷信。
    """
    try:
        v = rdmsr(0x1A2)
        tjmax = ((v >> 16) & 0xFF) if v else 100
        if tjmax < 50 or tjmax > 120:
            tjmax = 100
        tv = rdmsr(0x1B1)
        if tv is None:
            return None
        return {'temp': tjmax - ((tv >> 16) & 0x7F),
                'tjmax': tjmax,
                'thermal_status': tv & 1}
    except Exception:
        return None


def _pick_profile(cfg):
    """按当前电源状态选档。返回 (pl1, pl2, 档位名); pl1=None 表示自动模式。
    过热保护生效时由 _PL_OVERRIDE 覆盖(看守服务每轮重新对齐)。"""
    if _PL_OVERRIDE:
        return _PL_OVERRIDE[0], _PL_OVERRIDE[1], '过热保护'
    c1 = float(cfg['pl1_w'] or 0)
    c2 = float(cfg['pl2_w'] or 0)
    b1 = float(cfg.get('pl1_w_batt') or 0)
    b2 = float(cfg.get('pl2_w_batt') or 0)
    if c1 <= 0:                      # AC 档都没配 -> 自动模式
        return None, None, '自动'
    if (not on_ac()) and b1 > 0:     # 电池供电且配了电池档
        p1, p2 = b1, b2
        name = '电池/平衡'
    else:
        p1, p2 = c1, c2
        name = 'AC/高性能'
    if p2 <= 0:
        p2 = max(25.0, p1 * 1.25)    # 只填了 pl1, PL2 给个合理的
    if p2 < p1:
        p2 = p1
    return max(p1, MIN_PL_W), p2, name


def sync_msr(pl1, pl2):
    """把 MSR 0x610 的 PL1/PL2 同步到目标档。

    保留 time window / enable 位 / MSR 自己的锁位(bit63) 一概不动。
    用途: 运行中切档 —— CPU 取 min(MSR, MMIO), 把 MSR 压低立刻生效,
    不需要重启; 抬高则受 MMIO 锁限制(见 ensure_profile 的提示)。
    """
    v = rdmsr(MSR_PKG_POWER_LIMIT)
    if v is None:
        return False
    d = decode_pl(v)
    new = build_pl(pl1, pl2, d['pl1_t'] or 35, d['pl2_t'] or 66, bool(d['lock']))
    if new == v:
        return True
    wrmsr(MSR_PKG_POWER_LIMIT, new)
    return True


def ensure_profile(cfg):
    """按当前电源状态把 MSR 和 MMIO 都对齐到目标档(幂等, 可反复调用)。

    disable 模式: MMIO 只要不是"清零+锁"就补上, 其余全走 MSR —— 免重启切档。
    value 模式:   MMIO 写目标值+锁, MSR 顺手同步(切档只能压低, 抬升受锁限制)。
    返回变化描述字符串; 无变化返回 None。看守服务/计划任务每轮都调它,
    AC <-> 电池切换后 5 秒内生效(服务还能被电源事件立即唤醒)。
    """
    msr = rdmsr(MSR_PKG_POWER_LIMIT)
    if msr is None:
        return None
    d_m = decode_pl(msr)
    pl1, pl2 = _target_pl(cfg, d_m)
    _, _, name = _pick_profile(cfg)
    notes = []
    mode = mmio_mode(cfg)

    # 1) MSR 侧同步 —— disable 模式下的唯一控制通道, value 模式下的实时下调通道
    if pl1 is not None and (abs(d_m['pl1_w'] - pl1) > 0.5 or abs(d_m['pl2_w'] - pl2) > 0.5):
        try:
            sync_msr(pl1, pl2)
            notes.append('MSR PL1 %.1f->%.1fW PL2 %.1f->%.1fW'
                         % (d_m['pl1_w'], pl1, d_m['pl2_w'], pl2))
        except Exception as e:
            notes.append('MSR 同步失败: %s' % e)

    # 2) MMIO 侧
    mb = mchbar()
    if mb:
        v = rdphys(mb + MMIO_PL_OFF, 8)
        if v is not None:
            d_o = decode_pl(v)
            if mode == 'disable':
                if v == MMIO_DISABLED_LOCKED:
                    pass                 # 已是终态: 清零+锁, 以后什么都不用动
                elif d_o['lock']:
                    # MMIO 已被别的值锁死(比如老版本工具锁的 40W)。锁定位不可逆,
                    # 本加电周期内改不了 —— 不重复写, 只提示一次, 重启后由开机流程纠正。
                    n = _note_once('locked_stale',
                                   'MMIO 已锁 %.1fW 无法在本周期改为清零, disable 模式需重启后生效'
                                   % d_o['pl1_w'])
                    if n:
                        notes.append(n)
                else:
                    # 未锁(开机时固件打回的坏值) -> 一次性清零+锁
                    # 注意必须写 8 字节: wrphys 默认 size=4, 而清零+锁的值是 bit63=1 的
                    # 64 位数, 按 4 字节写会抛 "int too big to convert", 于是服务每次唤醒
                    # 都静默修复失败(2026-10-18 实测: 睡眠唤醒后 MMIO 被固件打回 55W,
                    # 服务报这个异常, 只能等计划任务里的 cmd_fix 兜底)。
                    wrphys(mb + MMIO_PL_OFF, MMIO_DISABLED_LOCKED, 8)
                    back = rdphys(mb + MMIO_PL_OFF, 8)
                    if back == MMIO_DISABLED_LOCKED:
                        notes.append('MMIO 清零+锁(固件侧已禁用, MSR 全权)')
                    else:
                        n = _note_once('disable_failed', 'MMIO 清零+锁未生效')
                        if n:
                            notes.append(n)
            else:
                t1 = d_m['pl1_t'] or int(cfg['pl1_time'])
                t2 = d_m['pl2_t'] or int(cfg['pl2_time'])
                if d_o['pl1_w'] < pl1 - 0.5 or (not d_o['lock'] and cfg.get('lock_mmio', True)):
                    wrphys(mb + MMIO_PL_OFF,
                           build_pl(pl1, pl2, t1, t2, bool(cfg.get('lock_mmio', True))), 8)
                    back = rdphys(mb + MMIO_PL_OFF, 8)
                    d_b = decode_pl(back) if back is not None else None
                    if d_b and d_b['pl1_w'] < pl1 - 0.5:
                        n = _note_once('value_locked_%.0f' % d_b['pl1_w'],
                                       'MMIO 已锁 %.1fW, 升档需重启' % d_b['pl1_w'])
                        if n:
                            notes.append(n)
    if notes:
        return '电源档位[%s/%s]: %s' % (name, mode, '; '.join(notes))
    return None


def _target_pl(cfg, d_m):
    """算出应该写进 MMIO 的目标功耗墙。cmd_fix / cmd_boot 共用同一套规则。"""
    pl1, pl2, name = _pick_profile(cfg)
    if pl1 is not None:                 # 用户明确指定 -> 精确目标值(按 AC/电池选档)
        return pl1, pl2
    c1 = float(cfg['pl1_w'] or 0)
    if c1 > 0:                          # 理论上到不了这里, 兜底
        return max(c1, MIN_PL_W), (float(cfg['pl2_w'] or 0) or max(c1 * 1.25, 25.0))
    if d_m['pl1_w'] >= cfg['min_pl1_w']:    # 自动: 镜像 MSR
        return d_m['pl1_w'], d_m['pl2_w']
    tdp = rated_tdp()
    pl1 = tdp or 25.0
    pl2 = round(tdp * 1.25, 2) if tdp else round(pl1 * 1.25, 2)
    out('[*] MSR 里的 PL1 只有 %.2fW, 判定为异常; 兜底取 CPU 标称 TDP: PL1=%.2fW PL2=%.2fW'
        % (d_m['pl1_w'], pl1, pl2))
    return pl1, pl2


def cmd_fix(quiet=False):
    cfg = load_cfg()
    if not ensure_driver(verbose=not quiet):
        out('[X] 驱动加载失败')
        return 1
    mb = mchbar()
    if not mb:
        out('[X] 读不到 MCHBAR')
        return 1

    msr = rdmsr(MSR_PKG_POWER_LIMIT)
    if msr is None:
        out('[X] 读不到 MSR 0x610')
        return 1
    d_m = decode_pl(msr)
    mmio_addr = mb + MMIO_PL_OFF
    mmio_before = rdphys(mmio_addr, 8)
    d_o = decode_pl(mmio_before) if mmio_before is not None else None

    # 目标功耗墙
    pl1, pl2 = _target_pl(cfg, d_m)
    _, _, prof_name = _pick_profile(cfg)
    t1 = d_m['pl1_t'] or int(cfg['pl1_time'])
    t2 = d_m['pl2_t'] or int(cfg['pl2_time'])

    out('修复前: MSR 0x610=0x%016X PL1=%.2fW PL2=%.2fW | MMIO +0x59A0=0x%016X PL1=%.2fW PL2=%.2fW lock=%s'
        % (msr, d_m['pl1_w'], d_m['pl2_w'],
           mmio_before if mmio_before is not None else 0,
           d_o['pl1_w'] if d_o else -1, d_o['pl2_w'] if d_o else -1,
           d_o['lock'] if d_o else '?'))
    mode = mmio_mode(cfg)
    if mode == 'disable':
        out('目标    : MMIO 清零+锁(固件侧禁用, 之后只写 MSR, 免重启切档) [%s]' % prof_name)
        if pl1 is not None:
            out('         MSR 设为 PL1=%.2fW PL2=%.2fW (%.0f秒/%.0f秒)' % (pl1, pl2, t1, t2))
        target = MMIO_DISABLED_LOCKED
    else:
        out('目标    : PL1=%.2fW PL2=%.2fW (%.0f秒/%.0f秒) [%s]%s'
            % (pl1, pl2, t1, t2, prof_name, ', 锁定' if cfg.get('lock_mmio', True) else ', 不锁定'))
        target = build_pl(pl1, pl2, t1, t2, bool(cfg.get('lock_mmio', True)))

    ok = False
    mmio_locked_stale = False
    if mode == 'disable' and d_o and d_o['lock'] and mmio_before != MMIO_DISABLED_LOCKED:
        # 锁定位不可逆: MMIO 已被旧值锁死(如老版本锁的 40W), 本周期改不了, 别白写
        mmio_locked_stale = True
        ok = False
        out('跳过    : MMIO 已锁 %.1fW, 锁定位本加电周期不可逆, 无法改为清零; 重启后生效' % d_o['pl1_w'])
    if not mmio_locked_stale:
        for attempt in range(3):
            wrphys(mmio_addr, target, 8)
            time.sleep(0.15)
            back = rdphys(mmio_addr, 8)
            if back == target:
                ok = True
                break
            time.sleep(0.5)

    # MSR 侧同步到目标档 —— disable 模式下这是唯一控制通道, value 模式下是实时下调通道。
    # 2026-10-18 实测本机 MSR 0x610 可写(写-0.125W 后回读确认)。
    # 不置 MSR 自己的锁位(bit63), 免得以后 ThrottleStop 改不动。
    msr_after = None
    if pl1 is not None:
        try:
            if sync_msr(pl1, pl2):
                msr_after = decode_pl(rdmsr(MSR_PKG_POWER_LIMIT))
                out('MSR 同步 : PL1=%.2fW PL2=%.2fW' % (msr_after['pl1_w'], msr_after['pl2_w']))
        except Exception as e:
            out('[!] MSR 同步失败(不影响 MMIO 锁定): %s' % e)

    back = rdphys(mmio_addr, 8)
    d_b = decode_pl(back) if back is not None else None
    lock_ok = False
    if d_b:
        lock_ok = (not cfg.get('lock_mmio', True)) or (d_b['lock'] == 1)
        if mode == 'disable':
            out('修复后: MMIO 0x%016X (清零+锁%s)' % (back, ' [√] 固件侧已禁用' if ok else ' [X] 未生效'))
        else:
            out('修复后: MMIO 0x%016X PL1=%.2fW PL2=%.2fW lock=%d  %s'
                % (back, d_b['pl1_w'], d_b['pl2_w'], d_b['lock'],
                   '[√] 已生效并锁定' if (ok and lock_ok) else '[X] 未生效'))
    else:
        out('[X] 回读失败')

    # 等一下再看频率有没有抬起来
    time.sleep(0.6)
    r0 = (rdmsr(MSR_PERF_STATUS) or 0) >> 8 & 0xFF
    out('当前倍频: %d (约 %.2f GHz)' % (r0, r0 / 10.0))
    return 0 if ok else 1


def cmd_save():
    if not ensure_driver():
        return 1
    mb = mchbar()
    msr = rdmsr(MSR_PKG_POWER_LIMIT)
    d = decode_pl(msr)
    if d['pl1_w'] < 15:
        out('[!] 当前 MSR PL1 只有 %.2fW, 像是降频状态, 不适合作为参考值' % d['pl1_w'])
        return 1
    cfg = load_cfg()
    cfg.update({'pl1_w': d['pl1_w'], 'pl2_w': d['pl2_w'],
                'pl1_time': d['pl1_t'], 'pl2_time': d['pl2_t']})
    save_cfg(cfg)
    out('[√] 已保存为插电(AC)档: PL1=%.2fW PL2=%.2fW' % (d['pl1_w'], d['pl2_w']))
    out('    电池档 pl1_w_batt/pl2_w_batt 未改动; disable 模式下 1 分钟内 MSR 生效, 无需重启')
    return 0


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>开机与睡眠唤醒后修复 CPU 功耗墙(MMIO Lock)</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT1S</Delay>
      <Repetition>
        <Interval>PT5M</Interval>
        <Duration>PT30M</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </BootTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT3S</Delay>
    </LogonTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="System"&gt;&lt;Select Path="System"&gt;*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
      <Delay>PT3S</Delay>
    </EventTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="System"&gt;&lt;Select Path="System"&gt;*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=107]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
      <Delay>PT3S</Delay>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>0</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(exe)s</Command>
      <Arguments>"%(script)s" boot</Arguments>
      <WorkingDirectory>%(dir)s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _pyw():
    """优先用 pythonw.exe(无控制台窗口)"""
    exe = sys.executable
    pw = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    return pw if os.path.exists(pw) else exe


def _quote_if_space(p):
    return '"%s"' % p if ' ' in p else p


def _create_tool_svc(binpath):
    ok, msg = svc_create(SVC_TOOL, binpath, 'CpuPLFix 功耗墙/风扇看守',
                         SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL)
    if not ok:
        rc, txt = _sc('create', SVC_TOOL, 'type=', 'own', 'start=', 'auto',
                      'error=', 'normal', 'DisplayName=', 'CpuPLFix 功耗墙/风扇看守',
                      'binPath=', binpath)
        if rc != 0:
            return False, msg or txt.strip()[:200]
    return True, ''


def _install_tool_service():
    """把 CpuPLFixSvc 注册成自动启动的 Windows 服务"""
    script = os.path.join(BASE, 'CpuPLFixSvc.py')
    if not os.path.exists(script):
        return False, '找不到 CpuPLFixSvc.py'
    binpath = '"%s" "%s" --service' % (_pyw(), script)

    exists, _running = svc_state(SVC_TOOL)
    if exists:
        # 必须先停再删: DeleteService 对运行中的服务只是"标记删除",
        # 紧接着 CreateService 会报 1072(服务已标记为删除)。
        svc_delete(SVC_TOOL)
        for _ in range(24):                 # 最多等 12 秒, 等旧服务真正消失
            if not svc_state(SVC_TOOL)[0]:
                break
            time.sleep(0.5)

    ok, msg = _create_tool_svc(binpath)
    for _ in range(10):                     # 还在标记删除状态就再等等重试
        if ok:
            break
        time.sleep(1.0)
        ok, msg = _create_tool_svc(binpath)

    if not ok:
        return False, msg
    if svc_start(SVC_TOOL):
        return True, ''
    return True, '已注册, 但立即启动失败(下次开机仍会自启)'


def _remove_tool_service():
    return svc_delete(SVC_TOOL)


LEGACY_TASKS = ('ThinkFanCurve',)   # 旧版独立风扇曲线的计划任务, 合并进本工具后清理掉


def _remove_legacy_tasks():
    """清掉历史遗留的独立风扇任务, 免得它和本工具的服务抢 EC 寄存器"""
    gone = []
    for t in LEGACY_TASKS:
        try:
            p = subprocess.run(['C:\\Windows\\System32\\schtasks.exe', '/delete', '/tn', t, '/f'],
                               capture_output=True, text=True, errors='replace',
                               creationflags=0x08000000)
            if p.returncode == 0:
                gone.append(t)
        except Exception:
            pass
    return gone


def cmd_install():
    if not is_admin():
        out('[!] 需要管理员权限, 正在请求提权...')
        return 0 if elevate() else 1

    if not ensure_driver_file():
        return 1

    # 1. 驱动服务(已存在且可用就不动它, 避免把能用的环境搞坏)
    exists, running = svc_state(SVC)
    if exists and (running or _open_dev()):
        out('[1/4] 驱动服务: 已存在且可用, 跳过重建')
    else:
        ok, msg = svc_create(SVC, '\\??\\' + SYS_DST, 'TSCtl', SERVICE_KERNEL_DRIVER,
                             SERVICE_DEMAND_START, SERVICE_ERROR_IGNORE)
        if not ok:
            rc, txt = _sc('create', SVC, 'type=', 'kernel', 'start=', 'demand',
                          'error=', 'ignore', 'binPath=', _binpath())
            ok, msg = (rc == 0), txt.strip()[:120]
        out('[1/4] 驱动服务: %s' % ('创建成功' if ok else msg))
        svc_start(SVC)

    # 2. 常驻看守服务(随系统启动, 不依赖登录)
    ok2, msg2 = _install_tool_service()
    out('[2/4] 看守服务: %s' % ('已注册并启动(随系统启动, 常驻监视)'
                               if ok2 and not msg2 else (msg2 or '注册失败')))

    # 3. 计划任务(兜底: 万一服务没起来)
    try:
        exe = _pyw()
        xml = TASK_XML % {'exe': exe, 'script': os.path.abspath(__file__), 'dir': BASE}
        tmp = os.path.join(BASE, '_task.xml')
        with open(tmp, 'w', encoding='utf-16') as f:
            f.write(xml)
        p = subprocess.run(['C:\\Windows\\System32\\schtasks.exe', '/create', '/tn', TASK_NAME,
                            '/xml', tmp, '/f'], capture_output=True, text=True, errors='replace',
                           creationflags=0x08000000)
        ok = p.returncode == 0
        out('[3/4] 计划任务: %s' % ('已创建(开机/登录/唤醒兜底)' if ok
                                  else (p.stdout + p.stderr).strip()[:200]))
        try:
            os.remove(tmp)
        except Exception:
            pass
    except Exception as e:
        out('[3/4] 计划任务: 跳过 (%s)' % e)

    # 4. 立即执行一次
    out('[4/4] 立即应用一次:')
    rc = cmd_fix(quiet=False)
    try:
        for n in tick():
            out('[*] %s' % n)
    except Exception as e:
        out('[*] 风扇/过热保护初始化异常: %s' % e)
    gone = _remove_legacy_tasks()
    if gone:
        out('[i] 已清理旧版独立风扇任务: %s (风扇现在统一由本工具看守服务管)' % ', '.join(gone))
    out('')
    out('安装完成。开机、登录、睡眠唤醒、休眠恢复都会自动修复 功耗墙 + 风扇曲线 + 过热保护, 日志在 %s' % LOG_DIR)
    return rc


def cmd_uninstall():
    if not is_admin():
        out('[!] 需要管理员权限, 正在请求提权...')
        return 0 if elevate() else 1
    p = None
    try:
        p = subprocess.run(['C:\\Windows\\System32\\schtasks.exe', '/delete', '/tn', TASK_NAME, '/f'],
                           capture_output=True, text=True, errors='replace',
                           creationflags=0x08000000)
    except Exception:
        pass
    out('[1/3] 计划任务: %s' % ('已删除' if (p and p.returncode == 0) else '不存在或删除失败'))
    gone = _remove_legacy_tasks()
    if gone:
        out('      (顺带清理旧版独立风扇任务: %s)' % ', '.join(gone))
    out('[2/3] 看守服务: %s' % ('已删除' if _remove_tool_service() else '不存在或删除失败'))
    rc, txt = _sc('delete', SVC)
    out('[3/3] 驱动服务: %s' % ('已删除' if rc == 0 else txt.strip()[:120]))
    return 0


def svc_guard_active(cfg, et=None, flags=None):
    """看守服务是否正在"压着保护档"? -> cmd_boot 用它决定让位。

    2026-10-19 实测: 计划任务 boot 与服务同时看守时, 两边各有自己的保护状态(在进程内存里,
    互相看不见), 把 PL1 在 40W↔10W 之间互改了 4 分钟 41 次。规则:
      · 服务没在跑 -> 永不让位(必须自己修: 固件每次唤醒都会压低功耗墙);
      · 服务在跑 + EC 温度 > recover_c 或固件标志置位(=服务正压着保护档)-> 让位;
      · 其余情况照常检查/修复(这是刚需, 不能因为"服务在跑"就整个让掉)。
    """
    try:
        exists, running = svc_state(SVC_TOOL)
    except Exception:
        return False
    if not (exists and running):
        return False
    if et is not None and et > guard_cfg(cfg)['recover_c']:
        return True
    return bool(ec_tripped(flags or {}))


def cmd_boot():
    """开机 / 唤醒后尽早执行。已经健康就直接退出, 不健康才修复并进看守。"""
    t_start = time.time()
    up = uptime()
    # 同一个入口既服务"开机"、也服务"睡眠/休眠唤醒"、还会被计划任务在开机后 30 分钟内
    # 每 5 分钟重复叫一次。光看 uptime 区分不了后两者 —— 以前全被记成"睡眠/休眠唤醒后",
    # 日志里就会出现"刚开机 20 分钟却说刚睡醒"的假记录。现在用 boot_trigger() 实测有没有睡。
    if up < 0:
        tag, quiet = '触发', False
    else:
        tag, quiet = boot_trigger(up)     # quiet = 重复触发且没睡过 -> 没事只记一行
    if not quiet:
        out('[*] 进程启动(%s): 系统已运行 %.1f 秒, 解释器启动耗时 %.2f 秒'
            % (tag, up, t_start - _T_IMPORT))

    cfg = load_cfg()
    if not ensure_driver(verbose=True):
        out('[X] 驱动加载失败 (系统已运行 %.1f 秒)' % uptime())
        flush_log('boot')
        return 1

    # ---- 预检: 状态正常就立刻走人, 不占 CPU ----
    healthy = False
    mode = mmio_mode(cfg)
    try:
        mb = mchbar()
        msr = rdmsr(MSR_PKG_POWER_LIMIT)
        if mb and msr is not None:
            d_m = decode_pl(msr)
            v = rdphys(mb + MMIO_PL_OFF, 8)
            if v is not None:
                d_o = decode_pl(v)
                pl1, pl2 = _target_pl(cfg, d_m)
                msr_ok = (abs(d_m['pl1_w'] - pl1) <= 0.5 and abs(d_m['pl2_w'] - pl2) <= 0.5)
                if mode == 'disable':
                    healthy = (v == MMIO_DISABLED_LOCKED) and msr_ok
                    if healthy:
                        out('[*] 已是目标状态: MMIO 清零+锁, MSR PL1=%.2fW PL2=%.2fW, 倍频 %d, 无需修复'
                            % (d_m['pl1_w'], d_m['pl2_w'],
                               (rdmsr(MSR_PERF_STATUS) or 0) >> 8 & 0xFF))
                else:
                    healthy = (d_o['lock'] == 1) and (d_o['pl1_w'] >= pl1 - 0.5)
                    if healthy:
                        out('[*] 已是目标状态 MMIO PL1=%.2fW lock=1, 倍频 %d, 无需修复'
                            % (d_o['pl1_w'], (rdmsr(MSR_PERF_STATUS) or 0) >> 8 & 0xFF))
    except Exception as e:
        out('[*] 预检异常: %s' % e)

    if healthy:
        if svc_guard_active(cfg, last_ec_temp(), _G.get('last_flags')):
            out('[*] 看守服务正在压过热保护档 —— 本任务让位(只记录, 不碰功耗墙)')
        else:
            for n in tick(cfg):         # 功耗墙已健康, 但风扇还得按曲线走
                out('[*] %s' % n)
        if quiet:
            out('[*] 定时复查: 状态正常, 无需修复 (已运行 %.0f 秒, 倍频 %d)'
                % (uptime(), (rdmsr(MSR_PERF_STATUS) or 0) >> 8 & 0xFF))
        flush_log('boot')
        return 0

    # ---- 需要修复 ----
    if quiet:                            # 正常复查不刷"进程启动", 出事时才补上现场
        out('[*] 进程启动(%s): 系统已运行 %.1f 秒' % (tag, uptime()))
    out('[*] 需要修复, 开始执行 (系统已运行 %.1f 秒)' % uptime())
    try:
        cmd_fix(quiet=True)
    except Exception as e:
        out('[*] 首次修复异常: %s' % e)
    out('[*] 首次修复结束: 用时 %.2f 秒, 系统已运行 %.1f 秒'
        % (time.time() - t_start, uptime()))
    flush_log('boot')          # 立刻落盘, 不必等看守结束

    fixed_times = 0
    last_ec = last_fl = 0.0
    while time.time() - t_start < BOOT_WATCH_SEC:
        time.sleep(2.0)
        try:
            if not ensure_driver(verbose=False):
                continue
            # disable 模式: 只需要保证 MMIO 还是"清零+锁" + MSR 跟着当前电源档
            # value 模式:  MMIO 被固件压低就按当前档重写 + 上锁
            # tick 顺带做过热保护 + 风扇曲线(EC 读按节奏走, 不跟着 2 秒循环读)
            now = time.time()
            guard = guard_level() is not None
            t = last_ec_temp()
            hot = t is not None and t >= flag_hot_c(cfg)
            _poll_i, ec_i, fl_i = poll_plan(cfg, fast=True, guard=guard, hot=hot,
                                            jump=temp_jump_pending())
            do_ec = (now - last_ec) >= ec_i
            do_fl = do_ec and (now - last_fl) >= fl_i
            if do_ec:
                last_ec = now
            if do_fl:
                last_fl = now
            if svc_guard_active(cfg, last_ec_temp(), _G.get('last_flags')):
                m = _note_once('boot_yield', '[*] 看守服务正在压过热保护档 —— 本任务让位'
                                             '(免得两边互改 MSR, 2026-10-19 实测 41 次/4 分钟)')
                if m:
                    out(m)
                    flush_log('boot')
                continue
            notes = tick(cfg, ec=do_ec, read_flags=do_fl)
            if notes:
                fixed_times += 1
                for n in notes:
                    out('[*] %4.0f 秒: %s' % (time.time() - t_start, n))
                flush_log('boot')
        except Exception as e:
            out('[*] 看守异常: %s' % e)

    try:
        if ensure_driver(verbose=False):
            mb = mchbar()
            v = rdphys(mb + MMIO_PL_OFF, 8) if mb else None
            d_m = decode_pl(rdmsr(MSR_PKG_POWER_LIMIT))
            r = (rdmsr(MSR_PERF_STATUS) or 0) >> 8 & 0xFF
            if mode == 'disable':
                out('[*] 看守结束 (调整 %d 次), MMIO 清零+锁=%s, MSR PL1=%.2fW PL2=%.2fW, 当前倍频 %d'
                    % (fixed_times, '√' if v == MMIO_DISABLED_LOCKED else '✗',
                       d_m['pl1_w'], d_m['pl2_w'], r))
            elif v is not None:
                d = decode_pl(v)
                out('[*] 看守结束 (补锁 %d 次), MMIO PL1=%.2fW lock=%d, 当前倍频 %d'
                    % (fixed_times, d['pl1_w'], d['lock'], r))
    except Exception:
        pass
    return 0


def cmd_watch(interval=3):
    out('监控模式: 每隔几秒对齐一次 功耗墙/电源档位/风扇曲线 (Ctrl+C 退出); '
        'EC(温度/风扇/固件标志)按 config 的 poll_s/ec_poll_s/flag_poll_s 节奏读')
    last_ec = last_fl = 0.0
    while True:
        try:
            if ensure_driver(verbose=False):
                cfg = load_cfg()
                now = time.time()
                guard = guard_level() is not None
                t = last_ec_temp()
                hot = t is not None and t >= flag_hot_c(cfg)
                poll_i, ec_i, fl_i = poll_plan(cfg, fast=False, guard=guard, hot=hot)
                do_ec = (now - last_ec) >= ec_i
                do_fl = do_ec and (now - last_fl) >= fl_i
                if do_ec:
                    last_ec = now
                if do_fl:
                    last_fl = now
                for note in tick(cfg, ec=do_ec, read_flags=do_fl):
                    out('%s %s' % (datetime.datetime.now().strftime('%H:%M:%S'), note))
                    flush_log('watch')
                interval = poll_i
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            out('watch 异常: %s' % e)
            time.sleep(interval)


def cmd_fan(args):
    """fan [状态|curve|max|auto|level N] —— 看/切风扇模式(写进 config.json, 持久生效)"""
    if not is_admin():
        out('[!] 需要管理员权限, 正在请求提权...')
        return 0 if elevate() else 1
    if not ensure_driver():
        out('[X] 驱动加载失败, 无法访问 EC')
        return 1
    cfg = load_cfg()
    a = (args[0].lower() if args else 'status')
    f = cfg.setdefault('fan', {})
    mapped = {'curve': 'curve', 'max': 'max', 'auto': 'off', 'ec': 'off', 'off': 'off'}
    if a in ('status', 'st', 's'):
        cur = ec_hfsp()
        r = ec_rpm()
        t = ec_temp()
        g = guard_cfg(cfg)
        fl = ec_trip_flags()
        out('风扇档位 HFSP : %s' % fan_name(cur))
        out('风扇转速      : %s' % (('%d RPM' % r) if r is not None else '读取失败'))
        out('EC 温度       : %s' % (('%d °C' % t) if t is not None else '读取失败'))
        out('固件过热标志  : %s' % (flags_desc(fl) +
                                   ('   [!] 已置位(固件正在报 _HOT)' if ec_tripped(fl) else '   (正常)')
                                   if fl else '读取失败'))
        out('风扇模式      : %s   (config.json -> fan.mode)' % f.get('mode'))
        out('过热保护      : %s' % ('已开启 (预警 %.0f°C/严重 %.0f°C/逃生 %.0f°C)'
                                   % (g['warn_c'], g['crit_c'], g['escape_c'])
                                   if g.get('enabled') else '已关闭'))
        return 0
    if a == 'level':
        n = int(args[1]) if len(args) > 1 else 0
        if not (1 <= n <= 7 or n >= FAN_MAX):
            out('[X] 档位只能是 1~7, 或 64(满速)')
            return 2
        f['mode'] = 'level'
        f['level'] = n
        save_cfg(cfg)
        got = set_fan(n)
        out('[√] 风扇固定到 %s (config: fan.mode=level, fan.level=%d)' % (fan_name(got), n))
        out('    守护服务会一直维持这个档位; 想恢复自动曲线执行 fan curve')
        return 0
    if a in mapped:
        f['mode'] = mapped[a]
        save_cfg(cfg)
        cur = ec_hfsp()
        if a == 'max':
            set_fan(FAN_MAX)
        elif a == 'curve' and ec_temp() is not None:
            fan_tick(cfg, ec_temp())          # 立即按曲线给一个档, 不用等下一轮
        tips = {'curve': '按温度自动(见 fan.curve)', 'max': '固定满速',
                'auto': '交还 EC 原生自动(本机原生曲线偏平, 到不了满速)'}
        out('[√] 风扇模式 -> %s (%s)' % (mapped[a], tips.get(a, '')))
        rpm = ec_rpm()                      # 只读一次(原来这里 ec_rpm() 被调了两遍)
        out('    现在: %s, %s' % (fan_name(ec_hfsp()),
                                  ('%d RPM' % rpm) if rpm is not None else ''))
        return 0
    out('用法: CpuPLFix.py fan [状态|curve|max|auto|level N]')
    return 2


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else 'check'
    if cmd in ('-h', '--help', 'help', '?'):
        print(__doc__)
        print('用法: CpuPLFix.py [check|fix|save|install|uninstall|watch|boot|fan]')
        print('  风扇: CpuPLFix.py fan [状态|curve|max|auto|level N]')
        return 0

    if cmd == 'fan':
        try:
            rc = cmd_fan(sys.argv[2:])
        except Exception as e:
            import traceback
            out('[X] 异常: %s' % e)
            out(traceback.format_exc())
            rc = 1
        flush_log('fan')
        return rc or 0

    fn = {'check': cmd_check, 'fix': cmd_fix, 'save': cmd_save, 'boot': cmd_boot,
          'install': cmd_install, 'uninstall': cmd_uninstall, 'watch': cmd_watch}.get(cmd)
    if not fn:
        print('未知命令: %s' % cmd)
        return 2
    try:
        rc = fn()
    except Exception as e:
        import traceback
        out('[X] 异常: %s' % e)
        out(traceback.format_exc())
        rc = 1
    flush_log(cmd)
    return rc or 0


if __name__ == '__main__':
    sys.exit(main())
