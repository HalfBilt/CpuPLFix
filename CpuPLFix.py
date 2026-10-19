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
import threading
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
    # ★凉机(EC < flag_hot_c)且没在保护时, 标志读降到这个间隔 —— 少打扰 EC(EC 端口与系统
    #   ACPI EC 驱动/固件共用, 读得越勤越容易互相插队, 见 03 §4)
    "flag_poll_slow_s": 120.0,
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
        "dts_max_c": 92.0,          # ★DTS(核心温度, 5 秒粒度)到它立刻拉满 —— 比 EC 0x78 快得多
        "load_boost_pct": 70.0,     # 占用 >= 它算"高负载"
        "load_boost_s": 10.0,       # 连续高负载满这么久才拉(躲开短时突发; 5 秒粒度计时)
        "load_hold_s": 30.0,        # 负载落下后再多吹这么久(温度还没下来)
        "load_boost_level": 64,     # 拉到的档位(64=满速); 0 = 关掉前馈
        # ★非紧急写入之间的最小间隔(秒, 2026-10-19 加): 防"高负载时每几秒改一次风扇档"
        #   (那天 16:32~16:34 就是 3~11 秒一次, 之后机器直接断电)。保护中/温度到 warn_c 不节流。
        "min_write_gap_s": 10.0
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
        # ★2026-10-19 按 i7-1360P 规格重设(原来是一步砍到 10W):
        #   预警只收 PL2(保突发); 严重分三级 → PBP 28W → Minimum Assured Power 20W → 地板 12W
        "warn_pl1_w": 0.0,          # ★0 = 预警不动 PL1(推荐); 填了就按它压
        "warn_pl2_w": 40.0,         # ★预警只把 PL2 收到 40W
        "crit_pl1_w": 28.0,         # ★第一级 = PBP(Processor Base Power)
        "crit_pl2_w": 35.0,
        "crit2_pl1_w": 20.0,        # ★第二级 = Minimum Assured Power
        "crit2_pl2_w": 25.0,
        "crit3_pl1_w": 12.0,        # ★地板(仅 100°C 或持续压不下来时)
        "crit3_pl2_w": 14.0,
        "crit_hold_s": 10.0,        # ★驻留: 持续 >=crit_c 这么久才进"严重"
        "crit_now_c": 100.0,        # ★单次到它(=TjMax)立即进"严重"
        "step_dwell_s": 15.0,       # ★每级停留这么久还压不下来就再深一级
        "ramp_step_s": 30.0,        # ★解除后逐级回升, 每级这么多秒
        "ramp_pls": [[20.0, 25.0], [28.0, 35.0]],   # ★回升阶梯(末尾自动接回原档)
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
        # ★2026-10-20: 温度→功耗调节器("风扇优先"模型)。默认关。
        #   开 = 日常功耗跟着温度走(顶档受 pl1_w 约束), 保护阶梯只在 ≥98°C 接手。
        #   详见手册 02 §4.5; 3-config.bat tpower=on/off
        "temp_power": False,
        # ★2026-10-19(用户指出后加): 标志档的"温度正常持续一段时间就自动解除"出口。
        #   标志挂着 + 温度连续 <= flag_release_c 达 flag_release_s 秒 -> 判"固件锁存位,
        #   不是真过热" -> 分级回升放开, 并保持放开(标志清零前不再据此降档)。
        #   默认 flag_release_c = 预警线(95°C): 低于预警线就算"温度正常"。
        #   flag_release_s = 0 表示关掉这条出口(退回"标志不清零就一直压着"的老行为)。
        "flag_release_c": 95.0,
        "flag_release_s": 60.0,
        "min_hold_s": 30.0,
        "release_dwell_s": 10.0,
        # ★保护退出后继续拉满风扇这么久(秒, 2026-10-19 加): "10W -> 40W" 是个功率阶跃,
        #   退出瞬间就把风扇收回去会让温度二次冲顶(固件热事件就是这么被点着的)。
        "cool_tail_s": 30.0,
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
            # ★2026-10-19: 60 秒改成 20 秒 —— 11:29:42 那次固件热事件强制休眠只睡了
            #   45 秒, 被这里判成"定时复查", 于是 cmd_boot 里查 Event 88 的那段没跑,
            #   日志里少了一句"上次是固件热事件强制休眠"。
            if slept > 20:                      # 真睡过
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
def _tau_s(t):
    """MSR 0x610 时间窗字段 -> 秒(Intel SDM: 2^Y * (1 + Z/4))。

    ★2026-10-20 修正: 参数是 `(v >> 16) & 0xFF` 那个字节, 而 SDM 里
      Y = MSR bits 16:17 (2 位), Z = MSR bits 22:23 (2 位)
      -> 落到这个字节里: Y = bits 1:0, Z = bits 7:6。
      老实现写的是 "Y=低5位, Z=次2位"(ThrottleStop 那种 7 位编码), 于是
      pl1_time=35 被算成 2^3*(1+1/4)=10 秒、pl2_time=66 算成 6 秒 —— 与
      `_pl_time()`(按 SDM 算: 8 秒 / 5 秒) 自相矛盾, `2-check` 和 `power`
      两个命令会报出不同的窗口。实测 MSR 0x610=0x004281B800238140:
        PL1 bits16:17=0b11=3, bits22:23=0b00=0 -> 2^3*1.00 = 8 秒
        PL2 bits48:49=0b10=2, bits54:55=0b01=1 -> 2^2*1.25 = 5 秒
    """
    try:
        t = int(t) & 0xFF
    except Exception:
        return '?'
    y, z = t & 0x3, (t >> 6) & 0x3
    s = (2.0 ** y) * (1.0 + z / 4.0)
    return ('%.3f ms' % (s * 1000)) if s < 0.01 else ('%.0f 秒' % s if s < 60 else '%.0f 分' % (s / 60))


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
# ★2026-10-19: 老机型兜底开关。本机**关**(见 ec_rpm() 里的说明) —— 手册 03 §5.1/§5.4
#   写着"工具不写 0x31", 代码以前却在 ec_rpm() 的兜底路径里写它, 自相矛盾。
EC_LEGACY_FSEL = False
EC_TEMP_EXTRA = (0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0xC9)   # TTS1/TTS6/TTS2/TTS3/TTS4/HWAT
FAN_AUTO, FAN_MAX = 0x80, 0x40
# ★2026-10-19: 6 档改成实测 3880(原来 3490 是从别的项目抄的 —— 同一行日志会印成
#   "6 档(约 3490 RPM) 3875RPM", 自己跟自己差 400 转)。1~5/7 档本机还没有可信标定, 先留旧值。
FAN_LEVEL_RPM = {1: 2480, 2: 2610, 3: 2720, 4: 3190, 5: 3185, 6: 3880, 7: 3880, 64: 5000}

# 固件"热到要强制休眠"的真实判据(来自本机 DSDT 里 \_TZ.THM0._TMP 的反汇编):
#   这三个寄存器任一的低 7 位非零 -> _TMP 立刻返回 _HOT(=TCRT-1=401K≈127.9°C),
#   于是固件/系统判定热区到顶, 强制休眠(S4)。
#   注意: 它跟温度寄存器 0x78 的读数无关 —— 0x78 只是给 OS 看的温度(而且 0x80 是
#   "无效"哨兵值, AML 会把它当成 48°C)。2026-10-18 那次强制休眠就是这几个标志触发的。
#   ⚠️ 只有这三个! 0x88(TSL0/TSR0) **不**参与 _TMP 的判据(它只被 _WAK/_Q40 用来挑热表),
#      2026-10-18 实测本机 0x88 是 0x40(bit6), 但 2026-10-19 断电复位后变成 0x00 ——
#      它**会变**, 属正常, 别当成"过热"。
EC_TRIP_FLAGS = (0x89, 0x8A, 0x8B)
EC_TEMP_SENTINEL = 0x80        # 0x78 读到这个值 = 传感器给了无效码
# ★2026-10-19: ec_temp() 把 0x80 当"读失败"滤掉了(out of range), 于是 tick() 里那个
#   "读到无效码"的提示**永远不会触发**(死代码)。这里留个标志位把它接回去。
_EC_TEMP_SENTINEL_SEEN = False


def _port_call(code, inbuf, outlen):
    """端口 IOCTL。失败时句柄多半已失效(睡眠后驱动重载等) —— 重开一次再试。
    (CpuPLFix.ioctl 只要失败就会把全局 _handle 清空, 不重开就再也发不出去了)"""
    ok, out = ioctl(code, inbuf, outlen)
    if not ok:
        ensure_driver(verbose=False)
        ok, out = ioctl(code, inbuf, outlen)
    return ok, out


EC_FAIL_BRAKE_S = 0.005       # ★端口调用失败后的刹车(秒): 见 rd_port/wr_port 注释


def rd_port(port):
    ok, b = _port_call(IOCTL_READ_PORT, struct.pack('<H', port), 1)
    if not ok:
        # ★2026-10-19 11:13 实测: 端口/驱动一有问题, 上层重试会在毫秒级连打上千次端口调用
        #   (日志里出现过 "122 次判定 / 0.2 秒"). 失败后踩一脚刹车, 把最坏情况压到 ~200 次/秒。
        time.sleep(EC_FAIL_BRAKE_S)
        return None
    return b[0]


def wr_port(port, val):
    ok, _unused = _port_call(IOCTL_WRITE_PORT, struct.pack('<HHB', port, 0, val & 0xFF), 0)
    if not ok:
        time.sleep(EC_FAIL_BRAKE_S)
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
# ★2026-10-19: 再拿一把"行业通用"的 EC 锁。HWiNFO / AIDA64 / LibreHardwareMonitor 都用
#   'Global\\Access_EC' 这个名字串行化 EC 端口访问 —— 它们**不认识**我们自己那把
#   'Global\\CpuPLFix-EC'。装了 HWiNFO 并常驻时, 它每秒一次的 EC 轮询会直接插进我们的
#   事务窗口里(本机实测: 串位就是"别的主控插进来做完它那一笔, 把输出缓冲覆盖掉")。
#   尽力而为: 拿不到就照旧干活, 只是退回"和对家赛跑"(原来的行为)。
EC_MUTEX_PEER = 'Global\\Access_EC'
_ec_mx = None
_ec_mx_peer = None
_ec_peer_held = False


_MX_SDDL = 'D:(A;;0x00100001;;;WD)'      # Everyone: SYNCHRONIZE(0x100000)|MUTEX_MODIFY_STATE(0x1)
_DACL_SECURITY_INFORMATION = 0x00000004

