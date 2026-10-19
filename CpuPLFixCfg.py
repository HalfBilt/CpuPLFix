# -*- coding: utf-8 -*-
"""CpuPLFix 命令行配置工具 —— 只改 config.json, 不碰硬件。

    CpuPLFixCfg.py             看当前配置
    CpuPLFixCfg.py ac=40,55    改插电 PL1/PL2
    CpuPLFixCfg.py fan=56,79,5 改风扇曲线边界 + 回差
    CpuPLFixCfg.py hold=45 改固件标志锁存判定秒数（0=关掉这条出口）
    CpuPLFixCfg.py dwell=60 降温降档最短驻留秒数（0=关）
    CpuPLFixCfg.py flag_pl=20,28 标志置位但温度正常时的 PL1,PL2（默认=预警档，不砍到 10W）
    CpuPLFixCfg.py gmin=30 保护最短驻留秒数 / rel=10 解除驻留秒数
    CpuPLFixCfg.py boost=70,20,64 负载前馈：占用>=70% 满 20 秒 -> 至少满速
    CpuPLFixCfg.py boost_hold=30 负载落下后再吹 30 秒
    CpuPLFixCfg.py fgap=10 非紧急风扇写入的最小间隔（只压降档，秒）
    CpuPLFixCfg.py fan_dts=92 DTS 前馈：核心温度到 92°C 立刻拉满风扇
    CpuPLFixCfg.py flag_slow=120 凉机时固件标志读的间隔（秒）
    CpuPLFixCfg.py cool_tail=30 退出过热保护后继续满速 30 秒
    CpuPLFixCfg.py undo        撤销上一次保存
    CpuPLFixCfg.py raw         打印完整 JSON

保存后由看守服务在 60 秒内自动重读, 不用重启、不用管理员。
"""
import io
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, 'config.json')
BAK = CFG + '.bak'

FAN_RPM = {1: 2480, 2: 2610, 3: 2720, 4: 3190, 5: 3185, 6: 3490, 7: 3880, 64: 5000}


# ---------------------------------------------------------------- 读写
def load(path=CFG):
    with io.open(path, encoding='utf-8') as f:
        return json.load(f)


def atomic_write(path, text):
    tmp = path + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def dump(cfg):
    return json.dumps(cfg, indent=2, ensure_ascii=False) + '\n'


def save(cfg):
    old = None
    try:
        with io.open(CFG, encoding='utf-8') as f:
            old = f.read()
    except OSError:
        pass
    text = dump(cfg)
    if old and old.strip() and old != text:
        atomic_write(BAK, old)      # 只留一版, 用于 undo
    atomic_write(CFG, text)


# ---------------------------------------------------------------- 显示
def fan_desc(cfg):
    f = cfg['fan']
    curve, hyst = f['curve'], f['hyst']
    parts = []
    for i, (hi, lv) in enumerate(curve):
        lo = 0 if i == 0 else curve[i - 1][0]
        lvname = '满速' if lv >= 64 else ('%d档' % lv)
        if i == 0:
            parts.append('<=%dC %s' % (hi - 1, lvname))
        elif i == len(curve) - 1:
            parts.append('>=%dC %s' % (lo, lvname))
        else:
            parts.append('%d~%dC %s' % (lo, hi - 1, lvname))
    return ' | '.join(parts) + ' | 回差 %g' % hyst


