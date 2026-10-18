# -*- coding: utf-8 -*-
"""离线自检: 不碰硬件, 只验证 guard_apply 的分级/解除 与 fan_level_of 的回差。

跑法(工具自带的 python 就行):
    "%APPDATA%\\CpuPLFix\\runtime\\python.exe" dev\\selftest.py
默认测【已安装】的那份代码(%APPDATA%\\CpuPLFix\\CpuPLFix.py); 想测仓库里这份, 把
DEP 改成 os.path.dirname(os.path.dirname(os.path.abspath(__file__))) 即可。
"""
import importlib.util
import os
import sys

DEP = os.path.join(os.environ.get('APPDATA', ''), 'CpuPLFix')
spec = importlib.util.spec_from_file_location('cpf', os.path.join(DEP, 'CpuPLFix.py'))
cpf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpf)

cfg = cpf.load_cfg()
FAIL = []


def step(desc, got, want_sub):
    ok = (got is not None and want_sub in got) if want_sub else (got is None)
    print('  [%s] %-46s -> %s' % ('OK' if ok else '!!', desc, (got or '(无变化)')[:70]))
    if not ok:
        FAIL.append(desc)


print('=== guard_apply: 温度线 ===')
cpf._G['level'] = None
step('EC 90°C (低于预警 93)', cpf.guard_apply(cfg, 90, None, {0x89: 0, 0x8A: 0, 0x8B: 0}), None)
step('EC 93°C', cpf.guard_apply(cfg, 93, None, {}), '预警')
step('EC 94°C (同档不重复报)', cpf.guard_apply(cfg, 94, None, {}), None)
step('EC 96°C', cpf.guard_apply(cfg, 96, None, {}), '严重')
step('EC 105°C', cpf.guard_apply(cfg, 105, None, {}), '逃生')
step('EC 100°C (逃生->严重, 降档也是一次变化)', cpf.guard_apply(cfg, 100, None, {}), '严重')
step('EC 100°C (同上, 不重复报)', cpf.guard_apply(cfg, 100, None, {}), None)
step('EC 77°C -> 解除(77<=78)', cpf.guard_apply(cfg, 77, None, {}), '解除')

print('=== guard_apply: 固件标志线(0x89/0x8A/0x8B, 需连续两轮确认) ===')
cpf._G['level'] = None
cpf._G['trip_hits'] = 0
step('EC 60°C 第 1 轮读到 0x8A 置位 -> 只提示, 不动手',
     cpf.guard_apply(cfg, 60, None, {0x89: 0, 0x8A: 0x01, 0x8B: 0}), '再确认一轮')
step('第 2 轮仍读到 -> 最高级', cpf.guard_apply(cfg, 60, None, {0x89: 0, 0x8A: 0x01, 0x8B: 0}), '固件标志')
step('第 3 轮仍读到(同档不重复报)', cpf.guard_apply(cfg, 60, None, {0x89: 0, 0x8A: 0x01, 0x8B: 0}), None)
step('单轮毛刺(读到一轮后又没了) -> 不动作, 60<=78 于是解除',
     cpf.guard_apply(cfg, 60, None, {0x89: 0x80}), '解除')
step('EC 60°C 标志全零(已解除) -> 不再重复报', cpf.guard_apply(cfg, 60, None, {0x89: 0, 0x8A: 0, 0x8B: 0}), None)
cpf._G['level'] = None
cpf._G['trip_hits'] = 0
step('trip_flags=false 时标志被忽略', cpf.guard_apply({'guard': {'trip_flags': False}}, 60, None, {0x8A: 1}), None)

print('=== guard_apply: DTS 只做兜底 ===')
cpf._G['level'] = None
step('EC 70°C + DTS 99°C (EC 可信, 不该动)', cpf.guard_apply(cfg, 70, 99, {}), None)
step('EC 读不到 + DTS 99°C -> 严重(兜底)', cpf.guard_apply(cfg, None, 99, {}), '严重')
cpf._G['level'] = None
cpf.set_pl_override(None)

print('=== fan_level_of: 升降档与回差 ===')
curve = cpf.fan_cfg(cfg)['curve']
prev = None
seq = [(40, 1), (55, 2), (58, 4), (59, 4), (57, 4), (56.5, 4), (56.4, 2), (56, 2),
       (50, 2), (49, 2), (48.5, 2), (48.4, 1)]
for t, want in seq:
    lv, idx = cpf.fan_level_of(t, curve, prev)
    prev = idx
    ok = (lv == want)
    print('    [%s] %5s°C -> 档位 %-3s (段 %d)   期望 %s'
          % ('OK' if ok else '!!', t, lv, idx, want))
    if not ok:
        FAIL.append('fan_level_of @%s°C 期望档位 %s, 得到 %s' % (t, want, lv))

print('=== EC 解析函数 ===')
print('  ec_tripped({0x89:0,0x8A:0,0x80,0x8B:0}) =', cpf.ec_tripped({0x89: 0, 0x8A: 0x80, 0x8B: 0}))
print('  flags_desc =', cpf.flags_desc({0x89: 0, 0x8A: 0x80, 0x8B: None}))
print('  EC_TRIP_FLAGS =', ['0x%02X' % h for h in cpf.EC_TRIP_FLAGS])

print()
print('失败项: %d' % len(FAIL))
for f in FAIL:
    print('  - ' + f)
sys.exit(1 if FAIL else 0)