# ★★2026-10-19 复核(重要): 命名互斥体默认继承**创建者**的默认 DACL。
#   本机看守服务以 SYSTEM 身份创建这两个名字 -> 普通用户进程 CreateMutexW / OpenMutexW
#   一律 ERROR_ACCESS_DENIED(实测: SYNCHRONIZE / MUTEX_MODIFY_STATE / 两者 / ALL_ACCESS
#   四种组合全是 err=5; 对照组: 用户进程自己建的名字, 另一个用户进程立刻就能打开)。
#   后果有两层:
#     ① 2-check / fan 命令 / 13-EC诊断 / 15-EC延时对照 这些**用户级**工具一把锁都拿不到
#        -> _ec_lock() 直接返回 False -> 它们**完全不加锁**地和看守服务抢同一对端口,
#        正是 03 §4 说的插队场景(手册里"跨进程互斥"这条保险对本机其实是失效的);
#     ② 我们抢先创建的"行业通用那把"(Access_EC)还会让 HWiNFO 之类用户级程序**开不了它**
#        -> 同行锁不但没保护, 反而可能打断别人自己的锁。
#   对策: 显式给一把 "Everyone: SYNCHRONIZE|MUTEX_MODIFY_STATE" 的 DACL;
#   已经存在的那两个是本服务建的(= 本服务是所有者, 有权改 DACL)顺手改回来。
try:
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_long
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.c_void_p]
    adv.SetKernelObjectSecurity.restype = ctypes.c_long
    adv.SetKernelObjectSecurity.argtypes = [w.HANDLE, ctypes.c_uint32, ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    k32.LocalFree.argtypes = [ctypes.c_void_p]
except Exception:
    pass


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('nLength', ctypes.c_uint32), ('lpSecurityDescriptor', ctypes.c_void_p),
                ('bInheritHandle', w.BOOL)]


def _mx_sddl_sd():
    """把 _MX_SDDL 变成自相对安全描述符(调用方负责 LocalFree)。失败返回 None。"""
    try:
        sd = ctypes.c_void_p()
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                _MX_SDDL, 1, ctypes.byref(sd), None, None):
            return None
        return sd
    except Exception:
        return None


def _mx_fix_dacl(h):
    """把已存在的互斥体 DACL 改成"普通用户也能同步+改状态"。失败不致命。

    用 SetKernelObjectSecurity(内核对象 + **自相对**安全描述符)。
    注意: SetSecurityInfo 那条路要的是绝对格式的 DACL, 喂自相对 SD 会返回 1336。
    权限: 句柄要有 WRITE_DAC —— 创建者(=所有者)隐式就有, 所以本服务改自己建的那两个没问题。
    """
    sd = _mx_sddl_sd()
    if sd is None:
        return False
    try:
        return bool(adv.SetKernelObjectSecurity(h, _DACL_SECURITY_INFORMATION, sd))
    except Exception:
        return False
    finally:
        try:
            k32.LocalFree(sd)
        except Exception:
            pass


def _mx_handle(slot, name):
    """取(必要时创建)命名互斥体句柄, 缓存到模块全局; 失败返回 False。

    2026-10-19: 显式带一份"Everyone 可同步+改状态"的 DACL 去创建, 并把已存在的那个
    的 DACL 修回来 —— 否则服务(SYSTEM)先创建的话, 用户级进程一把锁都拿不到。
    """
    g = globals()
    h = g.get(slot)
    if h is None:
        sd = _mx_sddl_sd()
        try:
            sa = None
            if sd is not None:
                sa = _SECURITY_ATTRIBUTES()
                sa.nLength = ctypes.sizeof(sa)
                sa.lpSecurityDescriptor = sd
                sa.bInheritHandle = False
            try:
                k32.CreateMutexW.restype = w.HANDLE
                k32.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
                h = k32.CreateMutexW(ctypes.byref(sa) if sa is not None else None, False, name)
            except Exception:
                h = None
            if not h:
                try:
                    k32.OpenMutexW.restype = w.HANDLE
                    k32.OpenMutexW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
                    h = k32.OpenMutexW(0x00100000 | 0x0001, False, name)  # MUTEX_MODIFY_STATE|SYNCHRONIZE
                except Exception:
                    h = None
            h = h or False
        except Exception:
            h = False
        finally:
            if sd is not None:
                try:
                    k32.LocalFree(sd)
                except Exception:
                    pass
        if h:
            _mx_fix_dacl(h)       # 已存在的那个可能带着旧 DACL -> 改回来
        g[slot] = h
    return h


def _ec_lock(timeout_ms=3000):
    """把 EC 端口事务跨进程串行化(看守服务 / 命令行 / 扫描工具都用这一把 + 对家那把)。

    实测教训: 两个进程同时做那套"写命令 0x80 -> 写偏移 -> 读数据"的四步事务时, 双方会
    互相插队, 应答错位, 读出来的是对方那次事务的字节(0x78 读成 46 而实际 82)。
    用命名互斥体(Global\\ 才能跨会话 —— 服务在 session 0, 命令行在用户会话)。
    拿不到就退化成不加锁(尽力而为), 锁本身不会让功能失效。
    """
    global _ec_peer_held
    _ec_peer_held = False
    h = _mx_handle('_ec_mx', EC_MUTEX_NAME)
    if not h:
        return False
    try:
        r = k32.WaitForSingleObject(h, timeout_ms)
        if r not in (0x00000000, 0x00000080):     # WAIT_OBJECT_0 / WAIT_ABANDONED(上一个持有者死了)
            return False
    except Exception:
        return False
    # 再拿对家那把(短超时, 拿不到就算了 —— 不能为了它把整次读卡住)
    hp = _mx_handle('_ec_mx_peer', EC_MUTEX_PEER)
    if hp:
        try:
            if k32.WaitForSingleObject(hp, 250) in (0x00000000, 0x00000080):
                _ec_peer_held = True
        except Exception:
            pass
    return True


def _ec_unlock(locked):
    global _ec_peer_held
    if locked and _ec_peer_held and _ec_mx_peer:
        try:
            k32.ReleaseMutex(_ec_mx_peer)
        except Exception:
            pass
        _ec_peer_held = False
    if locked and _ec_mx:
        try:
            k32.ReleaseMutex(_ec_mx)
        except Exception:
            pass


def _ec_fail():
    """一笔 EC 读事务失败(读不出/命令写失败)。记一次"读失败", 返回 None。"""
    _ECBUS['hard'] = _ECBUS.get('hard', 0) + 1
    return None


def ec_read(off, _locked=False, delay=0.003):
    """读一个 EC 寄存器(失败返回 None)。delay = 写完偏移后等 EC 应答的时间(秒)"""
    locked = _ec_lock() if not _locked else True
    # ★2026-10-19: 事务计数。体检行以前拿"读判定次数"当分母, 而读判定只有严格读
    #   (固件标志)才会计 —— 温度/档位那些普通读一次都不计, 于是那个"串位率"既不是
    #   事务级、也没覆盖真正喂风扇曲线的那几笔。现在按**事务**计数, 分母才稳。
    _ECBUS['tx'] = _ECBUS.get('tx', 0) + 1
    try:
        _ec_drain()
        if not _ec_wait(True):
            return _ec_fail()
        if not wr_port(EC_CTRL, 0x80):      # ★2026-10-19: 原来丢掉返回值 —— 写失败也照样往下走
            ec_bus_stat(True)
            return _ec_fail()
        if not _ec_wait(False):
            return _ec_fail()
        if not wr_port(EC_DATA, off):
            ec_bus_stat(True)
            return _ec_fail()
        if not _ec_wait(False):
            return _ec_fail()
        # 等 EC 把结果放进输出缓冲。EC 忙(满载时它自己在管风扇/热区)时不会立刻应答,
        # 不等就直接读, 读到的往往是别的主控留在缓冲里的残留字节 —— 实测满载扫描时
        # 一次"读 0x8A"读到的是 0x7A 的值(46), 而且连读两次都是这个残留值。
        # 等待时间取"调用方给的"与"自适应值"的较大者。★注意等待**不是越久越好**: 等得越久,
        # 别的主控越有时间把输出缓冲覆盖掉(实测 1ms->1.4%, 20ms->32.6%), 所以基准 3ms、上限 4ms。
        time.sleep(max(delay, ec_bus_settle()))
        v = rd_port(EC_DATA)
        if v is None:
            ec_bus_stat(True)
            return _ec_fail()
        return v
    finally:
        if not _locked:
            _ec_unlock(locked)


def ec_read_ok(off, tries=3):
    """带一致性校验的读: 连续两次读到同一个值才算数, 否则重试; 都不一致就返回 None。

    两道保险一起用:
      1) 读完等一小会儿就读(基准 3ms, 上限 4ms) —— 防"抢在 EC 应答之前读"拿到残留字节。
         ★2026-10-19 实测: 等待越久越会被别的主控插队(1ms->1.4%, 20ms->32.6%), 所以
         这里**不再**"失败就加大等待" —— 旧版重试到 8/20ms 是在帮倒忙。
      2) 连读两次比对 —— 防别的主控的事务插在中间时读到它的应答。
    校验不过就宁可判成"这次没读到"(沿用上一次的判断), 也不拿可疑值去改保护等级。
    """
    prev = None
    delays = EC_RETRY_DELAYS
    for i in range(max(2, tries)):
        v = ec_read(off, delay=delays[min(i, len(delays) - 1)])
        if v is not None and prev is not None and v == prev:
            return v
        if v is not None and prev is not None:
            # ★2026-10-19: 两次读出的值不同 = 这两笔里有一笔被插队了(温度/转速是慢变量,
            #   5 ms 内不可能变)。以前这里只是默默继续重试, 于是"普通读"的撞车**完全没进账**
            #   —— 体检行的"串位率"只统计得到"读失败"和"哨兵不符", 少算一大半。
            _ECBUS['soft'] = _ECBUS.get('soft', 0) + 1
        prev = v
        if i + 1 < max(2, tries):
            time.sleep(0.005)       # ★重试之间也给 EC 喘口气(2026-10-19)
    ec_bus_stat(True)       # 读到不一致/读不出: 记一次总线异常(自适应等待会加大)
    return None


# ---------------------------------------------------------------- EC 总线加固
# EC 与系统 ACPI EC 驱动 / 固件 SMI 共用 0x62/0x66。我们自己的事务没问题, 问题出在
# **写完偏移之后那段等待里, 别的主控插进来做完了它自己的事务, 把输出缓冲覆盖成它的字节**
# —— 它读的是热区温度, 所以我们最后读到的是"别的温度寄存器的值"。
#   实测(2026-10-19 18:35, 受控对照: settle 钉在 1ms, 50 个寄存器 x3 轮, 只变等待时间):
#     写完偏移后等  1ms -> 同寄存器连读两次不一致  1.4%, 哨兵挡住 97.2%
#                   3ms ->  2.8% / 92.4%
#                   6ms -> 10.4% / 88.2%
#                  12ms -> 22.9% / 77.8%
#                  20ms -> 32.6% / 66.7%
#   即 **等得越久越容易被插队**, 超过 6ms 全是负收益。错值还高度集中(55 次错值里 46 次
#   恰好等于 0x7A 那一刻的读数), 直接证明"拿到的是别的主控留下的字节", 不是随机损坏。
#   当日服务日志也吻合: 等待 3~6ms 的窗口报 0.8~5.0%, 被顶到 20ms 之后报 40~62%。
#   ★教训: 旧版"检出串位就把等待翻倍(上限 20ms)"是在主动扩大对手的抢跑窗口 ——
#     偶发的一次失败会把等待顶到上限, 而"连续 20 次干净才回落"在 50% 失败率下永远
#     凑不满, 于是永久钉死在最差档位, 把 1.6% 放大成 60%。
# 三道对策(成本极低, 只在关键读上多花 1~2 次事务):
#   1) 哨兵: 0xC9(HWAT) 本机实测恒 65(见 05 §2), 断电复位也不变 —— 关键读的前后各读一次,
#      对不上就说明这次事务被插队了, 整批判废重来(被插队时"连读两次一致"也会一致地错);
#   2) 等待: 基准 3ms / 上限 4ms / 失败 3 次干净就回落 / 静默 60 秒直接回基准
#      (★不再往上爬到 20ms —— 见上面实测);
#   3) 计数: 串位/事务数攒起来, 每 30 次读判定报一行, 回头看总线到底有多糟。
EC_CANARY = (0xC9, 65)          # (偏移, 期望值): HWAT, 本机常量
EC_BASE_DELAY = 0.003           # 基准等待: 写完偏移后 3ms 再读(实测最干净的一段)
EC_MAX_DELAY = 0.004            # 上限等待: 实测 6ms 起急剧恶化, 所以基本不爬
EC_RETRY_DELAYS = (EC_BASE_DELAY, EC_MAX_DELAY, EC_MAX_DELAY)
EC_DECAY_S = 60.0               # 距上次串位超过这么久 -> 等待直接回基准
_ECBUS = {'ok': 0, 'bad': 0, 'retry': 0, 'settle': EC_BASE_DELAY, 'clean': 0, 'pending': False,
          't0': None, 'win_s': 0.0, 'last_report': 0.0, 'last_bad': 0.0,
          # ★2026-10-19: 事务级计数(tx / hard / soft / canary)—— 体检行的分母改成"笔事务",
          #   这样不同判定类型(普通读 2~3 笔 vs 严格读 6~9 笔)混在一起也不会让百分比失真。
          'tx': 0, 'hard': 0, 'soft': 0, 'canary': 0}
