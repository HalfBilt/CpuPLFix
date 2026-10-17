# -*- coding: utf-8 -*-
"""CpuPLFixSvc —— 把修复逻辑做成 Windows 服务, 随系统启动, 常驻看守。

为什么要有服务(而不是只靠计划任务):
  1. 服务由 SCM 在开机早期拉起, 不需要用户登录, 也不受任务计划程序调度的抖动影响
  2. 常驻进程可以持续盯着 —— 睡眠唤醒后固件若再次压低功耗墙, 几秒内就能补锁
  3. 0.4GHz 状态下 Windows 自身启动极慢, 服务比计划任务更早拿到执行机会

用法(由 CpuPLFix.py install 自动注册, 一般不需要手动运行):
    pythonw.exe CpuPLFixSvc.py --service
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes as w

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import importlib.util

_spec = importlib.util.spec_from_file_location('cpf', os.path.join(BASE, 'CpuPLFix.py'))
cpf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpf)

SERVICE_NAME = 'CpuPLFixSvc'
SERVICE_DISPLAY = 'CpuPLFix 功耗墙看守'

SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_START_PENDING = 2
SERVICE_STOPPED = 1
SERVICE_RUNNING = 4
SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_POWEREVENT = 0x00000040
SERVICE_CONTROL_STOP = 1
SERVICE_CONTROL_INTERROGATE = 4
SERVICE_CONTROL_POWEREVENT = 0x0000000D

# SERVICE_CONTROL_POWEREVENT 的 dwEventType, 与窗口消息 WM_POWERBROADCAST 的 wParam 同一套值
PBT_APMRESUMECRITICAL = 0x0006      # 从"未提前通知的休眠"恢复
PBT_APMRESUMESUSPEND = 0x0007       # 用户操作唤醒(按键盘/开盖/按电源键)
PBT_APMRESUMEAUTOMATIC = 0x0012     # 自动唤醒(定时器/网络事件等)
RESUME_EVENTS = (PBT_APMRESUMECRITICAL, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC)

POLL_FAST = 2.0      # 开机/唤醒后前 5 分钟的轮询间隔(秒)
POLL_SLOW = 5.0      # 稳定期的轮询间隔(秒)
POLL_FAST_WINDOW = 300.0

adv = ctypes.WinDLL('advapi32', use_last_error=True)


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [('dwServiceType', w.DWORD),
                ('dwCurrentState', w.DWORD),
                ('dwControlsAccepted', w.DWORD),
                ('dwWin32ExitCode', w.DWORD),
                ('dwServiceSpecificExitCode', w.DWORD),
                ('dwCheckPoint', w.DWORD),
                ('dwWaitHint', w.DWORD)]


HANDLER = ctypes.WINFUNCTYPE(w.DWORD, w.DWORD, w.DWORD, ctypes.c_void_p, ctypes.c_void_p)
SERVICE_MAIN_FUNC = ctypes.WINFUNCTYPE(None, w.DWORD, ctypes.POINTER(w.LPWSTR))


class SERVICE_TABLE_ENTRY(ctypes.Structure):
    _fields_ = [('lpServiceName', w.LPWSTR),
                ('lpServiceProc', SERVICE_MAIN_FUNC)]


adv.RegisterServiceCtrlHandlerExW.restype = w.HANDLE
adv.RegisterServiceCtrlHandlerExW.argtypes = [w.LPCWSTR, HANDLER, ctypes.c_void_p]
adv.SetServiceStatus.argtypes = [w.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
adv.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRY)]

_stop = threading.Event()
_wake = threading.Event()          # 收到"唤醒"电源事件时置位, 让轮询立刻返回
_resume = [0]                      # 最近一次电源事件类型(handler 里只记值, 不写日志)
_status_handle = None
_status = SERVICE_STATUS()
_handler_ref = None          # 必须保持引用, 否则回调会被回收


def _set_status(state, accepted=0, hint=0, cp=0):
    global _status
    _status.dwServiceType = SERVICE_WIN32_OWN_PROCESS
    _status.dwCurrentState = state
    _status.dwControlsAccepted = accepted
    _status.dwWin32ExitCode = 0
    _status.dwServiceSpecificExitCode = 0
    _status.dwCheckPoint = cp
    _status.dwWaitHint = hint
    if _status_handle:
        adv.SetServiceStatus(_status_handle, ctypes.byref(_status))


def _handler(control, event_type, event_data, context):
    if control == SERVICE_CONTROL_STOP:
        _stop.set()
        _wake.set()
        return 0
    if control == SERVICE_CONTROL_INTERROGATE:
        _set_status(_status.dwCurrentState, SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_POWEREVENT)
        return 0
    if control == SERVICE_CONTROL_POWEREVENT:
        # 睡眠 / 休眠恢复时 SCM 会送这个事件过来。这里只置标志, 日志和修复都交给
        # 工作线程去做 —— 服务控制回调里不能有阻塞操作。
        if event_type in RESUME_EVENTS:
            _resume[0] = event_type
            _wake.set()
        return 0
    return 0


def _log(msg):
    try:
        cpf.out(msg)
        cpf.flush_log('svc')
    except Exception:
        pass


def _pbt_name(t):
    return {PBT_APMRESUMECRITICAL: '休眠恢复-未通知',
            PBT_APMRESUMESUSPEND: '用户操作唤醒',
            PBT_APMRESUMEAUTOMATIC: '自动唤醒'}.get(t, '未知')


def _check_once(cfg, quiet=True):
    """返回 True 表示状态正常; 否则修一次"""
    if not cpf.ensure_driver(verbose=False):
        return False
    mb = cpf.mchbar()
    msr = cpf.rdmsr(cpf.MSR_PKG_POWER_LIMIT)
    if not mb or msr is None:
        cpf._handle = None          # 句柄可能已失效, 下次重新打开
        return False
    d_m = cpf.decode_pl(msr)
    v = cpf.rdphys(mb + cpf.MMIO_PL_OFF, 8)
    if v is None:
        cpf._handle = None
        return False
    d_o = cpf.decode_pl(v)
    pl1, _ = cpf._target_pl(cfg, d_m)
    ok = (d_o['lock'] == 1) and (d_o['pl1_w'] >= pl1 - 0.5)
    if ok:
        return True
    _log('[*] 检测到功耗墙异常: MMIO PL1=%.2fW lock=%d (目标 %.2fW), 重新锁定'
         % (d_o['pl1_w'], d_o['lock'], pl1))
    cpf.cmd_fix(quiet=True)
    cpf.flush_log('svc')
    return False


def _worker():
    # 开机早期驱动可能还没准备好, 最多等 3 分钟
    t_entry = cpf.uptime()
    ready = False
    for i in range(180):
        if _stop.is_set():
            return
        try:
            if cpf.ensure_driver(verbose=False):
                ready = True
                break
        except Exception:
            pass
        time.sleep(1.0)
    if not ready:
        _log('[X] 服务进程在系统运行 %.1f 秒时才启动, 之后 180 秒仍无法加载驱动, 放弃'
             % t_entry)
        return

    cfg = cpf.load_cfg()
    _log('[*] 服务就绪: 进程启动于系统运行 %.1f 秒, 驱动就绪于 %.1f 秒 (耗时 %.1f 秒), '
         '目标 PL1=%.1fW PL2=%.1fW'
         % (t_entry, cpf.uptime(), cpf.uptime() - t_entry,
            float(cfg['pl1_w'] or 0), float(cfg['pl2_w'] or 0)))
    _log('[*] 已订阅电源事件(SERVICE_ACCEPT_POWEREVENT): 睡眠/休眠恢复后 1 秒内响应; '
         '另有 %.0f 秒轮询兜底' % POLL_FAST)

    t0 = time.time()
    last = time.time()
    last_cfg = time.time()
    while not _stop.is_set():
        # 每分钟重读一次 config.json, 改完不用重启服务就生效
        if time.time() - last_cfg > 60:
            last_cfg = time.time()
            try:
                cfg = cpf.load_cfg()
            except Exception:
                pass
        try:
            _check_once(cfg)
        except Exception as e:
            _log('[*] 检查异常: %s' % e)

        # 前 5 分钟每 2 秒盯一次(开机阶段最容易出问题), 之后每 5 秒一次。
        # 每次检查只是几个 IOCTL, 开销可以忽略。
        interval = POLL_FAST if (time.time() - t0) < POLL_FAST_WINDOW else POLL_SLOW
        woke = _wake.wait(interval)
        if _stop.is_set():
            break

        now = time.time()
        gap = now - last
        last = now
        if woke:
            _wake.clear()
            # 路径一: SCM 送来的电源事件 —— 唤醒后不到 1 秒就能开始检查
            _log('[*] 收到电源事件 0x%04X (%s), 判定刚从睡眠/休眠恢复, 立即重新检查'
                 % (_resume[0], _pbt_name(_resume[0])))
        elif gap > interval + 20:
            # 路径二: 兜底 —— 时钟跳了一大截, 说明进程被挂起过(没收到电源事件时)
            _log('[*] 检测到 %.0f 秒挂起间隔, 判定刚从睡眠/休眠恢复, 立即重新检查' % gap)


def _service_main(argc, argv):
    global _status_handle, _handler_ref
    _handler_ref = HANDLER(_handler)
    _status_handle = adv.RegisterServiceCtrlHandlerExW(SERVICE_NAME, _handler_ref, None)
    _set_status(SERVICE_START_PENDING, 0, 5000, 1)
    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    _set_status(SERVICE_RUNNING, SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_POWEREVENT, 0, 0)
    _stop.wait()
    _set_status(SERVICE_STOPPED, 0, 0, 0)


def run_service():
    table = (SERVICE_TABLE_ENTRY * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = SERVICE_MAIN_FUNC(_service_main)
    table[1].lpServiceName = None
    table[1].lpServiceProc = SERVICE_MAIN_FUNC()
    if not adv.StartServiceCtrlDispatcherW(table):
        raise OSError('StartServiceCtrlDispatcher 失败: %d' % ctypes.get_last_error())


if __name__ == '__main__':
    if '--service' in sys.argv:
        run_service()
    else:
        print('这是 Windows 服务入口, 请用 CpuPLFix.py install 注册, 不要直接运行。')
        print('手动调试可以先跑: python CpuPLFix.py boot')
