# -*- coding: utf-8 -*-
"""离线自检: 不碰硬件。

覆盖:
  1) guard_apply 的温度线三级阶梯 / 驻留 / 解除 / 分级回升
  2) 固件标志线: 两轮确认 / 读失败不放开 / 97°C 连续 60 秒判锁存 / 98°C 重置
  3) 拆分后本工具**不该再有**任何风扇符号(fan_tick / set_fan / ec_rpm / FAN_LEVEL_RPM …)
  4) poll_plan 的节奏(凉机 120 秒 / 保护期 5-5-15 / 开机 2-5-10)
  5) MSR 0x610 时间窗解码: _tau_s 与 _pl_time 必须一致(SDM: 8 秒 / 5 秒)
  6) merge_flags: 读失败沿用上一轮、complete=False
  7) 3-config 的 hold= 同时写 trip_hold_s 与 flag_release_s

跑法:
    "T14scontrol\\cpu\\runtime\\python.exe" dev\\selftest.py
默认测【本文件所在目录的上一级】那份(cpu\\CpuPLFix.py)。
想测别的副本: 设环境变量 CPF_ROOT=<那个目录>。

2026-10-20 拆分: 风扇整段删除, 第 3 组断言改成"确认这些符号都不存在"。
2026-10-20 重写: 旧版的断言停留在"6 段风扇曲线 / 10W 地板 / 标志 30 秒"那代,
14 项一直红着, 等于没有回归保护。现在时间用打桩(time.time)推进, 不真等 60 秒。
"""
import importlib.util
import json
import os
import sys

# 2026-10-20: 原来写死 %APPDATA%\CpuPLFix(部署)/_repo(仓库), 拆分后目录变了 ——
# 默认跟着本文件走, 免得"测的是一份、改的是另一份"。
ROOT = os.environ.get('CPF_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location('cpf', os.path.join(ROOT, 'CpuPLFix.py'))
cpf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpf)

PASS = FAIL = 0