ECBUS_REPORT_EVERY = 30         # 30 次读判定一行(约 5 分钟) —— 便于盯"修好了没有"


def ec_bus_settle():
    """当前"写完偏移后等 EC 应答"的等待(秒) —— 检出串位会自动加大。"""
    return _ECBUS['settle']


def ec_bus_stat(hit_bad, retry=False):
    """记一次事务结果 + 自适应等待; 攒够了置 pending(由 ec_bus_report 取走)。

    hit_bad=True  = 哨兵不符/读不出 —— 确定的总线问题, 把等待往上顶一格(上限 EC_MAX_DELAY);
    retry=True    = 哨兵对但两次读出的值不同 —— 温度/转速这类**本来就该变**的寄存器
                    也会这样, 只计数、不动等待(2026-10-19 修正: 早先把两者混在一起,
                    报出过 24.4% 的虚高串位率)。
    """
    if _ECBUS.get('t0') is None:
        _ECBUS['t0'] = time.time()          # 窗口起点: 不初始化会算出"120 次/1 秒"这种假速率
    # ★2026-10-19: 时间衰减 —— 距上次串位超过 EC_DECAY_S 秒就无条件回到基准等待。
    #   只靠"连续 N 次干净"这一个回落条件不够: 失败率 50% 时它永远凑不满。
    _lb = _ECBUS.get('last_bad') or 0.0
    if _ECBUS['settle'] > EC_BASE_DELAY and _lb and (time.time() - _lb) > EC_DECAY_S:
        _ECBUS['settle'] = EC_BASE_DELAY
        _ECBUS['clean'] = 0
    if retry and not hit_bad:
        _ECBUS['retry'] += 1
        _ECBUS['ok'] += 1
        _ECBUS['clean'] += 1
        return
    if hit_bad:
        _ECBUS['bad'] += 1
        _ECBUS['clean'] = 0
        _ECBUS['last_bad'] = time.time()
        # ★2026-10-19: 上限从 20ms 降到 4ms。旧版"检出串位就把等待翻倍"实测是在帮倒忙
        #   —— 等待越长, 别的主控越有时间把输出缓冲覆盖掉(1ms->1.4%, 20ms->32.6%)。
        _ECBUS['settle'] = min(EC_MAX_DELAY, _ECBUS['settle'] * 2)
    else:
        _ECBUS['ok'] += 1
        _ECBUS['clean'] += 1
        # ★2026-10-19: 回落条件从"连续 20 次干净"放宽到 3 次 —— 20 次在 50% 失败率下
        #   永远凑不满, 会让等待永久钉死在上限, 把偶发失败放大成长期 40~60% 的串位率。
        if _ECBUS['clean'] >= 3 and _ECBUS['settle'] > EC_BASE_DELAY:
            _ECBUS['settle'] = max(EC_BASE_DELAY, _ECBUS['settle'] / 2.0)
    # ★2026-10-19: 必须加 not pending —— 否则阈值一过, 后面每来一次事务都会把 t0 推到
    #   "现在", win_s 永远退化成 max(0.2, ...) 那个地板, 日志里就成了"122 次 / 0.2 秒"。
    if (not _ECBUS['pending']
            and _ECBUS['ok'] + _ECBUS['bad'] >= ECBUS_REPORT_EVERY):
        _ECBUS['pending'] = True
        _ECBUS['win_s'] = max(0.2, time.time() - (_ECBUS.get('t0') or time.time()))
        _ECBUS['t0'] = time.time()


def ec_bus_stats():
    """(事务数, 撞车数, 等待毫秒, 读失败数) —— 给 2-check 显示。"""
    hard = _ECBUS.get('hard') or 0
    soft = _ECBUS.get('soft') or 0
    can = _ECBUS.get('canary') or 0
    return (_ECBUS.get('tx') or 0, hard + soft + can, _ECBUS['settle'] * 1000.0, hard)


def ec_bus_report():
    """攒够判定数就返回一行(最多 60 秒一行); 否则 None(并把窗口清零)。

    说人话: 这里数的是"一次读判定"(一次判定=1~9 次端口事务), 窗口很短时行里的
    "次/分钟"会很大 —— 那是失败快速重试的风暴(已加刹车), 不是真的在刷 EC。
    ★2026-10-19 起即使 0 串位也会出这一行: "0 串位"本身就是"修好了"的证据, 得看得见。
    ★2026-10-19: 分母改成**笔事务**(tx), 撞车率 = (读失败+值变动+哨兵不符)/事务数。
      以前那个"读判定次数"当分母有两处毛病: ① 判定里混着 2~3 笔的普通读和 6~9 笔的
      严格读, 比例一变百分比就变; ② 普通读(温度/档位)根本不计数, 真正喂风扇曲线
      的那几笔撞车全漏了。现在按事务算, 跨时段/跨模式都能比。
    """
    if not _ECBUS['pending']:
        return None
    n_ok, n_bad = _ECBUS['ok'], _ECBUS['bad']
    n_tx = _ECBUS.get('tx') or 0
    n_hard = _ECBUS.get('hard') or 0
    n_soft = _ECBUS.get('soft') or 0
    n_can = _ECBUS.get('canary') or 0
    win = float(_ECBUS.get('win_s') or 0.0)
    _ECBUS['ok'] = _ECBUS['bad'] = _ECBUS['retry'] = 0
    _ECBUS['tx'] = _ECBUS['hard'] = _ECBUS['soft'] = _ECBUS['canary'] = 0
    _ECBUS['pending'] = False
    if (time.time() - (_ECBUS.get('last_report') or 0.0)) < 60.0:
        return None                       # 限频: 风暴时别刷屏
    _ECBUS['last_report'] = time.time()
    hits = n_hard + n_soft + n_can
    # ★2026-10-19 起即使 0 串位也会出这一行: "0 串位"本身就是"修好了"的证据, 得看得见。
    return ('EC 总线体检: 最近 %d 笔事务 / %.1f 秒 —— 撞车 %d 次(%.1f%%) '
            '[读失败 %d / 值变动 %d / 哨兵不符 %d], 等待 %.1f ms —— '
            'EC 与系统 ACPI EC 驱动/固件共用 0x62/0x66, 等待越长越容易被插队'
            '(见 03 §4); 关键读已用哨兵挡住'
            % (n_tx, win, hits, 100.0 * hits / max(1, n_tx),
               n_hard, n_soft, n_can, _ECBUS['settle'] * 1000.0))


def ec_read_strict(off, tries=3):
    """关键读(会触发动作的): 哨兵 + 两次一致, 任一不过整批判废重试, 都不成就返回 None。

    比 ec_read_ok 多两道: 每次读的前后夹一次哨兵 0xC9=65; 判废时把等待加大再来一遍。
    给"固件过热标志 0x89/0x8A/0x8B"这种一置位就要动手的寄存器用 —— 误读的代价是白压功耗。
    """
    prev = None
    delays = EC_RETRY_DELAYS
    for i in range(max(2, tries)):
        d = max(delays[min(i, len(delays) - 1)], ec_bus_settle())
        c1 = ec_read(EC_CANARY[0], delay=d)
        v = ec_read(off, delay=d)
        c2 = ec_read(EC_CANARY[0], delay=d)
        good = (c1 == EC_CANARY[1] and c2 == EC_CANARY[1] and v is not None)
        if not good:
            _ECBUS['canary'] = _ECBUS.get('canary', 0) + 1
            ec_bus_stat(True)
            time.sleep(0.010)
            prev = None
            continue
        if prev is not None and v == prev:
            ec_bus_stat(False)
            return v
        # 哨兵对、但两次读出来的值不一样: 对动态寄存器(温度/转速)属正常, 只记一次"重试";
        # 对关键读(会用这个值去动手的)仍然要求两次一致才返回 —— 所以这里照样再来一遍。
        if prev is not None:
            _ECBUS['soft'] = _ECBUS.get('soft', 0) + 1
        ec_bus_stat(False, retry=(prev is not None))
        prev = v
    return None


def ec_write(off, val, _locked=False):
    """写一个 EC 寄存器"""
    locked = _ec_lock() if not _locked else True
    try:
        _ec_drain()
        if not _ec_wait(True):
            return False
        if not wr_port(EC_CTRL, 0x81):       # ★2026-10-19: 原来丢掉返回值, 写失败也返回 True
            return False
        if not _ec_wait(False):
            return False
        if not wr_port(EC_DATA, off):
            return False
        if not _ec_wait(False):
            return False
        if not wr_port(EC_DATA, val):
            return False
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
    global _EC_TEMP_SENTINEL_SEEN
    _EC_TEMP_SENTINEL_SEEN = False
    v = ec_read_ok(EC_TEMP)
    if v == EC_TEMP_SENTINEL:
        _EC_TEMP_SENTINEL_SEEN = True
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
            # ★2026-10-19: 读失败**不能**把这个 key 删掉 —— 删掉之后上层 ec_tripped({})
            #   得到的是"没有标志", 于是 flag 档被放开、下一轮又压回去(实测每 50~90 秒
            #   一个来回)。这里保留 key、值为 None, 由 merge_flags() 决定沿用上一轮的值。
            d[h] = None
            continue
        if tmp is not None and v == tmp:
            ec_bus_stat(True)
            d[h] = None          # 值恰好等于温度字节 -> 判读错, 同样按"没读到"处理
            continue
        d[h] = v
    return d


def ec_tripped(flags):
    """有标志置位(低 7 位非零)就返回 [(offset, value), ...]"""
    return sorted((h, v) for h, v in (flags or {}).items()
                  if v is not None and (v & 0x7F))


def merge_flags(new, old):
    """把这一轮的标志读数与上一轮合并 -> (merged, complete)。

    ★2026-10-19: 读失败(None) **不等于** "标志没了"。读不到时沿用上一轮的值，并告诉
    调用方"这一轮不算新证据"(complete=False -> flags_fresh=False)，这样 trip_hits /
    clr_hits 都不会被一次失败的读推动。原则：读不到就什么都不做，别当成清零。
    """
    new = new or {}
    old = old or {}
    merged, complete = {}, True
    for h in EC_TRIP_FLAGS:
        v = new.get(h)
        if v is None:
            complete = False
            v = old.get(h)
        merged[h] = v
    return merged, complete


