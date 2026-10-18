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


g = cfg['guard']
w, c, e, r = g['warn_c'], g['crit_c'], g['escape_c'], g['recover_c']

print('=== guard_apply: 温度线（阈值直接读 config：预警 %g / 严重 %g / 逃生 %g / 解除 %g） ===' % (w, c, e, r))
cpf._G['level'] = None
step('EC %g°C (低于预警)' % (w - 1), cpf.guard_apply(cfg, w - 1, None, {0x89: 0, 0x8A: 0, 0x8B: 0}), None)
step('EC %g°C (到预警)' % w, cpf.guard_apply(cfg, w, None, {}), '预警')
step('EC %g°C (同档不重复报)' % (w + 1), cpf.guard_apply(cfg, w + 1, None, {}), None)
step('EC %g°C (到严重)' % c, cpf.guard_apply(cfg, c, None, {}), '严重')
step('EC %g°C (到逃生)' % e, cpf.guard_apply(cfg, e, None, {}), '逃生')
step('EC %g°C (逃生->严重, 降档也是一次变化)' % (e - 5), cpf.guard_apply(cfg, e - 5, None, {}), '严重')
step('EC %g°C (同上, 不重复报)' % (e - 5), cpf.guard_apply(cfg, e - 5, None, {}), None)
step('EC %g°C -> 解除(%g<=%g)' % (r - 1, r - 1, r), cpf.guard_apply(cfg, r - 1, None, {}), '解除')

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

print('=== 轮询节奏 poll_plan: 少打扰 EC ===')


def chk(desc, ok, extra=''):
    print('  [%s] %-46s %s' % ('OK' if ok else '!!', desc, extra))
    if not ok:
        FAIL.append(desc)


d = cpf.poll_plan(dict(cpf.DEFAULT_CFG))
chk('默认 主循环/EC/标志 = %.0f/%.0f/%.0f 秒' % d, d == (5.0, 15.0, 30.0), '(DEFAULT_CFG)')
chk('没配节奏项时用默认值', cpf.poll_plan({}) == (5.0, 15.0, 30.0))
r = cpf.poll_plan(cfg)
chk('部署 config 的节奏 = %.0f/%.0f/%.0f 秒' % r, r[1] >= 10.0,
    'EC 读间隔应 >= 10 秒(降频确实生效)')
chk('开机前 5 分钟自动加密(EC<=5 秒)', cpf.poll_plan(cfg, fast=True)[1] <= 5.0)
gp = cpf.poll_plan(cfg, guard=True)
chk('保护生效: EC 读提到跟主循环同频', gp[1] == gp[0], '%s' % (gp,))
hp = cpf.poll_plan(cfg, hot=True)
chk('温度 > flag_hot_c: 标志跟 EC 同频', hp[2] == hp[1], '%s' % (hp,))
chk('乱填 0/负数也不会变成疯读',
    min(cpf.poll_plan({'poll_s': 0, 'ec_poll_s': -1, 'flag_poll_s': 0})) >= 1.0)
chk('flag_hot_c 读得到(默认 85)', abs(cpf.flag_hot_c({}) - 85.0) < 0.01)
import inspect
chk('tick 支持 ec/read_flags: %s' % str(inspect.signature(cpf.tick)),
    'ec' in str(inspect.signature(cpf.tick)) and 'read_flags' in str(inspect.signature(cpf.tick)))

print('=== 标志缓存不当新证据(flags_fresh=False) ===')
cpf._G['level'] = None
cpf._G['trip_hits'] = 0
cpf.guard_apply(cfg, 60, None, {0x8A: 1})                        # 真读第 1 轮
cpf.guard_apply(cfg, 60, None, {0x8A: 1}, flags_fresh=False)     # 缓存轮
cpf.guard_apply(cfg, 60, None, {0x8A: 1}, flags_fresh=False)     # 缓存轮
chk('缓存值反复传不会自己凑成"连续两轮"', cpf._G['level'] is None,
    'trip_hits=%s level=%s' % (cpf._G.get('trip_hits'), cpf._G['level']))
chk('第 2 次真读才确认', '固件标志' in (cpf.guard_apply(cfg, 60, None, {0x8A: 1}) or ''))
cpf._G['level'] = None
cpf._G['trip_hits'] = 0
cpf.set_pl_override(None)

print()
print('失败项: %d' % len(FAIL))
for f in FAIL:
    print('  - ' + f)
sys.exit(1 if FAIL else 0)
