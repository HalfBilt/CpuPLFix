# -*- coding: utf-8 -*-
"""
CpuPLFix —— 修复开机 / 睡眠唤醒后 CPU 被锁在 0.55GHz 的问题,
            并按 AC / 电池自动切换功耗墙(插电高性能 / 电池平衡)

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
守护服务(CpuPLFixSvc)在开机 / 睡眠唤醒 / 拔插电时 1 秒内对齐目标状态。

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
MIN_PL_W = 5.0               # 手动指定功耗墙时的安全下限, 低于它机器会慢到没法用
MCHBAR_REG = 0x48
MMIO_PL_OFF = 0x59A0
MSR_PKG_POWER_LIMIT = 0x610
MSR_PERF_STATUS = 0x198

# ThrottleStop 驱动的 IOCTL 码(由驱动分发表逆向得到, 见 README)
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
    "comment": "pl1_w/pl2_w = 插电(高性能)档, pl1_w_batt/pl2_w_batt = 电池(平衡)档。mmio_mode: disable = MMIO清零+锁后只写MSR(免重启切档, 实测本机MSR可写时推荐); value = 写目标值进MMIO+锁(老方案, 改配置要重启)。"
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


_lines = []


def out(msg=''):
    try:
        if sys.stdout is not None:      # pythonw.exe 下 stdout 为 None
            print(msg)
    except Exception:
        pass
    _lines.append(msg)


def flush_log(tag='run'):
    """把当前缓冲写盘并清空 —— 这样看守期间也能随时看到已发生的事"""
    if not _lines:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        p = os.path.join(LOG_DIR, 'fix.log')
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


# ---------------------------------------------------------------- 命令
def cmd_check():
    if not ensure_driver():
        out('[X] 驱动加载失败, 无法访问硬件寄存器')
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
            out('CPU 温度           : %d °C  [!] 接近/达到 TjMax — 这是温度墙限频, 和功耗墙无关' % tv['temp'])
            out('                     (治本: 清灰换硅脂; 或把 pl1_w 降到 35~40)')
        else:
            out('CPU 温度           : %d °C  (TjMax=%d, 公式 TjMax-DTS; 仅空闲可信, 满载会失真)'
                % (tv['temp'], tv['tjmax']))
            if ratio <= 8:
                out('[!] 倍频只有 %.1fGHz —— 空闲态确实异常, 看上面的功耗墙状态' % (ratio / 10.0))

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
    return 0


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
    """按当前电源状态选档。返回 (pl1, pl2, 档位名); pl1=None 表示自动模式。"""
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
                    wrphys(mb + MMIO_PL_OFF, MMIO_DISABLED_LOCKED)
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
                           build_pl(pl1, pl2, t1, t2, bool(cfg.get('lock_mmio', True))))
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
    ok, msg = svc_create(SVC_TOOL, binpath, 'CpuPLFix 功耗墙看守',
                         SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL)
    if not ok:
        rc, txt = _sc('create', SVC_TOOL, 'type=', 'own', 'start=', 'auto',
                      'error=', 'normal', 'DisplayName=', 'CpuPLFix 功耗墙看守',
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
    out('')
    out('安装完成。开机、登录、睡眠唤醒、休眠恢复都会自动修复, 日志在 %s' % LOG_DIR)
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
    out('[2/3] 看守服务: %s' % ('已删除' if _remove_tool_service() else '不存在或删除失败'))
    rc, txt = _sc('delete', SVC)
    out('[3/3] 驱动服务: %s' % ('已删除' if rc == 0 else txt.strip()[:120]))
    return 0


def cmd_boot():
    """开机 / 唤醒后尽早执行。已经健康就直接退出, 不健康才修复并进看守。"""
    t_start = time.time()
    up = uptime()
    # 同一个入口既服务"开机"也服务"睡眠/休眠唤醒"(计划任务的三种触发器都调它),
    # 靠系统运行时长区分, 方便事后看日志判断这次到底是哪种情况。
    if up < 0:
        tag = '触发'
    elif up < 600:
        tag = '开机'
    else:
        tag = '睡眠/休眠唤醒后'
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
        flush_log('boot')
        return 0

    # ---- 需要修复 ----
    out('[*] 需要修复, 开始执行 (系统已运行 %.1f 秒)' % uptime())
    try:
        cmd_fix(quiet=True)
    except Exception as e:
        out('[*] 首次修复异常: %s' % e)
    out('[*] 首次修复结束: 用时 %.2f 秒, 系统已运行 %.1f 秒'
        % (time.time() - t_start, uptime()))
    flush_log('boot')          # 立刻落盘, 不必等看守结束

    fixed_times = 0
    while time.time() - t_start < BOOT_WATCH_SEC:
        time.sleep(2.0)
        try:
            if not ensure_driver(verbose=False):
                continue
            # disable 模式: 只需要保证 MMIO 还是"清零+锁" + MSR 跟着当前电源档
            # value 模式:  MMIO 被固件压低就按当前档重写 + 上锁
            note = ensure_profile(cfg)
            if note:
                out('[*] %4.0f 秒: %s' % (time.time() - t_start, note))
                fixed_times += 1
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


def cmd_watch(interval=30):
    out('监控模式: 每 %d 秒对齐一次功耗墙/电源档位 (Ctrl+C 退出)' % interval)
    while True:
        try:
            if ensure_driver(verbose=False):
                cfg = load_cfg()
                note = ensure_profile(cfg)
                if note:
                    out('%s %s' % (datetime.datetime.now().strftime('%H:%M:%S'), note))
                    flush_log('watch')
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            out('watch 异常: %s' % e)
            time.sleep(interval)


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
        print('用法: CpuPLFix.py [check|fix|save|install|uninstall|watch|boot]')
        return 0

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