def show(cfg):
    g = cfg['guard']
    onoff = lambda b: '开' if b else '关'
    print()
    print('CpuPLFix 配置  %s' % CFG)
    print('服务每 60 秒自动重读；改完即生效（切 mmio_mode 要重启）')
    print()
    print('【功耗墙】插电 %g/%g W   电池 %s'
          % (cfg['pl1_w'], cfg['pl2_w'],
             '跟随插电' if not cfg['pl1_w_batt'] else '%g/%g W' % (cfg['pl1_w_batt'], cfg['pl2_w_batt'])))
    print('  ac=%g,%g         插电 PL1,PL2（0 = 自动）' % (cfg['pl1_w'], cfg['pl2_w']))
    print('  batt=%g,%g       电池 PL1,PL2（0,0 = 跟随插电）' % (cfg['pl1_w_batt'], cfg['pl2_w_batt']))
    print('  time=%d,%d         PL1,PL2 时间窗（秒）' % (cfg['pl1_time'], cfg['pl2_time']))
    print('  min_pl1=%g        自动模式兜底下限（W）' % cfg['min_pl1_w'])
    print('  mmio=%s        disable=清零锁死固件侧(推荐) / value=写值再锁'
          % cfg['mmio_mode'])
    print('  lock=%s          锁定 MMIO' % ('on' if cfg['lock_mmio'] else 'off'))
    print('  poll=%g,%g,%g      主循环/EC/固件标志（秒）'
          % (cfg['poll_s'], cfg['ec_poll_s'], cfg['flag_poll_s']))
    print('  flag_slow=%g       凉机(EC < flag_hot)时标志读的间隔（秒，少打扰 EC）'
          % cfg.get('flag_poll_slow_s', 120.0))
    print()
    print('【风扇】%s' % fan_desc(cfg))
    print('  fan=56,79,5      段边界 + 回差（按你机器的段数给边界）')
    print('  lv=%s           每段档位（1~7，64=满速）'
          % ','.join(str(r[1]) for r in cfg['fan']['curve']))
    print('  mode=%s        curve / max / level / ec' % cfg['fan']['mode'])
    print('  level=%s           mode=level 时用' % cfg['fan']['level'])
    print('  hyst=%g            降档回差（C）' % cfg['fan']['hyst'])
    print('  dwell=%g           降温降档最短驻留（秒，0=关）'
          % cfg['fan'].get('min_dwell_s', 60.0))
    print('  boost=%g,%g,%g      负载前馈：占用>=x%% 满 y 秒 -> 至少 z 档（64=满速；x=0 关）'
          % (cfg['fan'].get('load_boost_pct', 70.0), cfg['fan'].get('load_boost_s', 20.0),
             cfg['fan'].get('load_boost_level', 64)))
    print('  boost_hold=%g       负载落下后再吹这么久（秒）'
          % cfg['fan'].get('load_hold_s', 30.0))
    print('  fan_dts=%g         DTS(核心温度)到它就立刻拉满（0=关；比 EC 0x78 快）'
          % cfg['fan'].get('dts_max_c', 92.0))
    print('  fgap=%g            非紧急风扇写入的最小间隔（只压降档，秒，0=关）'
          % cfg['fan'].get('min_write_gap_s', 10.0))
    print()
    print('【过热保护】%s' % onoff(g['enabled']))
    print('  guard=%g,%g,%g,%g          预警/严重/逃生/解除（C）'
          % (g['warn_c'], g['crit_c'], g['escape_c'], g['recover_c']))
    print('  pl_guard=%g,%g,%g,%g        预警 PL1,PL2 / 严重 PL1,PL2（W）'
          % (g['warn_pl1_w'], g['warn_pl2_w'], g['crit_pl1_w'], g['crit_pl2_w']))
    print('  dts=%g             EC 读不到时的 DTS 兜底（C）' % g['dts_crit_c'])
    print('  flag_hot=%g        固件标志加密温度（C）' % g['flag_hot_c'])
    print('  guard_on=%s  ffmax=%s  trip=%s  hibernate=%s'
          % (onoff(g['enabled']), onoff(g['force_fan_max']),
             onoff(g['trip_flags']), onoff(g['escape_hibernate'])))
    print('  hold=%g            固件标志锁存判定秒数（0=关掉这条出口）'
          % g.get('trip_hold_s', 45.0))
    print('  flag_pl=%g,%g       标志置位但温度正常时的 PL1,PL2（不砍到 crit 的 10W）'
          % (g.get('flag_pl1_w', 20.0), g.get('flag_pl2_w', 28.0)))
    print('  gmin=%g  rel=%g     保护最短驻留 / 解除驻留（秒，防 40W↔10W 来回切）'
          % (g.get('min_hold_s', 30.0), g.get('release_dwell_s', 10.0)))
    print('  cool_tail=%g       退出保护后继续满速这么久（秒，缓冲功率阶跃）'
          % g.get('cool_tail_s', 30.0))
    print()
    print('改值：3-config.bat ac=40,55   |   撤销：3-config.bat undo   |   全部用法：3-config.bat help')
    print()


# ---------------------------------------------------------------- 解析
class Fail(Exception):
    pass


