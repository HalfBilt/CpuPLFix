# -*- coding: utf-8 -*-
"""EC 扫描 v2: 空闲 vs 满载, 找固件真正盯的温度/过热标志。

v1 的教训: 和服务同时做 EC 端口事务 -> 互相插队 -> 读出来是别人的应答(0x78 读成 46,
实际 82), 而且整段数据都不可信。v2 的应对:
  * 每个事务都过同一把跨进程互斥体(CpuPLFix._ec_lock, 服务也在用)
  * 关键寄存器用 ec_read_ok(连读两次一致才认), 读之前有固定等待(见 CpuPLFix.ec_read)
  * 全量 dump 连做两遍并互相比对: 不一致的寄存器直接标出来, 告诉我们数据可不可信
  * 安全阀: 标志位(校验后)出现 或 0x78(校验后)>=97 -> 立刻撤负载

怎么跑(要管理员):
    "%APPDATA%\\CpuPLFix\\runtime\\python.exe" dev\\ec_scan.py
只读 + 造 CPU 负载; 风扇/过热保护仍由看守服务负责(它正常运行)。不查 WMI(本机会挂死)。
结果写同目录 ec_scan_out.txt。

已知残留问题: 负载很重时 EC 忙不过来, 个别读数仍可能拿到残留字节(服务的稀疏读法实测没事,
本脚本的连读两遍比对能标出不一致)。所以它适合"看趋势", 不适合当精确标定。
"""
import importlib.util
import os
import threading
import time
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

DEP = os.path.join(os.environ.get('APPDATA', ''), 'CpuPLFix')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ec_scan_out.txt')
LO, HI = 0x60, 0xC8
KEY = (0x78, 0x89, 0x8A, 0x8B)
LOAD_SEC = 240

_lines = []


def say(s=''):
    print(s)
    _lines.append(str(s))


def save():
    try:
        with open(OUT, 'w', encoding='utf-8') as f:
            f.write('\n'.join(_lines) + '\n')
    except Exception as e:
        print('write %s failed: %s' % (OUT, e))


spec = importlib.util.spec_from_file_location('cpf', DEP + r'\CpuPLFix.py')
cpf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpf)

say('==== EC 扫描 v2  %s ====' % time.strftime('%Y-%m-%d %H:%M:%S'))
if not cpf.ensure_driver(verbose=False):
    say('[X] 驱动加载失败 —— 需要管理员权限')
    save()
    sys.exit(1)

say('互斥体: %s' % ('可用(与服务共用同一把)' if cpf._ec_lock(500) else '拿不到 —— 结果可能不可信!'))
cpf._ec_unlock(True)
g = cpf.guard_cfg(cpf.load_cfg())
say('guard: enabled=%s warn=%.0f crit=%.0f escape=%.0f recover=%.0f trip_flags=%s'
    % (g['enabled'], g['warn_c'], g['crit_c'], g['escape_c'], g['recover_c'], g.get('trip_flags')))


def dump2():
    """连做两遍全量 dump, 返回 (第一遍, 第二遍) —— 比对用"""
    t0 = time.time()
    a = {}
    for off in range(LO, HI):
        v = cpf.ec_read(off)
        if v is not None:
            a[off] = v
    t1 = time.time()
    b = {}
    for off in range(LO, HI):
        v = cpf.ec_read(off)
        if v is not None:
            b[off] = v
    t2 = time.time()
    return a, b, (t1 - t0) * 1000.0 / max(1, len(a)), (t2 - t1) * 1000.0 / max(1, len(b))


def show(d, title):
    say('--- %s ---' % title)
    for base in range(LO, HI, 8):
        cells = []
        for off in range(base, base + 8):
            v = d.get(off)
            cells.append('%02X:%-3s' % (off, '--' if v is None else ('%02X' % v)))
        say('  ' + ' '.join(cells))


def pl1_now():
    msr = cpf.rdmsr(cpf.MSR_PKG_POWER_LIMIT)
    return cpf.decode_pl(msr)['pl1_w'] if msr is not None else None


say('')
idle, idle2, ms1, ms2 = dump2()
show(idle, '空闲 0x%02X~0x%02X (第一遍)' % (LO, HI - 1))
diff_idle = [k for k in sorted(idle) if idle.get(k) != idle2.get(k)]
say('  两遍比对: %d 个寄存器不一致 %s   (每读 %.1f / %.1f ms)'
    % (len(diff_idle), ['0x%02X' % k for k in diff_idle[:16]], ms1, ms2))
