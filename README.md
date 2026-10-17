# CpuPLFix

修复 **Intel 笔记本开机 / 睡眠唤醒后 CPU 被锁在 0.55GHz** 的小工具。

症状：开机或唤醒后 CPU 频率被钉在很低的水平（0.55 / 0.8 / 1.0 GHz），风扇不转、温度不高，但什么都卡。
重启或者手动在 ThrottleStop 里点一下能好，下次又犯。

本工具做成**计划任务 + 内核寄存器写入**，开机 15 秒、唤醒 4 秒后自动修复，不需要你手动干预，
也不需要 ThrottleStop 常驻。

---

## 原理

Intel 平台的封装功耗墙（PL1 长时 / PL2 短时）在硬件里存了**两份镜像**：

| | 位置 | 谁在用 |
|---|---|---|
| 系统侧 | `MSR 0x610` (PKG_POWER_LIMIT) | Windows / ThrottleStop |
| 固件侧 | `MCHBAR + 0x59A0` (MMIO 镜像) | 主板 / EC 固件，bit63 是锁定位 |

正常情况下两份应该一致。但有些机器的固件会在开机和唤醒时把 MMIO 那一份改成一个很小的值，
CPU 就被钉在最低频。

在 ThrottleStop 里勾选 TPL 窗口的 **MMIO Lock** 之所以能修好，就是因为它把正常值写回 MMIO
再把 bit63 置 1 —— 锁上之后固件再也改不动，所以必须重启才能恢复原样。

本工具做同一件事，只是无界面、开机自动跑。

在作者机器上（Lenovo Ice Lake）实测：

```
修复前  MSR 0x610 : PL1 55.00W  PL2 55.00W  lock=0
        MMIO 0x59A0: PL1 21.00W  PL2 55.00W  lock=0   <-- 被固件压低
修复后  MMIO 0x59A0: PL1 55.00W  PL2 55.00W  lock=1   <-- 倍频回到 4.8GHz
```

---

## 用法

1. 下载本仓库（`Code` → `Download ZIP`），解压到任意目录
2. 准备 ThrottleStop：下载 <https://www.techpowerup.com/download/techpowerup-throttlestop/>
3. 双击 `1-install.bat`，UAC 弹窗点"是"

其它脚本：

| 脚本 | 作用 |
|---|---|
| `2-check.bat` | 查看当前状态（两份功耗墙、频率、是否已锁定） |
| `3-fix-now.bat` | 立即修复一次 |
| `9-uninstall.bat` | 卸载（删计划任务和驱动服务，不删文件） |

要求：Intel CPU / Windows 10、11 64 位 / 安装时需要管理员权限。

---

## 关于驱动

读写 MSR 和物理内存必须有内核驱动。**本仓库不附带任何驱动文件。**

安装时工具会从你机器上已安装的 `ThrottleStop.exe` 里提取它自带的那个驱动
（ThrottleStop 把驱动作为资源内嵌在 exe 里，作者签名完整，提取过程不改一个字节），
存成本目录下的 `TSCtl.sys` 并注册成服务 `TSCtl`。

所以请先把 `ThrottleStop.exe` 放到本目录，或者装在桌面 / 下载 / Program Files 下 ——
工具会自动去找。找不到会给出提示。

顺带一提，驱动内嵌的位置和提取方式见 `CpuPLFix.py` 里的 `_extract_driver_from_exe()`：
按 PE 结构扫描 `MZ`/`PE\0\0` 头，取 `machine == 0x8664` 的镜像，再按 Security 目录
把 Authenticode 签名一起带出来（少一个字节签名就失效，驱动就加载不了）。

---

## 关于安全性

工具的原则是**镜像系统自己的设置**，不是超频：

- MSR 里的 PL1 是正常值 → 原样写进 MMIO，一个瓦都不改
- 只有 MSR 本身也被压坏（低于 `min_pl1_w`，默认 15W）时，才用 CPU 标称 TDP 兜底

CPU 自身的热保护（温度墙、EDP 电流墙）完全不受影响，该降频还是会降频。

`config.json`：

```json
{
  "pl1_w": 0,        // 长时功耗墙(瓦)，0 = 自动
  "pl2_w": 0,        // 短时功耗墙(瓦)，0 = 自动
  "pl1_time": 35,
  "pl2_time": 66,
  "min_pl1_w": 15.0, // 判定 MSR 是否异常的阈值
  "lock_mmio": true  // 是否锁死 MMIO
}
```

> MMIO 一旦锁定，要重启才会解除。想连续调参的话先把 `lock_mmio` 设为 `false`。

---

## 技术细节：ThrottleStop 驱动的 IOCTL 表

驱动是 WinRing0 派生的，但 **IOCTL 编号被作者全部改掉了**，WinRing0 的标准码全部返回
`STATUS_UNSUCCESSFUL (0xC0000001)`。作者用 capstone 反汇编定位出来的：

`DriverEntry` (RVA `0x1730`) → `MajorFunction[14]` (RVA `0x1EF0`) → 范围校验
`add eax, 0x7FFF9BD0` → 有效码区间 `0x80006430..0x800064A4`，
二级跳转表在 RVA `0x2524`(byte 索引) / `0x24F4`(dword 偏移)。

| 功能 | IOCTL | 输入 / 输出 |
|---|---|---|
| 版本 | `0x80006494` | 空 → u32 |
| RDMSR | `0x80006448` | `u32 msr`（in len=4）→ `u64`（out len 必须=8） |
| WRMSR | `0x8000644C` | `{u32 msr; u64 value}` 共 12 字节 |
| 读物理内存 | `0x80006498` | `u64 物理地址` → 1/2/4/8 字节 |
| 写物理内存 | `0x8000649C` | `u64 地址` + 值（in len = 8+N） |
| 读 PCI 配置 | `0x800064A0` | `{u32 addr; u32 offset}`，`addr = bus \| dev<<8 \| fn<<16` |
| 写 PCI 配置 | `0x800064A4` | 同上 + 值 |
| 读 I/O 端口 | `0x80006430` | `u16 port` → 1/2/4 字节 |
| 写 I/O 端口 | `0x80006434` | `u16 port` + 值 |

读物理内存内部走 `MmMapIoSpace(addr, size, MmNonCached)` → 读/写 → `MmUnmapIoSpace`，
所以直接拿它操作 MMIO 是安全的。

---

## 已知限制

- 只处理 PL1/PL2 这条路径。如果是 **BD PROCHOT**、温度墙、电池/适配器识别问题导致的降频，
  本工具管不了，那种情况还是得用 ThrottleStop 排查。
- MMIO 功耗墙寄存器按 `0x59A0` 处理（Intel 近几代通用）。如果你的机器偏移不同，
  `2-check.bat` 会显示读不到或数值明显不对。

---

## License

代码 MIT。不包含任何第三方驱动或 ThrottleStop 的二进制文件。