def _num(v, lo, hi, name, integer=False, allow_zero=False):
    try:
        x = float(v)
    except ValueError:
        raise Fail('%s 必须是数字：%s' % (name, v))
    if allow_zero and x == 0:
        return 0 if integer else 0.0
    if x < lo or x > hi:
        raise Fail('%s 超出范围 %g~%g：%g' % (name, lo, hi, x))
    if integer:
        if abs(x - round(x)) > 1e-9:
            raise Fail('%s 必须是整数：%s' % (name, v))
        return int(round(x))
    return x


def _nums(v, n, name):
    xs = [s.strip() for s in str(v).split(',')]
    if len(xs) != n:
        raise Fail('%s 需要 %d 个数（用逗号分隔）：%s' % (name, n, v))
    return xs


def _bool(v, name):
    s = str(v).strip().lower()
    if s in ('on', '1', 'true', 'yes', 'y', '开'):
        return True
    if s in ('off', '0', 'false', 'no', 'n', '关'):
        return False
    raise Fail('%s 只能是 on/off：%s' % (name, v))


def apply_kv(cfg, key, val):
    k = key.strip().lower()
    f, g = cfg['fan'], cfg['guard']

    if k == 'ac':
        a, b = _nums(val, 2, 'ac')
        cfg['pl1_w'] = _num(a, 5, 150, 'ac 的 PL1', allow_zero=True)
        cfg['pl2_w'] = _num(b, 5, 150, 'ac 的 PL2', allow_zero=True)
    elif k == 'batt':
        a, b = _nums(val, 2, 'batt')
        cfg['pl1_w_batt'] = _num(a, 5, 150, 'batt 的 PL1', allow_zero=True)
        cfg['pl2_w_batt'] = _num(b, 5, 150, 'batt 的 PL2', allow_zero=True)
    elif k == 'time':
        a, b = _nums(val, 2, 'time')
        cfg['pl1_time'] = _num(a, 1, 255, 'PL1 时间窗', integer=True)
        cfg['pl2_time'] = _num(b, 1, 255, 'PL2 时间窗', integer=True)
    elif k == 'min_pl1':
        cfg['min_pl1_w'] = _num(val, 5, 150, 'min_pl1')
    elif k == 'mmio':
        s = str(val).strip().lower()
        if s not in ('disable', 'value'):
            raise Fail('mmio 只能是 disable 或 value')
        cfg['mmio_mode'] = s
    elif k == 'lock':
        cfg['lock_mmio'] = _bool(val, 'lock')
    elif k == 'poll':
        a, b, c = _nums(val, 3, 'poll')
        cfg['poll_s'] = _num(a, 1, 600, '主循环间隔')
        cfg['ec_poll_s'] = _num(b, 2, 600, 'EC 读间隔')
        cfg['flag_poll_s'] = _num(c, 2, 600, '固件标志间隔')
    elif k == 'fan':
        xs = [s.strip() for s in str(val).split(',')]
        n = len(f['curve']) - 1
        if len(xs) not in (n, n + 1):
            raise Fail('fan 需要 %d 个边界（可选再加 1 个回差），用逗号分隔' % n)
        prev = -1
        for i in range(n):
            hi = _num(xs[i], 1, 120, '第 %d 段边界' % (i + 1), integer=True)
            if hi <= prev:
                raise Fail('fan 的边界必须递增')
            prev = hi
            f['curve'][i][0] = hi
        f['curve'][-1][0] = 999
        if len(xs) == n + 1:
            f['hyst'] = _num(xs[n], 0, 30, '回差')
    elif k == 'lv':
        xs = _nums(val, len(f['curve']), 'lv')
        for i, s in enumerate(xs):
            v = _num(s, 1, 255, '第 %d 段档位' % (i + 1), integer=True)
            if 7 < v < 64:
                raise Fail('档位只能是 1~7 或 64（满速）')
            f['curve'][i][1] = v
    elif k == 'mode':
        s = str(val).strip().lower()
        if s not in ('curve', 'max', 'level', 'ec'):
            raise Fail('mode 只能是 curve / max / level / ec')
        f['mode'] = s
    elif k == 'level':
        f['level'] = _num(val, 0, 255, 'level', integer=True, allow_zero=True)
    elif k == 'hyst':
        f['hyst'] = _num(val, 0, 30, '回差')
    elif k == 'guard':
        xs = _nums(val, 4, 'guard')
        g['warn_c'] = _num(xs[0], 30, 120, '预警温度')
        g['crit_c'] = _num(xs[1], 30, 125, '严重温度')
        g['escape_c'] = _num(xs[2], 30, 130, '逃生温度')
        g['recover_c'] = _num(xs[3], 20, 115, '解除温度')
    elif k == 'pl_guard':
        xs = _nums(val, 4, 'pl_guard')
        g['warn_pl1_w'] = _num(xs[0], 4, 150, '预警 PL1')
        g['warn_pl2_w'] = _num(xs[1], 4, 150, '预警 PL2')
        g['crit_pl1_w'] = _num(xs[2], 4, 150, '严重 PL1')
        g['crit_pl2_w'] = _num(xs[3], 4, 150, '严重 PL2')
    elif k == 'dts':
        g['dts_crit_c'] = _num(val, 40, 125, 'dts')
    elif k == 'flag_hot':
        g['flag_hot_c'] = _num(val, 30, 115, 'flag_hot')
    elif k == 'guard_on':
        g['enabled'] = _bool(val, 'guard_on')
    elif k == 'ffmax':
        g['force_fan_max'] = _bool(val, 'ffmax')
    elif k == 'trip':
        g['trip_flags'] = _bool(val, 'trip')
    elif k == 'hold':
        g['trip_hold_s'] = _num(val, 0, 3600, '锁存判定秒数（0=关）', allow_zero=True)
    elif k == 'dwell':
        f['min_dwell_s'] = _num(val, 0, 600, '降档最短驻留秒数（0=关）', allow_zero=True)
    elif k == 'flag_pl':
        a, b = _nums(val, 2, 'flag_pl')
        g['flag_pl1_w'] = _num(a, 5, 150, '标志档 PL1', allow_zero=True)
        g['flag_pl2_w'] = _num(b, 5, 150, '标志档 PL2', allow_zero=True)
    elif k == 'gmin':
        g['min_hold_s'] = _num(val, 0, 600, '保护最短驻留秒数（0=关）', allow_zero=True)
    elif k == 'rel':
        g['release_dwell_s'] = _num(val, 0, 600, '解除驻留秒数（0=关）', allow_zero=True)
    elif k == 'boost':
        a, b, c = _nums(val, 3, 'boost')
        f['load_boost_pct'] = _num(a, 0, 100, '前馈占用阈值（0=关）', allow_zero=True)
        f['load_boost_s'] = _num(b, 0, 600, '前馈持续秒数', allow_zero=True)
        f['load_boost_level'] = _num(c, 0, 64, '前馈档位', integer=True, allow_zero=True)
    elif k == 'boost_hold':
        f['load_hold_s'] = _num(val, 0, 600, '前馈尾巴秒数', allow_zero=True)
    elif k == 'fan_dts':
        f['dts_max_c'] = _num(val, 0, 120, 'DTS 前馈阈值（0=关）', allow_zero=True)
    elif k == 'fgap':
        f['min_write_gap_s'] = _num(val, 0, 300, '风扇写入最小间隔秒数（0=关）', allow_zero=True)
    elif k == 'flag_slow':
        cfg['flag_poll_slow_s'] = _num(val, 2, 3600, '凉机标志读间隔秒数', allow_zero=True)
    elif k == 'cool_tail':
        g['cool_tail_s'] = _num(val, 0, 600, '保护退出后继续满速秒数（0=关）', allow_zero=True)
    elif k == 'hibernate':
        g['escape_hibernate'] = _bool(val, 'hibernate')
    # ---- 单项简写
    elif k in ('pl1', 'pl2', 'pl1_batt', 'pl2_batt'):
        path = {'pl1': 'pl1_w', 'pl2': 'pl2_w',
                'pl1_batt': 'pl1_w_batt', 'pl2_batt': 'pl2_w_batt'}[k]
        cfg[path] = _num(val, 5, 150, k, allow_zero=True)
    elif k in ('warn', 'crit', 'escape', 'recover'):
        path = {'warn': 'warn_c', 'crit': 'crit_c',
                'escape': 'escape_c', 'recover': 'recover_c'}[k]
        g[path] = _num(val, 20, 130, k)
    elif k in ('warn_pl1', 'warn_pl2', 'crit_pl1', 'crit_pl2'):
        g[k + '_w'] = _num(val, 4, 150, k)
    elif k in ('poll_s', 'ec_poll', 'flag_poll'):
        path = {'poll_s': 'poll_s', 'ec_poll': 'ec_poll_s', 'flag_poll': 'flag_poll_s'}[k]
        cfg[path] = _num(val, 1, 600, k)
    else:
        raise Fail('不认识的项：%s（运行 3-config.bat help 看用法）' % key)


