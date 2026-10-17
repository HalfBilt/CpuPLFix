# -*- coding: utf-8 -*-
"""
CpuPLFix —— 修复开机 / 睡眠唤醒后 CPU 被锁在 0.55GHz 的问题

原理
----
Intel 平台的封装功耗墙(PL1/PL2)同时存在于两处:
  1) MSR 0x610  (PKG_POWER_LIMIT)         —— CPU 内部, 操作系统/ThrottleStop 用这一份
  2) MCHBAR+0x59A0 (MMIO 镜像)            —— 主板/EC 固件用的那一份, bit63 是锁定位

本机实测(正常状态):
  MSR 0x610      = PL1 55W / PL2 55W / 未锁
  MCHBAR+0x59A0  = PL1 21W / PL2 55W / 未锁     <-- 被固件压低, 且随时可能被改得更低
降频到 0.55GHz 时, 这份 MMIO 值会被写成只有几瓦, CPU 于是被钉在最低频。

本工具做的事: 把 MSR 里那份正常值写回 MMIO, 并置 bit63 锁定位,
固件之后就无法再改回去 —— 与 ThrottleStop 的 "MMIO Lock" 完全等价, 但无需 GUI、开机即可自动执行。

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
TASK_NAME = 'CpuPLFix'

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
    "pl1_time": 35,
    "pl2_time": 66,
    "min_pl1_w": 15.0,
    "lock_mmio": True,
    "comment": "pl1_w/pl2_w = 0 表示自动(取 CPU 标称 TDP, 读 MSR 0x614)。只有当 MSR 里的 PL1 低于 min_pl1_w(即系统自己也被压坏)时才会用到它们；正常情况一律沿用 MSR 里的值，不会擅自改高。"
}

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.CreateFileW.restype = w.HANDLE
k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
k32.DeviceIoControl.restype = w.BOOL
k32.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, w.DWORD,
                                ctypes.POINTER(w.DWORD), w.LPVOID]
k32.GetConsoleWindow.restype = w.HWND

_lines = []


def out(msg=''):
    try:
        if sys.stdout is not None:      # pythonw.exe 下 stdout 为 None
            print(msg)
    except Exception:
        pass
    _lines.append(msg)


def flush_log(tag='run'):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        p = os.path.join(LOG_DIR, 'fix.log')
        with open(p, 'a', encoding='utf-8') as f:
            f.write('\n===== %s %s =====\n' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), tag))
            f.write('\n'.join(_lines) + '\n')
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


def ensure_driver(verbose=True):
    """确保驱动服务已加载, 返回设备句柄"""
    global _handle
    if _handle:
        return _handle

    h = _open_dev()
    if h:
        _handle = h
        return h

    rc, txt = _sc('query', SVC)
    if '1060' in txt:                      # 服务不存在 -> 创建
        if not ensure_driver_file():
            return None
        rc, txt = _sc('create', SVC, 'type=', 'kernel', 'start=', 'demand',
                      'error=', 'ignore', 'binPath=', SYS_DST)
        if verbose:
            out('[*] 创建驱动服务: %s' % ('成功' if rc == 0 else txt.strip()))

    rc, txt = _sc('start', SVC)
    if verbose and rc != 0 and '1056' not in txt and 'RUNNING' not in txt:
        out('[*] 启动驱动服务: %s' % txt.strip()[:160])

    for _ in range(20):                 # 开机早期可能还没准备好, 最多等 10 秒
        h = _open_dev()
        if h:
            _handle = h
            return h
        time.sleep(0.5)
    return None


def ioctl(code, inbuf, outlen):
    o = ctypes.create_string_buffer(max(outlen, 1))
    ret = w.DWORD(0)
    ib = ctypes.create_string_buffer(inbuf if inbuf else b'\x00', max(len(inbuf), 1))
    ok = k32.DeviceIoControl(_handle, code, ib, len(inbuf), o, outlen, ctypes.byref(ret), None)
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

    mb = mchbar()
    msr = rdmsr(MSR_PKG_POWER_LIMIT)
    mmio = rdphys(mb + MMIO_PL_OFF, 8) if mb else None
    d_m = decode_pl(msr) if msr is not None else None
    d_o = decode_pl(mmio) if mmio is not None else None

    ratio = (rdmsr(MSR_PERF_STATUS) or 0) >> 8 & 0xFF

    out('')
    out('CPU 当前倍频      : %d  (约 %.2f GHz)' % (ratio, ratio / 10.0))
    out('MCHBAR            : %s' % (('0x%X' % mb) if mb else '读取失败'))
    out('')
    out('%-26s %-22s %s' % ('', 'MSR 0x610 (系统侧)', 'MMIO +0x59A0 (固件侧)'))
    if d_m and d_o:
        out('%-26s %-22s %s' % ('PL1 长时功耗墙', '%.2f W' % d_m['pl1_w'], '%.2f W' % d_o['pl1_w']))
        out('%-26s %-22s %s' % ('PL2 短时功耗墙', '%.2f W' % d_m['pl2_w'], '%.2f W' % d_o['pl2_w']))
        out('%-26s %-22s %s' % ('锁定位 bit63', d_m['lock'], d_o['lock']))
        out('')
        if abs(d_m['pl1_w'] - d_o['pl1_w']) > 0.5:
            out('[!] 两侧 PL1 不一致 —— 这正是降频的根源')
        if not d_o['lock']:
            out('[!] MMIO 未锁定 —— 固件随时可以再把它改低')
        if d_m['pl1_w'] >= d_o['pl1_w'] - 0.5 and d_o['lock']:
            out('[√] 当前状态正常')
    else:
        out('[X] 寄存器读取失败')
    return 0


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
    #   config 里写了具体瓦数(>0) -> 作为下限, 保证不低于它(用户明确指定)
    #   config 为 0(自动)        -> 镜像 MSR 的值; 若 MSR 本身也坏掉了, 退回到 CPU 标称 TDP
    tdp = rated_tdp()
    c1, c2 = float(cfg['pl1_w'] or 0), float(cfg['pl2_w'] or 0)
    if c1 > 0:
        pl1 = max(d_m['pl1_w'], c1)
        pl2 = max(d_m['pl2_w'], c2) if c2 > 0 else max(d_m['pl2_w'], pl1 * 1.25)
    else:
        if d_m['pl1_w'] >= cfg['min_pl1_w']:
            pl1, pl2 = d_m['pl1_w'], d_m['pl2_w']
        else:
            pl1 = tdp or 25.0
            pl2 = round(tdp * 1.25, 2) if tdp else round(pl1 * 1.25, 2)
            if not quiet:
                out('[*] MSR 里的 PL1 只有 %.2fW, 判定为异常; 兜底取 CPU 标称 TDP: PL1=%.2fW PL2=%.2fW'
                    % (d_m['pl1_w'], pl1, pl2))
    t1 = d_m['pl1_t'] or int(cfg['pl1_time'])
    t2 = d_m['pl2_t'] or int(cfg['pl2_time'])

    target = build_pl(pl1, pl2, t1, t2, bool(cfg.get('lock_mmio', True)))

    if not quiet:
        out('修复前: MSR PL1=%.2fW PL2=%.2fW | MMIO PL1=%.2fW PL2=%.2fW lock=%s'
            % (d_m['pl1_w'], d_m['pl2_w'],
               d_o['pl1_w'] if d_o else -1, d_o['pl2_w'] if d_o else -1,
               d_o['lock'] if d_o else '?'))

    ok = False
    for attempt in range(3):
        wrphys(mmio_addr, target, 8)
        time.sleep(0.15)
        back = rdphys(mmio_addr, 8)
        if back == target:
            ok = True
            break
        time.sleep(0.5)

    # MSR 侧如果也被压低了, 顺手抬回(不置锁定位, 免得以后 ThrottleStop 改不动)
    if d_m['pl1_w'] < cfg['min_pl1_w'] and not d_m['lock']:
        wrmsr(MSR_PKG_POWER_LIMIT, build_pl(pl1, pl2, t1, t2, False))

    back = rdphys(mmio_addr, 8)
    d_b = decode_pl(back) if back is not None else None
    if d_b:
        out('修复后: MMIO PL1=%.2fW PL2=%.2fW lock=%s  %s'
            % (d_b['pl1_w'], d_b['pl2_w'], d_b['lock'], '[√] 已生效' if ok else '[X] 写入未生效'))
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
    out('[√] 已保存参考值: PL1=%.2fW PL2=%.2fW' % (d['pl1_w'], d['pl2_w']))
    return 0


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>开机与睡眠唤醒后修复 CPU 功耗墙(MMIO Lock)</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT15S</Delay>
    </BootTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="System"&gt;&lt;Select Path="System"&gt;*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
      <Delay>PT4S</Delay>
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
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(exe)s</Command>
      <Arguments>"%(script)s" fix</Arguments>
      <WorkingDirectory>%(dir)s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def cmd_install():
    if not is_admin():
        out('[!] 需要管理员权限, 正在请求提权...')
        return 0 if elevate() else 1

    if not ensure_driver_file():
        return 1

    # 1. 驱动服务
    _sc('stop', SVC)
    _sc('delete', SVC)
    time.sleep(0.4)
    rc, txt = _sc('create', SVC, 'type=', 'kernel', 'start=', 'demand',
                  'error=', 'ignore', 'binPath=', SYS_DST)
    out('[1/3] 驱动服务: %s' % ('创建成功' if rc == 0 else txt.strip()[:120]))

    # 2. 计划任务
    exe = sys.executable
    pw = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    if os.path.exists(pw):
        exe = pw
    xml = TASK_XML % {'exe': exe, 'script': os.path.abspath(__file__), 'dir': BASE}
    tmp = os.path.join(BASE, '_task.xml')
    with open(tmp, 'w', encoding='utf-16') as f:
        f.write(xml)
    p = subprocess.run(['C:\\Windows\\System32\\schtasks.exe', '/create', '/tn', TASK_NAME,
                        '/xml', tmp, '/f'], capture_output=True, text=True, errors='replace',
                       creationflags=0x08000000)
    ok = p.returncode == 0
    out('[2/3] 计划任务: %s' % ('已创建(开机 + 唤醒时自动运行)' if ok
                              else (p.stdout + p.stderr).strip()[:200]))
    try:
        os.remove(tmp)
    except Exception:
        pass

    # 3. 立即执行一次
    out('[3/3] 立即应用一次:')
    rc = cmd_fix(quiet=False)
    out('')
    out('安装完成。以后开机和睡眠唤醒后会自动修复, 日志在 %s' % LOG_DIR)
    return rc


def cmd_uninstall():
    if not is_admin():
        out('[!] 需要管理员权限, 正在请求提权...')
        return 0 if elevate() else 1
    p = subprocess.run(['C:\\Windows\\System32\\schtasks.exe', '/delete', '/tn', TASK_NAME, '/f'],
                       capture_output=True, text=True, errors='replace', creationflags=0x08000000)
    out('[1/2] 计划任务: %s' % ('已删除' if p.returncode == 0 else '不存在或删除失败'))
    _sc('stop', SVC)
    rc, txt = _sc('delete', SVC)
    out('[2/2] 驱动服务: %s' % ('已删除' if rc == 0 else txt.strip()[:120]))
    return 0


def cmd_watch(interval=30):
    out('监控模式: 每 %d 秒检查一次, 功耗墙被改动就立刻恢复 (Ctrl+C 退出)' % interval)
    while True:
        try:
            if ensure_driver(verbose=False):
                cfg = load_cfg()
                mb = mchbar()
                msr = rdmsr(MSR_PKG_POWER_LIMIT)
                if mb and msr:
                    d_m = decode_pl(msr)
                    v = rdphys(mb + MMIO_PL_OFF, 8)
                    d_o = decode_pl(v) if v is not None else None
                    if d_o and (d_o['pl1_w'] < cfg['min_pl1_w'] or not d_o['lock']):
                        out('%s 检测到功耗墙异常, 重新应用...'
                            % datetime.datetime.now().strftime('%H:%M:%S'))
                        cmd_fix(quiet=True)
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
        print('用法: CpuPLFix.py [check|fix|save|install|uninstall|watch]')
        return 0

    fn = {'check': cmd_check, 'fix': cmd_fix, 'save': cmd_save,
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