def ec_rpm(tries=2):
    """风扇转速。先读 0x84 再读 0x85 —— 顺序不能反(thinkpad_acpi 记载的固件 bug)。

    ★2026-10-19: 原来这里两笔都是**裸读**(没有哨兵、没有一致性校验), 插队时会把别的
      寄存器的值当成转速 —— 日志里因此出现过 "2 档 4587 RPM"、"6 档 4491 RPM" 这种
      离群值(真值分别是 ~2850 / ~3880), 还误导过手册的风扇表。
      现在每一轮都是 哨兵 -> lo -> hi -> 哨兵, 前后两次 0xC9 都等于 65 才认;
      转速再限一个 0~8000 RPM 的物理范围。代价是多几次事务, 但 ec_rpm() 只在
      2-check / fan 命令里调, 不在服务的轮询热路径上。

    2026-10-18 实测: **不需要**先写 0x31。
      · 本机 DSDT 里 0x31 的位叫 HUWB(bit2)/VPON(bit6)/VRST(bit7), bit0/1 是保留位;
        "写 0x31=0 选主风扇" 是老 IBM / X60 那代(固件 TP-7M)的做法, 抄过来是错的。
      · 本机 0x31 实测 0x00 和 0x40(VPON 置位) 都出现过 —— 它会随状态变, 别拿它判断异常;
        但"写"和"不写"读到的转速完全一样(3888/3889 RPM)。
      · 原来每次读转速都多写一次 EC —— 那三个位语义没确认, 少碰为好。
    只有"两次读都失败"时才退回老做法试一次(兼容别的机型)。
    """
    locked = _ec_lock()
    try:
        need = max(1, int(tries))
        for i in range(need + 1):
            if i == need:
                # ★2026-10-19 复核: 老机型兜底(写 0x31 选主/副风扇)在本机**不该跑** ——
                #   手册 03 §5.1/§5.4、05 §3、06 §1 都写着"工具不写 0x31", 而本机 0x31
                #   平时是 0x40(VPON 置位), 写 0 会把 VPON 清掉。而"哨兵连续两轮都失败"
                #   在本机并不罕见(单笔污染 ~10% 时约 12% 的概率) —— 等于每十几次读转速
                #   就写一次 0x31。改成默认关; 要兼容别的机型再把 EC_LEGACY_FSEL 打开。
                if EC_LEGACY_FSEL:
                    ec_write(EC_FSEL_LEGACY, 0, _locked=True)
                ec_bus_stat(True)
            c1 = ec_read(EC_CANARY[0], _locked=True)
            lo = ec_read(EC_RPM_LO, _locked=True)
            hi = ec_read(EC_RPM_HI, _locked=True)
            c2 = ec_read(EC_CANARY[0], _locked=True)
            if c1 != EC_CANARY[1] or c2 != EC_CANARY[1] or lo is None or hi is None:
                ec_bus_stat(True)
                continue
            rpm = (hi << 8) | lo
            if 0 <= rpm <= 8000:
                ec_bus_stat(False)
                return rpm
            ec_bus_stat(True)              # 物理上不可能的值: 当这次没读到
        return None
    finally:
        _ec_unlock(locked)


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
    """写风扇档位。返回 (写入是否成功, 回读值)。

    ★2026-10-19: 以前这里返回的只是**回读值**, 而 ec_write 的返回值被丢掉 —— 于是
      "写成功、只是回读被串位搞成两次不一致"也会被报成
      "风扇: 写 0x06 失败(EC 无响应)"。日志里 10-19 的
      16:52:54 / 17:40:35 / 18:31:11 三次全是这种情况(写其实成功了)。
    """
    ok = ec_write(EC_HFSP, v)
    return ok, ec_hfsp()


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


def fan_tick(cfg, temp=None, force_max=False, load=None, force_why=None,
             boost=False, dts=None):
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
    dts_hot = False
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
        # ---- ★两条前馈(2026-10-19) ------------------------------------------
        # 只按 EC 温度升档会"慢半拍": 0x78 是另一路传感器、比核心真实温度滞后 —— 实测
        # 100% 负载跑起来时风扇还停在 6 档, 等 EC 报 98°C 才拉满, 接着温度砸下去、风扇停、
        # 再冲上来。两条前馈都不花 EC 事务:
        #   ① DTS 前馈: CPU 自己的 DTS(MSR, 5 秒一读、反应快)到 fan.dts_max_c 就立刻拉满;
        #   ② 负载前馈: 持续占用 >= load_boost_pct 满 load_boost_s 秒 -> 至少 load_boost_level
        #      (计时在 tick 里按 5 秒粒度做, 传进来的 boost 就是结论)。
        lvl = int(f.get('load_boost_level') or 0)
        dts_max = float(f.get('dts_max_c') or 92.0)
        hold_s = float(f.get('load_hold_s') or 30.0)
        # ★2026-10-19: DTS 前馈加**保持时间**(和负载前馈同一个 load_hold_s)。
        #   原来只要 DTS 一跌破阈值就立刻降回曲线档 —— DTS 读数本身有噪声(实测能抖好几度),
        #   于是风扇会在"满速"和"曲线档"之间来回切(手册 06 §3.1 那个抖动的成因之一)。
        #   现在: 到了阈值就置满速并保持 hold_s 秒, 期间不因为 DTS 回落而立刻降档。
        if dts is not None and dts >= dts_max:
            _FAN['dts_until'] = time.time() + hold_s
        if _FAN.get('dts_until') and time.time() < _FAN['dts_until']:
            dts_hot = True
        if dts_hot and want < FAN_MAX:
            want, boosted = FAN_MAX, True
        elif boost and lvl > 0 and lvl > want:
            want, boosted = lvl, True
    cur = ec_hfsp()
    if cur is None:
        return None
    if not _fan_ok(cur):
        ok, got = set_fan(want)             # 回读不可信, 目标档照样写下去
        _FAN['idx'] = idx
        if not ok:
            return '风扇: 写 0x%02X 失败(EC 无响应)' % want
        if got is None:
            return _note_once('fan_rb_%02x' % want,
                              '风扇: 已写 0x%02X, 但回读没确认(EC 事务串位) —— 下一轮会重试' % want)
        return _note_once('fan_junk',
                          '风扇: 档位回读异常(0x%02X, EC 事务串位) -> 已按目标重写 %s'
                          % (cur, fan_name(got)))
    if cur == want:
        _FAN['idx'] = idx
        _FAN['lv'] = cur
        return None
    # ---- ★写入节流(2026-10-19): 非紧急的曲线写入之间至少隔 fan.min_write_gap_s 秒 ----
    # 2026-10-18 16:32~16:34 的日志里出现过 3~11 秒一次的风扇档写入(6 次/2 分钟), 那天
    # 之后机器在那段高负载里直接断电了(事件 6008)。EC 是共用的慢总线, 能不写就不写:
    # 保护中(force_max)或温度已经到 warn_c 的写入照旧立即执行, 只节流"曲线慢慢调档"。
    gap = float(f.get('min_write_gap_s') or 0.0)
    cur_lv = _FAN.get('lv')
    urgent = force_max or (temp is not None and temp >= guard_cfg(cfg)['warn_c'])
    # 只压"往低档退", 升档(降温方向)永不延迟 —— 2026-10-19 两次压测后改: 前馈/曲线要升档时
    # 被节流掉就失去意义了。
    if (gap > 0 and not urgent and cur_lv is not None and want < cur_lv and _FAN.get('t')
            and (time.time() - _FAN['t']) < gap):
        _FAN['idx'] = idx
        return _note_once('fan_gap_%d_%d' % (cur_lv, want),
                          '风扇: 想在 %.0f 秒内连改档(%s -> %s), 已按最小间隔 %.0f 秒节流'
                          % (gap, fan_name(cur_lv), fan_name(want), gap))
    t0 = time.time()
    ok, got = set_fan(want)
    _FAN['idx'] = idx
    if not ok:
        return '风扇: 写 0x%02X 失败(EC 无响应)' % want
    if got is None:
        return _note_once('fan_rb_%02x' % want,
                          '风扇: 已写 0x%02X, 但回读没确认(EC 事务串位) —— 下一轮会重试' % want)
    if not _fan_ok(got):
        # ★2026-10-19: 回读值不是合法档位编码时不能当"已达成" —— 日志里出现过
        #   "风扇: 6 档(约 3490 RPM) -> 0x30 (84°C)"(0x30=48 不是档位), 那是串位读到的
        #   别的寄存器。以前这里直接写进日志、还覆盖了 _FAN['lv'] 和驻留计时。
        return _note_once('fan_rb_bad_%02x' % got,
                          '风扇: 已写 %s, 但回读值 0x%02X 不在合法档位里(EC 事务串位) —— 下一轮再看'
                          % (fan_name(want), got))
    if _FAN.get('lv') != got:
        _FAN['t'] = t0                      # 档位真的变了 -> 驻留计时从这里起算
    _FAN['lv'] = got
    if force_max and mode != 'max':
        why = ' (%s)' % (force_why or '过热保护')
    elif boosted and dts_hot:
        why = ' (DTS 前馈 %d°C)' % (dts if dts is not None else 0)
    elif boosted:
        why = (' (负载前馈 %.0f%%)' % load) if load is not None else ' (负载前馈)'
    else:
        why = ' (%d°C)' % temp if (idx is not None and temp is not None) else ''
    why = why if isinstance(why, str) else ''   # 保险: 上面分支写错也不会让日志炸掉
    if got != want and want != FAN_AUTO:
        # ★2026-10-19: 写进去的目标档和回读对不上 —— 回读是**合法档位**, 所以上面那道
        #   "不在合法档位里"的检查抓不到它。要么这一笔被串位了, 要么 EC 没接受这次写入。
        #   两种都得让人看见: 否则日志里会出现"6 档 -> 6 档"这种看不懂的行
        #   (实测 2026-10-19 20:40:04 就是这么来的 —— 下一轮重写就成功了)。
        #   (交还 EC 自动(0x80) 除外 —— 那时 0x2F 由 EC 自己写, 回读当然不等于 0x80。)
        return _note_once('fan_rb_mismatch_%02x_%02x' % (want, got),
                          '风扇: %s -> %s%s  [已写 0x%02X 但回读是 0x%02X, 不是目标 —— '
                          '下一轮会重写]' % (fan_name(cur), fan_name(got), why, want, got))
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
      'trip_since': None, 'trip_latched': False, 'cool_since': None, 'released_at': 0.0,
      # ★2026-10-19 温度墙分级所需
      'crit_since': None, 'step': 0, 'step_since': 0.0, 'why': '', 'ramp': None, 'ramp_at': 0.0,
      'clr_hits': 0, 'flag_ok_since': None}


def _gv(g, key, default):
    """取 guard 里的数值: **0 是有效值**(表示关掉这条), 不能用 `x or 默认值` —— 会被吃掉。"""
    v = g.get(key)
    return float(default) if v is None else float(v)