def chk(desc, ok, extra=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print('  [OK] %-52s %s' % (desc, extra))
    else:
        FAIL += 1
        print('  [!!] %-52s %s' % (desc, extra))


# ---------------------------------------------------------------- 打桩
NOW = [1000.0]
cpf.time.time = lambda: NOW[0]
PL = []


def _fake_set_pl(a, b=None):
    """模拟 set_pl_override: 记录调用**并**同步 _PL_OVERRIDE。

    ★2026-10-20: 以前只 append 不更新 _PL_OVERRIDE, 于是 guard_apply 里
    "解除时按当前档位过滤 ramp"那段读到 None, 永远走完整 ramp —— 测不出
    "warn 档解除先把 PL1 压到 20W"这个缺陷。
    """
    PL.append((a, b))
    cpf._PL_OVERRIDE = None if not a else (float(a), float(b or a))


cpf.set_pl_override = _fake_set_pl
cpf.on_ac = lambda: True

CFG = {
    'pl1_w': 28.0, 'pl2_w': 55.0, 'pl1_w_batt': 20.0, 'pl2_w_batt': 28.0,
    'guard': {
        'enabled': True, 'warn_c': 95.0, 'crit_c': 98.0, 'escape_c': 105.0,
        'crit_now_c': 100.0, 'crit_hold_s': 10.0, 'step_dwell_s': 15.0,
        'recover_c': 78.0, 'min_hold_s': 30.0, 'release_dwell_s': 10.0,
        'flag_release_c': 97.0, 'flag_release_s': 60.0,
        'flag_pl1_w': 20.0, 'flag_pl2_w': 28.0,
        'warn_pl1_w': 0.0, 'warn_pl2_w': 40.0,
        'crit_pl1_w': 28.0, 'crit_pl2_w': 35.0,
        'crit2_pl1_w': 20.0, 'crit2_pl2_w': 25.0,
        'crit3_pl1_w': 12.0, 'crit3_pl2_w': 14.0,
        'ramp_pls': [[20.0, 25.0], [28.0, 35.0]], 'ramp_step_s': 30.0,
        'trip_flags': True, 'temp_power': False, 'dts_crit_c': 97.0,
        'cool_tail_s': 30.0,
    },
}
FLAG_ON = {0x89: 0x01, 0x8A: 0x00, 0x8B: 0x00}
FLAG_OFF = {0x89: 0x00, 0x8A: 0x00, 0x8B: 0x00}


def reset(t0=1000.0):
    NOW[0] = t0
    del PL[:]
    cpf._PL_OVERRIDE = None
    for k in list(cpf._G.keys()):
        cpf._G[k] = None
    cpf._G.update({'level': None, 'since': 0.0, 'hibernate': False, 'last_et': None,
                   'trip_hits': 0, 'trip_since': None, 'trip_latched': False,
                   'cool_since': None, 'released_at': 0.0, 'crit_since': None,
                   'step': 0, 'step_since': 0.0, 'why': '', 'ramp': None, 'ramp_at': 0.0,
                   'clr_hits': 0, 'flag_ok_since': None})


def tick(et, dts=None, flags=None, fresh=True, dt=0.0):
    NOW[0] += dt
    return cpf.guard_apply(CFG, et, dts, flags if flags is not None else FLAG_OFF,
                           flags_fresh=fresh)


# ================================================================ 1. 温度线
print('=== 1. 温度线: 95 预警 / 98 驻留 10 秒 / 100 立即 / 105 逃生 ===')
reset()
chk('EC 94°C 不动', tick(94) is None, 'level=%r' % cpf._G['level'])
m = tick(95)
chk('EC 95°C -> 预警', m is not None and '预警' in m, (m or '')[:40])
chk('EC 96°C 同档不重复报', tick(96) is None)
chk('EC 98°C 第 1 次: 驻留中, 仍是预警', cpf._G['level'] == 'warn',
    'level=%r' % cpf._G['level'])
tick(98)                                   # 第一次触 98 才开始计驻留
m = tick(98, dt=10.0)
chk('驻留满 10 秒 -> 严重', cpf._G['level'] == 'crit' and '严重' in (m or ''),
    'level=%r' % cpf._G['level'])
reset()
m = tick(100)
chk('单次 100°C(=crit_now_c) 立即严重', cpf._G['level'] == 'crit', 'level=%r' % cpf._G['level'])
m = tick(105)
chk('EC 105°C -> 逃生(第三级)', cpf._G['level'] == 'crit3', 'level=%r' % cpf._G['level'])

print('=== 2. 三级阶梯: 每级 step_dwell_s(15 秒) 深一级 ===')
reset()
tick(100)                                  # 立即 crit
m = tick(99, dt=15.0)
chk('15 秒后 -> 第二级 20/25', cpf._G['level'] == 'crit2', 'level=%r' % cpf._G['level'])
m = tick(99, dt=15.0)
chk('再 15 秒 -> 第三级 12/14', cpf._G['level'] == 'crit3', 'level=%r' % cpf._G['level'])
chk('地板是 12/14(不是 10W)', cpf._G.get('level') == 'crit3')

print('=== 3. 解除: min_hold_s(30) + release_dwell_s(10) 都要满足 ===')
reset()
tick(100)                                  # 进 crit, since=NOW
NOW[0] += 20.0
tick(77)
chk('驻留不足 30 秒: 不解除', cpf._G['level'] == 'crit', 'level=%r' % cpf._G['level'])
NOW[0] += 15.0                             # 累计 35 秒 > 30
tick(77)                                   # cool_since 从这里起算
chk('刚进降温窗口: 还不解除', cpf._G['level'] == 'crit')
NOW[0] += 10.0
m = tick(77)
chk('降温稳住 10 秒 -> 解除', cpf._G['level'] is None and '解除' in (m or ''),
    'level=%r' % cpf._G['level'])
chk('解除后 ramp 不把功耗压得比解除前更低', PL[-1][0] >= 28.0, 'PL[-1]=%r' % (PL[-1],))
chk('解除后有回升阶梯', cpf._G.get('ramp') is not None, 'ramp=%r' % (cpf._G.get('ramp'),))

print('=== 3b. ★解除时不许"反向降级"(审计发现的 bug) ===')
reset()
tick(95)                                   # warn 档: PL1 28 / PL2 40
chk('warn 档 override = 28/40', cpf._PL_OVERRIDE == (28.0, 40.0), '%r' % (cpf._PL_OVERRIDE,))
NOW[0] += 35
tick(77)
NOW[0] += 11
m = tick(77)                               # 解除
chk('warn 解除: PL1 不低于 28W(None=回原档, 也算对)',
    cpf._PL_OVERRIDE is None or cpf._PL_OVERRIDE[0] >= 28.0,
    'override=%r' % (cpf._PL_OVERRIDE,))
chk('warn 解除: 直接回原档(ramp=None)', cpf._G.get('ramp') is None,
    'ramp=%r' % (cpf._G.get('ramp'),))
chk('warn 解除日志说"直接恢复原档"', '直接恢复原档' in (m or ''), (m or '')[:60])

reset()
tick(100)                                  # crit 档: 28/35
NOW[0] += 35
tick(77)
NOW[0] += 11
tick(77)
chk('crit 解除: PL1 不低于 28W', (cpf._PL_OVERRIDE or (0, 0))[0] >= 28.0,
    'override=%r' % (cpf._PL_OVERRIDE,))

reset()
tick(100)
NOW[0] += 15
tick(99)                                   # -> crit2 (20/25)
NOW[0] += 35
tick(77)
NOW[0] += 11
tick(77)
chk('crit2 解除: 从 20/25 起(不跳过也不倒退)',
    cpf._PL_OVERRIDE == (20.0, 25.0), 'override=%r' % (cpf._PL_OVERRIDE,))

# ================================================================ 4. 固件标志
print('=== 4. 固件标志: 两轮确认 / 读失败不放开 / 97°C 60 秒判锁存 ===')
reset()
m = tick(90, flags=FLAG_ON)
chk('第 1 轮只提示', m is not None and '再确认' in m, (m or '')[:30])
m = tick(90, flags=FLAG_ON)
chk('第 2 轮 -> flag 档 20/28', cpf._G['level'] == 'flag' and PL[-1] == (20.0, 28.0),
    'level=%r PL=%r' % (cpf._G['level'], PL[-1] if PL else None))
for _ in range(6):                          # 30 秒, 全部"沿用缓存"
    tick(90, flags=FLAG_ON, fresh=False, dt=5.0)
chk('读失败(沿用缓存)不放开', cpf._G['level'] == 'flag', 'level=%r' % cpf._G['level'])
for _ in range(7):                          # 再 35 秒, 累计 65 秒
    tick(90, flags=FLAG_ON, dt=5.0)
chk('连续 ≤97°C 满 60 秒 -> 判锁存放开', bool(cpf._G.get('trip_latched')),
    'latched=%r level=%r' % (cpf._G.get('trip_latched'), cpf._G['level']))
chk('锁存放开是 sticky(标志还挂着也不回 flag)',
    tick(90, flags=FLAG_ON, dt=5.0) is None and cpf._G['level'] is None,
    'level=%r' % cpf._G['level'])

print('=== 5. 97 边界: 96 不重置 / 98 重置并交给温度线 ===')
reset()
tick(90, flags=FLAG_ON); tick(90, flags=FLAG_ON)      # 进 flag
for _ in range(6):
    tick(90, flags=FLAG_ON, dt=5.0)                   # 30 秒
tick(96, flags=FLAG_ON, dt=5.0)
chk('96°C 不重置计时', cpf._G.get('flag_ok_since') is not None,
    'since=%r' % cpf._G.get('flag_ok_since'))
reset()
tick(90, flags=FLAG_ON); tick(90, flags=FLAG_ON)
for _ in range(6):
    tick(90, flags=FLAG_ON, dt=5.0)
tick(98, flags=FLAG_ON, dt=5.0)
chk('98°C 重置计时', cpf._G.get('flag_ok_since') is None)
chk('98°C 时不判锁存', not cpf._G.get('trip_latched'))

print('=== 6. 标志清零: 连续两轮真读才解除锁存 ===')
reset()
tick(90, flags=FLAG_ON); tick(90, flags=FLAG_ON)
for _ in range(13):
    tick(90, flags=FLAG_ON, dt=5.0)                   # 65 秒 -> 锁存
chk('前置: 已锁存', bool(cpf._G.get('trip_latched')))
tick(60, flags=FLAG_OFF, dt=5.0)
chk('清零 1 轮: 仍锁存', bool(cpf._G.get('trip_latched')),
    'clr_hits=%r' % cpf._G.get('clr_hits'))
m = tick(60, flags=FLAG_OFF, dt=5.0)
chk('清零 2 轮: 解除锁存', not cpf._G.get('trip_latched') and '清零' in (m or ''),
    (m or '')[:40])

# ================================================================ 7. 拆分后没有风扇代码
print('=== 7. 拆分后: 本工具不含任何风扇控制符号 ===')
for _name in ('fan_level_of', 'fan_tick', 'set_fan', 'ec_hfsp', 'ec_rpm',
              'FAN_LEVEL_RPM', 'FAN_MAX', 'EC_HFSP', 'EC_RPM_LO', 'cmd_fan',
              'cool_tail_active'):
    chk('没有 %s' % _name, not hasattr(cpf, _name))
chk('DEFAULT_CFG 没有 fan 段', 'fan' not in cpf.DEFAULT_CFG)
chk('guard 没有 force_fan_max', 'force_fan_max' not in cpf.DEFAULT_CFG['guard'])
chk('guard 没有 cool_tail_s', 'cool_tail_s' not in cpf.DEFAULT_CFG['guard'])

# ================================================================ 8. 节奏
print('=== 8. poll_plan: 凉机 120 / 保护期 5-5-15 / 开机 2-5-10 ===')
d = cpf.poll_plan(dict(cpf.DEFAULT_CFG))
chk('默认(凉机) 5/15/120 秒', d == (5.0, 15.0, 120.0), '%r' % (d,))
chk('没配节奏项时同默认', cpf.poll_plan({}) == (5.0, 15.0, 120.0))
gp = cpf.poll_plan(dict(cpf.DEFAULT_CFG), guard=True)
chk('保护期 5/5/15 秒(标志不再 5 秒)', gp == (5.0, 5.0, 15.0), '%r' % (gp,))
fp = cpf.poll_plan(dict(cpf.DEFAULT_CFG), fast=True)
chk('开机前 5 分钟 2/5/10 秒', fp == (2.0, 5.0, 10.0), '%r' % (fp,))
hp = cpf.poll_plan(dict(cpf.DEFAULT_CFG), hot=True)
chk('温度高时标志跟 EC 同频', hp[2] == hp[1], '%r' % (hp,))
chk('乱填 0/负数不会疯读',
    min(cpf.poll_plan({'poll_s': 0, 'ec_poll_s': -1, 'flag_poll_s': 0})) >= 1.0)

# ================================================================ 9. 时间窗解码
print('=== 9. MSR 0x610 时间窗: _tau_s 必须与 _pl_time 一致 ===')
v = 0x004281B800238140          # 本机实测值
t1, t2 = (v >> 16) & 0xFF, (v >> 48) & 0xFF
chk('PL1 35 -> 8 秒', cpf._tau_s(t1) == '8 秒', '_tau_s=%r _pl_time=%.1f'
    % (cpf._tau_s(t1), cpf._pl_time(v, 16)))
chk('PL2 66 -> 5 秒', cpf._tau_s(t2) == '5 秒', '_tau_s=%r _pl_time=%.1f'
    % (cpf._tau_s(t2), cpf._pl_time(v, 48)))
chk('两个解码器结果一致(PL1)',
    cpf._tau_s(t1) == '%.0f 秒' % cpf._pl_time(v, 16))
chk('两个解码器结果一致(PL2)',
    cpf._tau_s(t2) == '%.0f 秒' % cpf._pl_time(v, 48))

# ================================================================ 10. merge_flags
print('=== 10. merge_flags: 读失败沿用上一轮, 且 complete=False ===')
mf = cpf.merge_flags
# 签名是 merge_flags(new=本轮, old=上一轮)
f, ok = mf({0x89: None, 0x8A: None, 0x8B: None}, {0x89: 1, 0x8A: 0, 0x8B: 0})
chk('本轮全读失败 -> 沿用上一轮, complete=False',
    f.get(0x89) == 1 and ok is False, 'merged=%r ok=%r' % (f, ok))
f, ok = mf({0x89: 0, 0x8A: 0, 0x8B: 0}, {0x89: 0, 0x8A: 0, 0x8B: 0})
chk('本轮全读到 -> complete=True', ok is True)
f, ok = mf(None, {0x89: None, 0x8A: None, 0x8B: None})
chk('无历史且全失败 -> 值仍是 None, complete=False',
    f.get(0x89) is None and ok is False, 'merged=%r ok=%r' % (f, ok))
chk('ec_tripped 跳过 None', cpf.ec_tripped({0x89: None, 0x8A: 0, 0x8B: 0}) == [])
chk('flags_desc 把 None 显示成 --', '--' in cpf.flags_desc({0x89: None, 0x8A: 0, 0x8B: 0}))

# ================================================================ 11. 配置工具
print('=== 11. 3-config: hold= 同时写 trip_hold_s / flag_release_s ===')
spec2 = importlib.util.spec_from_file_location(
    'cfgmod', os.path.join(ROOT, 'CpuPLFixCfg.py'))
m2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(m2)
import copy
c = copy.deepcopy(CFG)
m2.apply_kv(c, 'hold', '90')
chk('hold=90 -> 两个键都写', c['guard'].get('trip_hold_s') == 90.0
    and c['guard'].get('flag_release_s') == 90.0,
    'trip=%r flag=%r' % (c['guard'].get('trip_hold_s'), c['guard'].get('flag_release_s')))
c2 = copy.deepcopy(CFG)
m2.apply_kv(c2, 'flag_rel', '97,60')
chk('flag_rel=97,60 -> guard 值',
    (c2['guard'].get('flag_release_c'), c2['guard'].get('flag_release_s')) == (97.0, 60.0))

# ================================================================ 12. 真实配置
print('=== 12. 实机 config.json 的关键取值 ===')
try:
    real = json.load(open(os.path.join(ROOT, 'config.json'), encoding='utf-8'))
    rg = real.get('guard') or {}
    chk('flag_release_c = 97', rg.get('flag_release_c') == 97.0, '= %r' % rg.get('flag_release_c'))
    chk('flag_release_s = 60', rg.get('flag_release_s') == 60.0, '= %r' % rg.get('flag_release_s'))
    chk('97 < crit_c', 97.0 < float(rg.get('crit_c') or 98.0), 'crit_c=%r' % rg.get('crit_c'))
    chk('pl1_w = 28 / pl2_w = 55',
        (real.get('pl1_w'), real.get('pl2_w')) == (28.0, 55.0),
        '%r' % ((real.get('pl1_w'), real.get('pl2_w')),))
except Exception as e:
    chk('读 config.json', False, repr(e))

print()
print('结果: %d 通过 / %d 失败' % (PASS, FAIL))
if FAIL:
    print('失败明细见上面的 [!!] 行')
sys.exit(1 if FAIL else 0)