def check(cfg):
    """整体一致性检查。返回 warning 列表；不合法直接抛 Fail。"""
    f, g = cfg['fan'], cfg['guard']
    warns = []
    if not (g['recover_c'] < g['warn_c'] < g['crit_c'] <= g['escape_c']):
        raise Fail('温度阈值必须满足 解除 < 预警 < 严重 <= 逃生')
    if g['crit_pl1_w'] > g['warn_pl1_w']:
        warns.append('严重档 PL1 比预警档还高，保护力度是反的')
    if g['crit_pl2_w'] > g['warn_pl2_w']:
        warns.append('严重档 PL2 比预警档还高，保护力度是反的')
    if 0 < cfg['pl1_w'] and 0 < cfg['pl2_w'] and cfg['pl2_w'] < cfg['pl1_w']:
        warns.append('PL2 低于 PL1，工具会自动抬平（等于没有爆发）')
    if 0 < cfg['pl1_w_batt'] and 0 < cfg['pl2_w_batt'] and cfg['pl2_w_batt'] < cfg['pl1_w_batt']:
        warns.append('电池档 PL2 低于 PL1，工具会自动抬平')
    if cfg['flag_poll_s'] < cfg['ec_poll_s']:
        warns.append('固件标志读得比 EC 温度还勤，建议 flag_poll 不小于 ec_poll')
    if f['hyst'] < 2:
        warns.append('回差小于 2C 时，温度在边界附近几乎必然来回切档')
    if f['mode'] in ('ec', 'off', 'auto'):
        warns.append('风扇交还 EC 自动：原生曲线到不了满速')
    if cfg['mmio_mode'] == 'value':
        warns.append('mmio=value 是锁死 MMIO 的老方案，改功耗墙要重启')
    if not cfg['lock_mmio']:
        warns.append('lock=off 时固件可能把功耗墙改回去')
    if not g['enabled']:
        warns.append('过热保护已关闭')
    if g['escape_hibernate']:
        warns.append('hibernate=on：到逃生温度会主动休眠，未保存的工作可能丢')
    return warns