def profile_pl(cfg):
    """档位本身的值(插电/电池), **不看 _PL_OVERRIDE** —— 给"预警只收 PL2"用。"""
    c1 = float(cfg['pl1_w'] or 0)
    c2 = float(cfg['pl2_w'] or 0)
    b1 = float(cfg.get('pl1_w_batt') or 0)
    b2 = float(cfg.get('pl2_w_batt') or 0)
    if c1 <= 0:
        return None, None, '自动'
    if (not on_ac()) and b1 > 0:
        return max(b1, MIN_PL_W), (b2 or max(25.0, b1 * 1.25)), '电池/平衡'
    return max(c1, MIN_PL_W), (c2 if c2 > 0 else max(25.0, c1 * 1.25)), 'AC/高性能'


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
    now = time.time()
    if not g.get('enabled', True):
        if _G['level'] or _G.get('ramp'):
            _G['level'] = None
            _G['ramp'] = None
            _G['step'] = 0
            set_pl_override(None)
            return '过热保护: 配置里已关闭, 功耗墙恢复原档'
        return None
    hit = ec_tripped(flags) if g.get('trip_flags', True) else []   # 标志置位 -> 最高级
    if flags_fresh:
        # 必须连续两轮"真读"都读到才认 —— 单轮的垃圾读数(两个进程插队)不能让我们误判
        # "固件翻脸了"。沿用的缓存值既不算命中、也不清零: 它根本不是一轮读数, 要是让
        # 它清零, 标志读降到 30 秒一次后就得 60 秒才确认得了。
        _G['trip_hits'] = (_G.get('trip_hits', 0) + 1) if hit else 0
        if hit:
            _G['clr_hits'] = 0      # 看到标志就把"连着没标志"的计数清掉(串位读成 0 不算清零)
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
        _G['flag_ok_since'] = None
        # ★2026-10-19(补 06d4e01 漏掉的一半): 清零也要连续两轮真读才认 —— 而且**不管有没有
        #   进过"锁存"**。原来这段只写在 trip_latched 分支里, 可实测的抖动根本不进那个分支
        #   (trip_hold_s=45 秒永远追不上 42~44 秒的温度解除), 所以 clr_hits 一次都没生效。
        if flags_fresh:
            _G['clr_hits'] = int(_G.get('clr_hits') or 0) + 1
        if int(_G.get('clr_hits') or 0) >= 2:
            was_latched = bool(_G.get('trip_latched'))
            _G['trip_latched'] = False
            _G['clr_hits'] = 0
            released = _G['level'] in ('flag', 'trip')
            if released:
                _G['level'] = None
                _G['step'] = 0
                _G['ramp'] = None
                set_pl_override(None)
            if was_latched or released:
                return ('固件过热标志已清零(连续两轮真读确认)%s'
                        % (' —— 解除"锁存"判定, 功耗墙恢复原档(下次置位仍会正常保护)'
                           if was_latched else
                           ' —— 退出"标志降温"档, 功耗墙恢复原档'))
    elif _G.get('trip_hits', 0) >= 2:
        _G['clr_hits'] = 0
        if _G.get('trip_since') is None:
            _G['trip_since'] = time.time()
        # ---- ★标志档的"温度正常持续一段时间就解除"(2026-10-19 用户指出后加) ----------
        # 旧条件要求"EC <= recover_c(78°C) 满 45 秒"才算锁存, 而 78°C 在本机偏苛刻:
        # 28W 满载的稳态就是 89~95°C —— 于是标志一挂上, 只要温度在 78~95°C 之间就被
        # **永久**压在 20/28W(不再自动放开)。现在改成:
        #   连续 (ET <= flag_release_c, 默认 = 预警线 95°C) 达 flag_release_s(默认 60 秒)
        #   -> 判"固件锁存位, 不是真过热" -> 分级回升放开并保持(sticky)。
        # 关键是"连续": 中途温度升过线, 计时从头来 —— 所以不会 60 秒来回切。
        _rc = g.get('flag_release_c')
        rel_c = float(g['warn_c'] if _rc is None else _rc)
        _rs = g.get('flag_release_s')
        if _rs is None:
            _rs = g.get('trip_hold_s')            # 兼容旧配置: 旧键语义 = 锁存判定秒数
        rel_s = 60.0 if _rs is None else float(_rs)
        if et is not None and et > rel_c:
            _G['flag_ok_since'] = None            # 升过"正常线" -> 计时重来
        elif rel_s > 0 and not _G.get('trip_latched'):
            if _G.get('flag_ok_since') is None:
                _G['flag_ok_since'] = time.time()
            if time.time() - _G['flag_ok_since'] >= rel_s:
                _G['trip_latched'] = True
                _G['trip_since'] = time.time()
                _G['flag_ok_since'] = None
                pname = profile_pl(cfg)[2]
                _G['level'] = None
                _G['step'] = 0
                _G['released_at'] = time.time()
                _G['ramp'] = [list(x) for x in (g.get('ramp_pls')
                                                or [[20.0, 25.0], [28.0, 35.0]])] + [None]
                _G['ramp_at'] = time.time()
                set_pl_override(_G['ramp'][0][0], _G['ramp'][0][1])
                return ('固件过热标志 %s 挂着, 但 EC 温度已连续 %.0f 秒 ≤ %.0f°C —— 判定为'
                        '**固件锁存位**(不是真过热): 功耗墙分级回升到 [%s](先 %.0f/%.0fW), '
                        '标志清零前不再据此降档; 温度线 %g/%g 全程有效。'
                        % (desc, rel_s, rel_c, pname, _G['ramp'][0][0], _G['ramp'][0][1],
                           g['warn_c'], g['crit_c']))
        if _G.get('trip_latched'):
            hit = []            # 锁存期间: 标志不再参与定级

    # ---- ★解除后的分级回升(2026-10-19) ---------------------------------------
    # 从 10W/20W 一步跳回 40W 是功率阶跃, 温度会二次冲顶。现在按 ramp_pls 逐级回升。
    if _G['level'] is None and _G.get('ramp'):
        adv = False
        if now - float(_G.get('ramp_at') or 0.0) >= _gv(g, 'ramp_step_s', 30.0):
            _G['ramp'].pop(0)
            _G['ramp_at'] = now
            adv = True
        st = _G['ramp'][0] if _G['ramp'] else None
        if st is None:
            set_pl_override(None)
            _G['ramp'] = None
            if adv:
                return '过热保护解除后回升完成: 功耗墙已接回原档'
        else:
            set_pl_override(st[0], st[1])
            if adv:
                return ('过热保护解除后回升: PL1=%.0fW/PL2=%.0fW (每级 %.0f 秒, %s)'
                        % (st[0], st[1], _gv(g, 'ramp_step_s', 30.0),
                           '还要再抬一级' if len(_G['ramp']) > 1 else '这一级后接回原档'))

    # ---- ★温度墙的"驻留"计时(2026-10-19) --------------------------------------
    # 以前: 单次读到 >=crit_c 就立刻把功耗砍到 10W。现在: 持续 crit_hold_s 秒(默认 10 秒,
    # 约一个 EC 采样周期)才进"严重"; 单次冲到 crit_now_c(默认 100°C = TjMax)才立即动手。
    # 依据: 1360P 的 TjMax=100°C, 98°C 还有 2°C 余量; PL2(55W/6 秒)突发常造成瞬时尖峰,
    # 单点就砍等于把尖峰当持续过热, 白丢几十秒性能(实测 10:59~11:04 白压过 4 次)。
    if et is None or et < g['crit_c']:
        _G['crit_since'] = None
        if _G.get('level') in ('crit', 'crit2', 'crit3', 'trip'):
            # ★掉到严重线以下 -> 阶梯计时也重新开始。否则"在 98°C 附近来回碰线"会被一路
            #   加深到地板档(等于又回到"10W 白压"), 而真正的失控是"持续压不下来"。
            _G['step_since'] = now
    elif _G.get('crit_since') is None:
        _G['crit_since'] = now

    lv = None
    why = ''
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
        why = '固件标志'
    elif et is not None:                # 读不到 EC 温度就不按温度动, 免得误判
        if et >= g['escape_c']:
            lv, why = 'crit3', '逃生(≥%.0f°C)' % g['escape_c']
        elif et >= g['crit_c']:
            # ★已经在严重档时不许被"驻留门限"退回预警档 —— 否则单次 100°C 进来后, 下一轮
            #   99°C 会因为"计不满 10 秒"退成 warn(PL2 从 35 回到 40), 属于自造抖动。
            already = _G.get('level') in ('crit', 'crit2', 'crit3', 'trip')
            if (already or et >= _gv(g, 'crit_now_c', 100.0)
                    or (now - float(_G.get('crit_since') or now))
                    >= _gv(g, 'crit_hold_s', 10.0)):
                lv, why = 'crit', '温度'
            else:
                lv, why = 'warn', '温度(驻留中)'      # 驻留期内先只收 PL2
        elif et >= g['warn_c']:
            lv, why = 'warn', '温度'
    elif dts is not None and dts >= g.get('dts_crit_c', 97.0):
        why = 'DTS 兜底'
        # EC 读不到时才拿 CPU DTS 兜底。本机 DTS(MSR 0x1B1) 满载时读数会乱走,
        # 不能跟 EC 并列当触发条件 —— 否则会"压 10W -> 下一轮读回正常 -> 解除"地来回抖。
        lv = 'crit'
    if lv is None:
        # ★2026-10-19: flag 档只走两条出口 —— ① 45 秒锁存判定 ② 连续两轮确认清零。
        #   绝不能走下面这条"温度恢复就放开": flag 档本来就是在凉机上压的, 一进来就满足
        #   et <= recover_c, 40~44 秒后必然放开 -> 放开 3 秒又被压回去, 无限循环。
        if (_G['level'] and _G['level'] != 'flag'
                and et is not None and et <= g['recover_c']):
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
            pname = profile_pl(cfg)[2]
            _G['level'] = None
            _G['cool_since'] = None
            _G['step'] = 0
            _G['released_at'] = now      # ★风扇"尾巴"从这里起算(K3)
            # ★分级回升(2026-10-19): 不一步跳回原档, 免得温度二次冲顶
            _G['ramp'] = [list(x) for x in (g.get('ramp_pls') or [[20.0, 25.0],
                                                                 [28.0, 35.0]])] + [None]
            _G['ramp_at'] = now
            set_pl_override(_G['ramp'][0][0], _G['ramp'][0][1])
            return ('过热保护解除(EC %d°C ≤ %.0f°C 且稳住 %.0f 秒): 功耗墙分级回升 [%s], '
                    '原保护等级 %s —— 先给 PL1=%.0fW/PL2=%.0fW, 每 %.0f 秒抬一级'
                    % (et, g['recover_c'], float(g.get('release_dwell_s') or 0.0), pname, old,
                       _G['ramp'][0][0], _G['ramp'][0][1], _gv(g, 'ramp_step_s', 30.0)))
        _G['cool_since'] = None
        return None
    # ---- ★阶梯加深(2026-10-19): 已经在严重档而温度还压在墙上 -> 每 step_dwell_s 深一级
    WS = ('crit', 'crit2', 'crit3')
    if lv in ('crit', 'trip'):
        prev = _G.get('level') in WS
        step = int(_G.get('step') or 1) if prev else 1
        if prev:
            if et is not None and et >= g['escape_c']:
                step = 3
            elif (et is not None and et >= g['crit_c']
                  and (now - float(_G.get('step_since') or 0.0))
                  >= _gv(g, 'step_dwell_s', 15.0)):
                step = min(3, step + 1)
        if step != int(_G.get('step') or 0) or not prev:
            _G['step'] = step
            _G['step_since'] = now
        if why:
            # ★2026-10-19: "为什么"以**本轮**为准。原来只在"换级"时更新, 于是"温度线已经
            #   在 crit 档、固件标志这时才置位"这一路, _G['why'] 还是旧的'温度' ——
            #   下面"标志触发时取更严的一档"(clamp)就不会生效, 手册 02 §4.1 那条规则落空。
            _G['why'] = why
        lv = WS[step - 1]        # ★无论有没有换级, 等级都要对齐到当前级
    if lv == _G['level']:
        # ★2026-10-19: 等级没变、但"为什么"变了(温度线已在 crit, 固件标志这时才置位):
        #   标志档要求"取更严的一档"(默认 20/28W), 这里补一次重设, 否则会一直停在 28/35W。
        if lv in ('crit', 'crit2', 'crit3') and _G.get('why') == '固件标志' and _PL_OVERRIDE:
            st = {'crit': 1, 'crit2': 2, 'crit3': 3}.get(lv, 1)
            p1 = float((g.get('crit_pl1_w') if st == 1 else g.get('crit%d_pl1_w' % st)) or 28.0)
            p2 = float((g.get('crit_pl2_w') if st == 1 else g.get('crit%d_pl2_w' % st)) or 35.0)
            fp1 = float(g.get('flag_pl1_w') or 20.0)
            fp2 = float(g.get('flag_pl2_w') or 28.0)
            tgt = (min(p1, fp1), min(p2, fp2))
            cur = _PL_OVERRIDE
            if cur[0] > tgt[0] + 0.01 or cur[1] > tgt[1] + 0.01:
                set_pl_override(tgt[0], tgt[1])
                return ('过热保护[固件标志]: 等级不变(%s)但固件标志要求更严的功耗档 —— '
                        '功耗墙收到 PL1=%.0fW/PL2=%.0fW(原来 %.0fW/%.0fW), 风扇满速'
                        % (lv, tgt[0], tgt[1], cur[0], cur[1]))
        return None
    _G['level'] = lv
    _G['since'] = now
    _G['cool_since'] = None
    _G['ramp'] = None            # 一进保护就取消回升阶梯
    if lv in ('crit', 'crit2', 'crit3'):
        step = {'crit': 1, 'crit2': 2, 'crit3': 3}.get(lv, 1)
        _G['step'] = step
        p1 = float((g.get('crit_pl1_w') if step == 1 else g.get('crit%d_pl1_w' % step)) or 28.0)
        p2 = float((g.get('crit_pl2_w') if step == 1 else g.get('crit%d_pl2_w' % step)) or 35.0)
        # ★2026-10-19 实测发现: 固件标志挂着时, "温度也高"走第一级(28/35)反而比"标志降温档"
        #   (20/28)更松 —— 于是 EC 在 95°C 上下一点来回, 就 28/35 ↔ 20/28 反复切。
        #   规则: 标志触发时不许比标志档更松(取更严的那个); 阶梯仍可在它之下继续加深。
        clamp = _G.get('why') == '固件标志'
        if clamp:
            p1 = min(p1, float(g.get('flag_pl1_w') or 20.0))
            p2 = min(p2, float(g.get('flag_pl2_w') or 28.0))
        set_pl_override(p1, p2)
        tag = {1: '第一级', 2: '第二级', 3: '地板'}[step]
        nice = {1: 'PBP 28W 档', 2: 'Minimum Assured Power 20W 档',
                3: '地板档(只留给 100°C 或持续压不下来)'}[step]
        if _G.get('why') == '固件标志':
            if g.get('escape_hibernate') and step == 1:
                _G['hibernate'] = True
                return ('过热保护[固件标志]: EC %s —— 固件已把热区报成 _HOT(401K), '
                        '不等它强制断电, 立即主动休眠(S4)' % desc)
            return ('过热保护[固件标志]: EC %s 且温度也高(EC %d°C ≥ %.0f°C) —— 功耗墙压到 '
                    '%s PL1=%.0fW/PL2=%.0fW(不比标志降温档%.0f/%.0fW 更松), 风扇满速'
                    '(已连续两轮确认)'
                    % (desc, et, g['warn_c'], tag, p1, p2,
                       float(g.get('flag_pl1_w') or 20.0), float(g.get('flag_pl2_w') or 28.0)))
        if et is None:
            return ('过热保护[严重·%s]: EC 温度读不到, 用 CPU DTS %d°C 兜底 —— 功耗墙压到 '
                    'PL1=%.0fW/PL2=%.0fW, 风扇满速' % (tag, dts, p1, p2))
        if step == 1:
            cond = ('单次就到 TjMax %.0f°C' % _gv(g, 'crit_now_c', 100.0)
                    if et >= _gv(g, 'crit_now_c', 100.0)
                    else 'EC ≥%.0f°C 已持续 ≥%.0f 秒' % (g['crit_c'],
                                                       _gv(g, 'crit_hold_s', 10.0)))
        else:
            cond = '上一级压了 ≥%.0f 秒仍 ≥%.0f°C' % (_gv(g, 'step_dwell_s', 15.0), g['crit_c'])
        return ('过热保护[严重·%s]: EC %d°C ≥ %.0f°C%s (%s) —— 功耗墙压到 PL1=%.0fW/PL2=%.0fW '
                '(按 i7-1360P 的 %s 取值), 风扇满速'
                % (tag, et, g['crit_c'], ('/DTS %d°C' % dts) if dts is not None else '',
                   cond, p1, p2, nice))
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
    p1w = float(g.get('warn_pl1_w') or 0.0) or float(profile_pl(cfg)[0] or 0.0) or 40.0
    set_pl_override(p1w, float(g.get('warn_pl2_w') or 40.0))
    return ('过热保护[预警]: EC %d°C ≥ %.0f°C%s —— 只把 PL2 收到 %.0fW(PL1 仍 %.0fW, 保瞬时性能), '
            '风扇满速; 持续 %.0f 秒还这么高才进"严重"'
            % (et, g['warn_c'], (' (%s)' % _G['why']) if _G.get('why') else '',
               _gv(g, 'warn_pl2_w', 40.0), p1w, _gv(g, 'crit_hold_s', 10.0)))