et0 = cpf.ec_temp()
fl0 = cpf.ec_trip_flags()
say('  空闲(校验后): 0x78=%s°C  RPM=%s  PL1=%sW  标志 %s'
    % (et0, cpf.ec_rpm(), pl1_now(), cpf.flags_desc(fl0)))

say('')
say('=== 满载: 每 4 秒一采 (关键寄存器都做一致性校验) ===')
stop = [False]


def burn():
    x = 1
    while not stop[0]:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF


ths = [threading.Thread(target=burn, daemon=True) for _ in range(max(4, os.cpu_count() or 4))]
for t in ths:
    t.start()

peak = dict(idle)
peak2 = dict(idle2)
series = []
reason = '跑满 %d 秒' % LOAD_SEC
t0 = time.time()
hot = hot2 = None
rounds = 0
while time.time() - t0 < LOAD_SEC:
    rounds += 1
    et = cpf.ec_temp()
    fl = cpf.ec_trip_flags()
    hit = cpf.ec_tripped(fl)
    hf = cpf.ec_hfsp()
    series.append((int(time.time() - t0), et, fl.get(0x89), fl.get(0x8A), fl.get(0x8B)))
    say('  t=%3ds 0x78=%-4s 0x89=%-4s 0x8A=%-4s 0x8B=%-4s | PL1=%-6s HFSP=%s RPM=%s'
        % (int(time.time() - t0), et, fl.get(0x89), fl.get(0x8A), fl.get(0x8B),
           pl1_now(), (cpf.fan_name(hf) if hf is not None else '--'), cpf.ec_rpm()))
    if rounds % 3 == 0:                     # 每 3 轮做一次全量 dump
        a, b, ma, mb = dump2()
        for k in a:
            if a[k] > peak.get(k, -1):
                peak[k] = a[k]
            if b.get(k, -1) > peak2.get(k, -1):
                peak2[k] = b[k]
        nd = len([k for k in a if a.get(k) != b.get(k)])
        say('     [全量 dump: 两遍不一致 %d 个, 每读 %.1f/%.1f ms]' % (nd, ma, mb))
    if hit:
        reason = '固件过热标志置位! ' + ', '.join('0x%02X=0x%02X' % (h, v) for h, v in hit)
        break
    if et is not None and et >= 97:
        reason = 'EC 0x78 = %d°C (触到安全阀 97)' % et
        break
    time.sleep(4)

say('  → 撤负载原因: %s' % reason)
stop[0] = True
time.sleep(0.5)
hot, hot2, mh1, mh2 = dump2()
show(hot, '撤负载那一刻的 EC 全貌')
say('  两遍不一致: %d 个 %s'
    % (len([k for k in hot if hot.get(k) != hot2.get(k)]),
       ['0x%02X' % k for k in sorted(hot) if hot.get(k) != hot2.get(k)][:16]))

say('')
say('=== 降温观察 ===')
for i in range(4):
    say('  0x78=%-4s 标志 %s  PL1=%sW' % (cpf.ec_temp(), cpf.flags_desc(cpf.ec_trip_flags()), pl1_now()))
    time.sleep(3)

say('')
say('=== 峰值 vs 空闲: 变化最大的 20 个寄存器(两遍 dump 取并集) ===')
mx = {k: max(peak.get(k, 0), peak2.get(k, 0), hot.get(k, 0) if hot else 0) for k in range(LO, HI)}
diffs = sorted(((mx[k] - idle.get(k, 0), k) for k in sorted(mx)), reverse=True)[:20]
for dv, k in diffs:
    say('  0x%02X: idle=%-4s 峰值=%-4s 变化 %+d' % (k, idle.get(k), mx[k], dv))

say('')
say('=== 峰值 > 99 (0x63) 的寄存器 —— 能表达"更热"的候选 ===')
ov = [k for k in sorted(mx) if mx[k] > 99]
if ov:
    for k in ov:
        say('  0x%02X: idle=%-4s 峰值=%d' % (k, idle.get(k), mx[k]))
else:
    say('  (没有)' )

say('')
say('=== EC 0x78 时间序列(校验后) ===')
say('  ' + ' '.join(str(s[1]) for s in series))
vals = [s[1] for s in series if s[1] is not None]
if vals:
    say('  峰值 %d°C, 最低 %d°C' % (max(vals), min(vals)))
say('')
say('==== 扫描结束 ====')
save()