# ---------------------------------------------------------------- 主流程
def main(argv):
    if os.path.exists(CFG):
        try:
            cfg = load()
        except Exception as e:
            print('[X] config.json 不是合法 JSON：%s' % e)
            print('    先运行 3-config.bat undo 恢复上一版')
            return 1
    else:
        print('[X] 找不到 %s' % CFG)
        return 1

    args = [a for a in argv if a.strip()]
    if not args:
        show(cfg)
        return 0
    if args[0].lower() in ('help', '-h', '--help', '?'):
        print(__doc__.strip())
        show(cfg)
        return 0
    if args[0].lower() == 'raw':
        print(dump(cfg), end='')
        return 0
    if args[0].lower() == 'undo':
        if not os.path.exists(BAK):
            print('[X] 没有可撤销的备份（%s 不存在）' % BAK)
            return 1
        try:
            old = load(BAK)
        except Exception as e:
            print('[X] 备份文件也坏了：%s' % e)
            return 1
        atomic_write(CFG, dump(old))
        print('已恢复到上一次保存前的内容。')
        show(load())
        return 0

    before = dump(cfg)
    changed = []
    for a in args:
        if '=' not in a:
            print('[X] 格式不对：%s（应该是 键=值，例如 ac=40,55）' % a)
            show(cfg)
            return 2
        k, v = a.split('=', 1)
        try:
            apply_kv(cfg, k, v)
            changed.append(k.strip())
        except Fail as e:
            print('[X] %s' % e)
            return 2

    try:
        warns = check(cfg)
    except Fail as e:
        print('[X] %s' % e)
        return 2

    if dump(cfg) == before:
        print('数值没有变化，没有写盘。')
        return 0

    save(cfg)
    print('已保存（上一版存为 config.json.bak），服务会在 60 秒内自动应用。')
    for w in warns:
        print('  [!] %s' % w)
    show(load())
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(1)