def thermal_hib_info(minutes=20):
    """查系统日志里最近有没有 Kernel-Power 事件 88(严重热事件 -> 固件强制休眠)。

    2026-10-19 11:29:42 实测: EC 0x78 只有 58°C、DTS 58°C、负载 14%, 而固件热区
    \\_TZ.THM0 还是报了 _HOT -> ACPI 直接休眠(S4)。人看到的是"毫无征兆直接关机",
    其实会话被落盘了、开机后程序都还在。这条只在"刚开机/刚唤醒"时调一次(起个 powershell,
    ~1 秒), 让日志里留一句人话。
    """
    try:
        import subprocess
        ps = ('[Console]::OutputEncoding=[Text.Encoding]::UTF8;'
              '$ErrorActionPreference="SilentlyContinue";'
              'Get-WinEvent -FilterHashtable @{LogName="System";Id=88;'
              'StartTime=(Get-Date).AddMinutes(-%d)} -MaxEvents 1 -ErrorAction SilentlyContinue |'
              'ForEach-Object { $_.TimeCreated.ToString("MM-dd HH:mm:ss") }' % int(minutes))
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
                           capture_output=True, timeout=25)
        s = (r.stdout or b'').decode('utf-8', 'replace').strip()
        return s or None
    except Exception:
        return None


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
    # 负载前馈的计时放在这里(5 秒粒度; 放在 fan_tick 里只有 EC 的 15 秒粒度, 两次压测实测太慢)
    _fc = fan_cfg(cfg)
    _lbp = float(_fc.get('load_boost_pct') or 0.0)
    boost = False
    if _lbp > 0 and load is not None:
        if load >= _lbp:
            if _FAN.get('hi_since') is None:
                _FAN['hi_since'] = time.time()
            if time.time() - _FAN['hi_since'] >= float(_fc.get('load_boost_s') or 10.0):
                _FAN['boost_until'] = time.time() + float(_fc.get('load_hold_s') or 30.0)
        else:
            _FAN['hi_since'] = None
        boost = bool(_FAN.get('boost_until') and time.time() < _FAN['boost_until'])
    flags = _G.get('last_flags') or {}
    fresh = bool(ec and read_flags)
    if fresh:
        try:
            flags, complete = merge_flags(ec_trip_flags(_G.get('last_et')),
                                          _G.get('last_flags'))
            _G['last_flags'] = flags
            if not complete:
                # 有寄存器没读到 -> 这一轮不算新证据(既不算命中, 也不算清零)
                fresh = False
        except Exception:
            flags = _G.get('last_flags') or {}
            fresh = False
    if ec and _EC_TEMP_SENTINEL_SEEN:
        notes.append('注意: EC 温度寄存器(0x78)读到无效码 0x80 —— 固件的 ACPI 热区'
                     '会把它当成 48°C(0x30), 属于传感器异常, 别当真温度')
    n = guard_apply(cfg, et, dts, flags, flags_fresh=fresh)
    if n:
        notes.append(n)
    # ★2026-10-20 温度→功耗调节器("风扇优先"模型): 保护没在管的时候, 日常功耗跟着温度走;
    #   保护一旦进入任何等级, 调节器立刻让位(它只在 guard.temp_power=on 时才动手)。
    n = temp_power_apply(cfg, et, dts)
    if n:
        notes.append(n)
    n = ensure_profile(cfg)
    if n:
        notes.append(n)
    if ec:
        g = guard_cfg(cfg)
        tail = cool_tail_active(cfg)
        force = bool(g.get('force_fan_max', True)) and (
            _G['level'] in ('warn', 'crit', 'trip', 'escape', 'flag') or tail)
        try:
            n = fan_tick(cfg, et, force_max=force, load=load, dts=dts, boost=boost,
                         force_why='过热保护尾部' if tail and _G['level'] is None else None)
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
    """当前过热保护等级: None / 'warn' / 'crit' / 'trip' / 'escape' / 'flag'"""
    return _G.get('level')


def cool_tail_active(cfg):
    """刚退出过热保护(默认 30 秒内)? -> 风扇继续拉满, 缓冲功率阶跃带来的二次升温。"""
    t = float(guard_cfg(cfg).get('cool_tail_s') or 0.0)
    if t <= 0 or not _G.get('released_at'):
        return False
    return (time.time() - _G['released_at']) < t


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
      guard —— 过热保护生效期间: 温度读提到跟主循环同频, 标志读最多 15 秒一次
               (标志是锁存位, 读太勤只会增加 EC 事务和读失败);
      hot   —— EC 温度高于 flag_hot_c: 标志改跟 EC 读同频(平时 30 秒一次);
      jump  —— ★上一轮 EC 温度跳变被当可疑值压住了(temp_jump_pending): EC 读提前到
               EC_JUMP_RETRY_S, 赶紧再确认一次, 别让风扇白等一个 ec_poll_s。
    """
    d = DEFAULT_CFG
    poll = float(cfg.get('poll_s') or d['poll_s'])
    ec = float(cfg.get('ec_poll_s') or d['ec_poll_s'])
    fl = float(cfg.get('flag_poll_s') or d['flag_poll_s'])
    # ★凉机 + 没在保护: 把固件标志读降到 flag_poll_slow_s(默认 120 秒)。
    #   理由(2026-10-19): 标志读现在每次要 3 遍 × (哨兵+目标+哨兵) = 最多 9 次 EC 事务,
    #   而 EC 端口与系统 ACPI EC 驱动/固件共用, 读得越勤越容易互相插队(见 03 §4)
    #   —— 少打扰就是安全。凉机时读标志本来也不动手
    #   (温度正常 -> flag 档), 所以慢一点没有代价。热起来(>= flag_hot_c)或保护中就恢复。
    if not (fast or guard or hot):
        slow = cfg.get('flag_poll_slow_s')
        slow = 120.0 if slow is None else float(slow)
        if slow > 0:                      # 0 = 不降频(照旧 30 秒读)
            fl = max(fl, slow)
    if fast:
        poll, ec, fl = min(poll, 2.0), min(ec, 5.0), min(fl, 10.0)
    if jump:
        ec, fl = min(ec, EC_JUMP_RETRY_S), min(fl, EC_JUMP_RETRY_S)
    if guard:
        ec = min(ec, poll)
        # ★2026-10-19: 标志是"锁存位", 保护期间没必要跟主循环同频 —— 每次最多 9 次 EC 事务,
        #   而工程机读失败率 15~26%, 读得越勤越容易把"读不到"变成"标志没了"(见 merge_flags)。
        #   温度线仍跟主循环(5 秒), 标志降到 15 秒。
        fl = min(fl, max(ec, 15.0))
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
        out('生效档位          : [%s] MSR PL1=%.2fW(窗口 %s) PL2=%.2fW(窗口 %s) (mmio_mode=%s)'
            % (_pick_profile(cfg)[2], d_m['pl1_w'], _tau_s(d_m['pl1_t']),
               d_m['pl2_w'], _tau_s(d_m['pl2_t']), mode))
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
            elif any(v is None for v in fl.values()):
                # ★2026-10-19: 有寄存器读不到 ≠ 标志没了(f2007b1 的规矩)。显示上必须分开报,
                #   否则"读不到"会被当成"(正常)", 掩盖掉 §4.1 第 7 条那个坑。
                _s += ('   [!] 有寄存器**读不到**(显示 --) —— 这一轮不算证据, 沿用上一轮判断; '
                       '读不到 ≠ 标志清零(见 03 §4.1 第 7 条)')
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
        _n, _b, _ms, _re = ec_bus_stats()
        out('%s 本进程事务 %d 次 / 撞车 %d 次 / 读失败 %d 次, 当前自适应等待 %.1f ms'
            % (_pad('EC 总线', 26), _n, _b, _re, _ms))
        out('%s %s' % (_pad('风扇模式', 26), '%s   (config.json -> fan.mode)' % f.get('mode')))
        if str(f.get('mode')).lower() == 'curve':
            out('%s %s' % (_pad('温度曲线', 26),
                           ' '.join('≤%d°C->%s' % (hi, '满速' if lv >= FAN_MAX else '%d档' % lv)
                                    for hi, lv in f['curve'][:-1]) + '  更高->满速'))
        out('%s %s' % (_pad('过热保护', 26),
                       ('已开启  预警 %.0f°C->收 PL2 到 %.0fW(PL1 不动)  严重 %.0f°C 持续 %.0f 秒 -> '
                        '%.0f/%.0fW -> %.0f/%.0fW -> %.0f/%.0fW  单次 %.0f°C 立即  逃生 %.0f°C%s'
                        % (g['warn_c'], float(g.get('warn_pl2_w') or 40.0), g['crit_c'],
                           float(g.get('crit_hold_s') or 10.0),
                           g['crit_pl1_w'], g['crit_pl2_w'],
                           float(g.get('crit2_pl1_w') or 20.0), float(g.get('crit2_pl2_w') or 25.0),
                           float(g.get('crit3_pl1_w') or 12.0), float(g.get('crit3_pl2_w') or 14.0),
                           float(g.get('crit_now_c') or 100.0), g['escape_c'],
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
    # ★刚开机/刚唤醒: 查一下上一次是不是"固件热事件"强制休眠(Event 88), 留一句人话
    if tag.startswith('开机') or '唤醒' in tag:
        th = thermal_hib_info()
        if th:
            out('[*] 注意: 上一次是**固件热事件强制休眠**(Kernel-Power 88, %s) —— 不是掉电, '
                '会话已落盘、程序都还在; 固件热区 _TZ.THM0 报 _HOT 就会这样(手册 02 §3.2)' % th)
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
        ok, got = set_fan(n)
        if not ok:
            out('[X] 写 0x%02X 失败(EC 无响应)' % n)
            return 1
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
            m = fan_tick(cfg, ec_temp())       # 立即按曲线给一个档, 不用等下一轮
            if m:
                # ★2026-10-19: 原来把 fan_tick() 的返回值丢掉了 —— 手动切档触发的写操作
                #   不会进日志, 看起来像"0x2F 自己跳了档"。
                out('    %s' % m)
        tips = {'curve': '按温度自动(见 fan.curve)', 'max': '固定满速',
                'auto': '交还 EC 原生自动(本机原生曲线偏平, 到不了满速)'}
        out('[√] 风扇模式 -> %s (%s)' % (mapped[a], tips.get(a, '')))
        rpm = ec_rpm()                      # 只读一次(原来这里 ec_rpm() 被调了两遍)
        out('    现在: %s, %s' % (fan_name(ec_hfsp()),
                                  ('%d RPM' % rpm) if rpm is not None else ''))
        return 0
    out('用法: CpuPLFix.py fan [状态|curve|max|auto|level N]')
    return 2


def cmd_lock():
    """看两把命名互斥体能不能被**普通用户**打开(DACL 有没有放开)。**不需要管理员**。

    2026-10-19: 原来"跨进程互斥"这条保险在本机其实是失效的 —— 命名互斥体默认继承
    **创建者**的默认 DACL, 而看守服务以 SYSTEM 身份创建 -> 用户级进程 OpenMutex 一律
    ERROR_ACCESS_DENIED(实测四种权限组合全是 err=5), 于是 2-check / fan 命令 /
    13-EC诊断 / 15-EC延时对照 这些用户级工具一把锁都没拿到, 等于完全不加锁地和
    看守服务抢同一对端口。现在改成显式给 "Everyone: SYNCHRONIZE|MUTEX_MODIFY_STATE"
    的 DACL, 这个命令就是用来验它的。
    """
    out('EC 互斥体可及性（不需要管理员）:')
    ok_all = True
    for slot, name in (('_probe_own', EC_MUTEX_NAME), ('_probe_peer', EC_MUTEX_PEER)):
        h = _mx_handle(slot, name)
        if not h:
            out('  %-22s  打开失败' % name)
            ok_all = False
            continue
        try:
            r = k32.WaitForSingleObject(h, 0)
        except Exception:
            r = -1
        if r == 0:
            try:
                k32.ReleaseMutex(h)
            except Exception:
                pass
        state = {0: '空闲(能立刻拿到锁)',
                 0x80: '上次持有者异常退出(已废弃)',
                 0x102: '正被别人持有(服务在跑时正常)'}.get(r, '未知(%s)' % r)
        out('  %-22s  可打开; %s' % (name, state))
    out('')
    out('  "可打开" = 普通用户也拿得到这两把锁 -> 跨进程互斥、以及和 HWiNFO 那类'
        '用户级工具的"同行锁"才真正生效。')
    out('  打不开 = DACL 又被收窄了（服务是 SYSTEM 身份创建的, 会继承 SYSTEM 的 DACL）。')
    return 0 if ok_all else 1


def _pl_time(v, shift):
    """MSR 0x610 里的时间窗字段: bits 16:17=Y, 22:23=Z -> 秒 = 2^Y * (1 + Z/4)。"""
    y = (v >> shift) & 3
    z = (v >> (shift + 6)) & 3
    return (1 << y) * (1.0 + z / 4.0)


def _rapl_pl(v):
    """MSR 0x638/0x639/0x64A: bits 15:0 = 瓦(1/8), 16:17/22:23 = 时间窗。"""
    if v is None:
        return None, None
    return (v & 0x7FFF) / 8.0, _pl_time(v, 16)


def cmd_power():
    """打印这颗 CPU 的功耗信息: 芯片自己声明的约束 / 当前生效的功耗墙 / 实测封装功率。

    2026-10-19 加: 用户问"这 CPU 日常功耗应该设多少"。光看 config 里的 28W 是不够 ——
    要看**硬件自己**声明的约束，那才是这颗芯片的标称功耗:
      MSR 0x638 PP0_RAPL_PL1_HW  硬件强制的 PL1 (= 芯片的 PBP)
      MSR 0x639 PP0_RAPL_PL2_HW  硬件强制的 PL2 (= 芯片的 MTP)
      MSR 0x64A PP0_RAPL_PL4_HW  硬件强制的 PL4 (瞬时峰值)
      MSR 0x614 PKG_POWER_INFO   标称 TDP
      MSR 0x615 PKG_THERMAL_MAX  硬件过热上限
      MSR 0x610/0x612            当前**生效**的功耗墙和时间窗(工具写进去的)
      MSR 0x611 + 0x606          封装能耗计数器 -> 实测功率

    对照:
      i7-1360P (4P+8E, 18MB L3)  PBP 28W / MTP 64W / TjMax 100°C
      i7-1365U (2P+8E+2LPE,18MB) PBP 15W / MTP 55W / TjMax 100°C
      —— 两者都是 12核16线程 + 18MB L3，光看 WMI 分不出来，得看上面那两个 MSR。
    """
    out('=== CPU 功耗信息  %s ===' % time.strftime('%Y-%m-%d %H:%M:%S'))
    out()
    if not is_admin():
        out('[!] 读 MSR 需要管理员权限 —— 正在弹 UAC 提权（提权后的窗口会停住，按回车关闭）...')
        try:
            if elevate():
                return 0
        except Exception:
            pass
        out('[X] 提权失败；请在**管理员**命令行里跑: %s power' % os.path.basename(sys.argv[0]))
        return 1
    out('--- 芯片自己声明的约束（硬件强制，改不了）---')
    pl1_hw, t1_hw = _rapl_pl(rdmsr(0x638))
    pl2_hw, t2_hw = _rapl_pl(rdmsr(0x639))
    pl4_hw, t4_hw = _rapl_pl(rdmsr(0x64A))
    out('  PL1 硬件上限 (0x638) : %s' % ('%.0f W (窗 %s 秒)' % (pl1_hw, t1_hw) if pl1_hw else '读不到'))
    out('  PL2 硬件上限 (0x639) : %s' % ('%.0f W (窗 %s 秒)' % (pl2_hw, t2_hw) if pl2_hw else '读不到'))
    out('  PL4 硬件上限 (0x64A) : %s' % ('%.0f W (窗 %s 秒)' % (pl4_hw, t4_hw) if pl4_hw else '读不到'))
    tdp = rated_tdp()
    out('  标称 TDP     (0x614) : %s' % ('%.0f W' % tdp if tdp else '读不到'))
    tmax = rdmsr(0x615)
    out('  过热上限     (0x615) : %s' % ('%.0f W' % ((tmax & 0x7FFF) / 8.0) if tmax else '读不到'))
    if pl1_hw and pl2_hw:
        if abs(pl1_hw - 28) <= 2 and abs(pl2_hw - 64) <= 3:
            out('  → 与 **i7-1360P** 一致（PBP 28W / MTP 64W）')
        elif abs(pl1_hw - 15) <= 2 and abs(pl2_hw - 55) <= 3:
            out('  → 与 **i7-1365U** 一致（PBP 15W / MTP 55W）')
        else:
            out('  → 两款零售 SKU 都不完全对上（工程样本/另有 SKU），按实测值定功耗墙')
    out()
    out('--- 当前生效的功耗墙（工具写进 MSR 的）---')
    v = rdmsr(0x610)
    if v:
        d = decode_pl(v)
        out('  MSR 0x610: PL1=%.0fW (窗 %.1f 秒)  PL2=%.0fW (窗 %.1f 秒)  锁定位=%d'
            % (d['pl1_w'], _pl_time(v, 16), d['pl2_w'], _pl_time(v, 48), d['lock']))
    else:
        out('  MSR 0x610: 读不到')
    vt = rdmsr(0x612)
    if vt:
        out('  MSR 0x612: PL1 窗 %.1f 秒 / PL2 窗 %.1f 秒' % (_pl_time(vt, 16), _pl_time(vt, 48)))
    cfg = load_cfg()
    out('  config.json: pl1_w=%.0f pl2_w=%.0f (电池 %.0f/%.0f) pl1_time=%s pl2_time=%s'
        % (float(cfg.get('pl1_w') or 0), float(cfg.get('pl2_w') or 0),
           float(cfg.get('pl1_w_batt') or 0), float(cfg.get('pl2_w_batt') or 0),
           cfg.get('pl1_time'), cfg.get('pl2_time')))
    out()
    out('--- 实测封装功率（MSR 0x611 能量计数器）---')
    unit = rdmsr(0x606)
    e_unit = (1.0 / (1 << 16)) if unit else (1.0 / 65536.0)
    e0 = rdmsr(0x611)
    if e0 is None:
        out('  读不到能量计数器')
        return 1
    e0 &= 0xFFFFFFFF
    t0 = time.time()
    time.sleep(2.0)
    e1 = (rdmsr(0x611) or 0) & 0xFFFFFFFF
    dt = max(0.001, time.time() - t0)
    p_idle = ((e1 - e0) & 0xFFFFFFFF) / e_unit / dt
    out('  空闲封装功率: %.1f W' % p_idle)
    th = cpf_read_thermal()
    if th:
        out('  当前核心温度(DTS 公式): %d°C (TjMax %d°C, 节流标志=%d)'
            % (th['temp'], th['tjmax'], th['thermal_status']))
    # ---- 短负载: 看功耗墙到底有没有生效 ----
    ncpu = os.cpu_count() or 8
    n = min(8, ncpu)
    out()
    out('  正在跑 %d 线程满载 15 秒，看实际功率 ...' % n)
    stop = threading.Event()
    ths = []

    def spin():
        x = 0.0
        while not stop.is_set():
            for _ in range(20000):
                x = x * 1.000001 + 1.0

    for _ in range(n):
        t = threading.Thread(target=spin, daemon=True)
        t.start()
        ths.append(t)
    samples = []
    e_prev = (rdmsr(0x611) or 0) & 0xFFFFFFFF
    t_prev = time.time()
    t_start = time.time()
    while time.time() - t_start < 15.0:
        time.sleep(0.5)
        e_now = (rdmsr(0x611) or 0) & 0xFFFFFFFF
        now = time.time()
        d = max(0.01, now - t_prev)
        samples.append(((e_now - e_prev) & 0xFFFFFFFF) / e_unit / d)
        e_prev, t_prev = e_now, now
    stop.set()
    for t in ths:
        t.join(timeout=1.0)
    e_end = (rdmsr(0x611) or 0) & 0xFFFFFFFF
    dt = max(0.01, time.time() - t_prev)
    samples.append(((e_end - e_prev) & 0xFFFFFFFF) / e_unit / dt)
    if samples:
        out('  满载封装功率: 平均 %.1f W / 峰值 %.1f W / 末值 %.1f W'
            % (sum(samples) / len(samples), max(samples), samples[-1]))
    th = cpf_read_thermal()
    if th:
        out('  满载后核心温度: %d°C (节流标志=%d)' % (th['temp'], th['thermal_status']))
    out()
    out('  怎么看: 若"满载平均"明显超过 PL1，说明功耗墙没压住（PL2 突发的 5 秒窗口也算进去）；')
    out('        若峰值≈PL2 而平均≈PL1，说明功耗墙工作正常。')
    try:
        if sys.stdin and sys.stdin.isatty():
            input('\n按回车关闭这个窗口 ...')
    except Exception:
        pass
    return 0


# ---- ★温度→功耗调节器("风扇优先"模型) --------------------------------------
# 2026-10-20 用户提出: "高负载下风扇满速, 然后最大功耗能是多少, 用这种方式来控温;
# 不应该每次都撞功耗墙, 撞功耗墙应该是一种保护手段。"
# 这台机器的散热能力(实测): 风扇满速时 28W 能稳在 EC 89~95°C, 40W 会在 30 秒~2 分钟里
# 撞到 99°C —— 所以"最大功耗能是多少"的答案是 **持续 ~28~30W, 短时能到 45~64W(吃热容)**。
#
# 为什么不能直接拿保护阶梯(guard_apply)当调速器:
#   阶梯是"只降不升"的 —— 释放条件是 EC <= recover_c(78°C), 而热着的机器根本到不了 78°C,
#   于是它会一路踩到地板档(12W)并且**永远停在那**(手册 02 §3.1 记过同款 bug:
#   "凉机上被白压两次 10W")。所以日常调速必须是一个**独立、带回差、能升回来**的调节器,
#   保护阶梯只留作硬保护(98/105°C 那两级)。
#
# 映射(默认, 保守; 每一档是"温度低于阈值时能用的 PL1"): 越热越收, 顶档受 config 的
# pl1_w 约束(那就是用户说的"最大功耗能是多少")。回差 3°C, 每轮最多改 3W(防抖)。
TEMP_POWER_DEFAULT_MAP = [[82.0, 45.0], [85.0, 38.0], [90.0, 30.0],
                           [93.0, 28.0], [96.0, 22.0]]
TEMP_POWER_HYST = 3.0        # °C: 降档要越过阈值, 升回来要低 3 度
TEMP_POWER_RATE = 3.0        # W/次: 每轮最多改这么多


def temp_power_apply(cfg, et=None, dts=None):
    """按温度决定"日常功耗"(风扇优先模型)。返回变化描述; 没动返回 None。

    开启条件: config.json 的 `guard.temp_power = true`(**默认 false = 保持老行为**)。
    让位条件: 只要过热保护处于任何等级(warn/crit/crit2/crit3/trip/escape/flag),
              就完全交给保护阶梯 —— 调节器只管"没有保护时的日常功耗"。

    2026-10-20: 现在 PL1 是个静态值(28W), 任何重负载都"撞功耗墙"(CPU 被功耗限住,
    而不是被温度限住); 本调节器把"日常功耗"变成温度的函数: 温度低就多吃瓦(吃热容、
    相当于一次 turbo 突发), 温度上来了按比例收 —— 撞功耗墙重新变成**保护**手段。
    """
    g = guard_cfg(cfg)
    if not bool(g.get('temp_power', False)):
        if _G.get('tp_on'):
            _G['tp_on'] = False
            _G['tp_idx'] = None
            _G['tp_pl1'] = 0.0
            if _G.get('level') is None:
                set_pl_override(None)      # 交回配置里的档
        return None
    if _G.get('level') is not None:
        return None                      # 保护在管, 调节器让位
    if et is None:
        et = last_ec_temp()
    if et is None:
        return None
    ceiling = float(cfg.get('pl1_w') or 0) or 45.0
    if not on_ac():
        ceiling = float(cfg.get('pl1_w_batt') or 0) or min(ceiling, 20.0)
    mp = g.get('temp_power_map') or TEMP_POWER_DEFAULT_MAP
    try:
        mp = [(float(a), float(b)) for a, b in mp]
    except Exception:
        mp = TEMP_POWER_DEFAULT_MAP
    # 目标档: 温度落在哪个区间(索引越大 = 越热 = 功耗越低)
    idx = 0
    for i in range(len(mp)):
        if et >= mp[i][0]:
            idx = i
    # 往更凉的方向(索引变小=功耗变大)要留回差, 否则会在阈值上反复横跳
    cur = _G.get('tp_idx')
    if cur is not None and idx < cur:
        if et >= mp[cur][0] - TEMP_POWER_HYST:
            idx = cur
    want = min(float(mp[idx][1]), ceiling)
    floor = float(g.get('temp_power_min') or 18.0)
    want = max(want, min(floor, ceiling))
    # ★映射表是"风扇**满速**时"测出来的散热能力 —— 风扇没满速就不能按这张表给瓦数,
    #   否则轻负载(风扇还在 6 档)时会把超出散热能力的瓦放出去。
    #   所以: 风扇没到满速, 就只给"保守档"(temp_power_fanlow, 默认 28W = 今天的固定档);
    #   等风扇满速了(高负载时前馈会拉满)才放开。这正是用户那个模型:
    #   "高负载 → 风扇满速 → 功耗给到散热能力允许的上限"。
    fan_lv = _FAN.get('lv')
    if not (fan_lv is not None and fan_lv >= FAN_MAX):
        cap = float(g.get('temp_power_fanlow') or 28.0)
        if want > cap:
            want = min(cap, ceiling)
    # 每轮最多改 TEMP_POWER_RATE 瓦, 防抖
    last = float(_G.get('tp_pl1') or 0.0)
    if _G.get('tp_on') and last > 0:
        if want > last + TEMP_POWER_RATE:
            want = last + TEMP_POWER_RATE
        elif want < last - TEMP_POWER_RATE:
            want = last - TEMP_POWER_RATE
    pl2_cap = float(cfg.get('pl2_w') or 0) or 0
    pl2 = min(pl2_cap, max(want * 1.5, want)) if pl2_cap else want * 1.5
    _G['tp_on'] = True
    _G['tp_idx'] = idx
    if abs(last - want) < 0.5 and _G.get('tp_pl1'):
        return None
    _G['tp_pl1'] = want
    set_pl_override(want, pl2)
    return ('功耗调节[温度→瓦]: EC %.0f°C -> PL1 %.0fW / PL2 %.0fW'
            '（上限 %.0fW = config 的 pl1_w；保护阶梯仍只在 ≥98°C 时接手）'
            % (et, want, pl2, ceiling))


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
        print('用法: CpuPLFix.py [check|fix|save|install|uninstall|watch|boot|fan|lock|power]')
        print('  风扇: CpuPLFix.py fan [状态|curve|max|auto|level N]')
        print('  互斥: CpuPLFix.py lock  (不需要管理员; 看两把 EC 锁普通用户能不能打开)')
        print('  功耗: CpuPLFix.py power  (要管理员; 芯片功耗约束 + 生效的功耗墙 + 实测功率)')
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
          'install': cmd_install, 'uninstall': cmd_uninstall, 'watch': cmd_watch,
          'lock': cmd_lock, 'power': cmd_power}.get(cmd)
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
