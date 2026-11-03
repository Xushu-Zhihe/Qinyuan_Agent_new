构建 Linux 沙箱：

docker build `
  --progress=plain `
  --provenance=false `
  --sbom=false `
  --file Sandbox/dockerfile `
  --tag qinyuan/pentest-sandbox:0.3.8 `
  .

构建 Windows/Wine 专用 profile（必须在 Linux 镜像之后）：

docker build `
  --progress=plain `
  --file Sandbox/windows.dockerfile `
  --tag qinyuan/windows-sandbox:0.3.9 `
  .

构建网络目标转发 sidecar（网络绑定任务才会用到）：

docker build `
  --progress=plain `
  --file Sandbox/target_forwarder.dockerfile `
  --tag qinyuan/t6-target-forwarder:0.1.0 `
  .

转发镜像只需要 python3 和两个固定脚本，因此使用 `debian:bookworm-slim`
而不是整套分析镜像（176MB 而不是 2.67GB）；`T6TargetNetwork` 只按 tag 和
`qinyuan.target.private_ipv4=rfc1918` 标签取镜像。

构建后分别把
`docker image inspect <tag> --format '{{.Id}}'` 的结果同步到
`agent_adapters/docker_profile.py` 的 `IMAGE_ID` / `WINDOWS_IMAGE_ID`。
D9 按固定镜像 ID 和完整容器 spec 校验运行配置。

0.3.6 在 0.3.5 基础上增加：mingw-w64 交叉编译（x86_64-w64-mingw32-gcc）、
pefile、qemu-system-x86_64、cpio，以及 qinyuan-exec（PR_SET_PTRACER 启动器，
让 `gdb -p` 能附加到前一次 tool call 启动的进程）。

0.3.7 在 0.3.6 基础上补齐归档/解包能力：
unzip、zip、p7zip-full（7z）、libarchive-tools（bsdtar）、zstd、xz-utils、
bzip2、lz4、cabextract、xxd、squashfs-tools、binwalk、e2fsprogs（debugfs）、
unrar-free、msitools（msiextract）、innoextract。
binwalk 2.3 依赖 capstone 4/5，运行时 PYTHONPATH 暴露的是 capstone 6，因此
`/usr/local/bin/binwalk` 包装器清空 PYTHONPATH 后调用 Debian 的
python3-capstone；`kernel_unpack` 的 cpio 解压链也扩展到
gzip/xz/zstd/lz4/bzip2/raw。

0.3.8（Linux）增加轻量 crypto 数学栈（不装完整 SageMath）：
`pari-gp`、`python3-cypari2`、`python3-sympy`、`python3-gmpy2`、
`python3-fpylll`，合计约 165MB；`Crypto_Mcp` 提供 `pari`（PARI/GP 表达式）、
`sympy`（符号计算/因式分解）、`lll`（fpylll 格基约简）。同时修正了 T6 的
PATH 顺序：`/opt/dirsearch/bin` 必须排在 `/usr/bin` 之后，否则它的 python3
符号链接会遮住系统解释器，导致 MCP 的 `python3` 看不到 apt 安装的
sympy/cypari2/fpylll。Linux profile 因此升为 `d3-fixed-readonly-v3`；
资源档位调整后标签再次升为 v4，见下。

## 资源档位（2026-11-03）

镜像 ID 不变，只改容器资源上限；`QINYUAN_SANDBOX_PROFILE` 一次只选一档，
默认 `linux`：

| profile | 标签 | 内存/swap | CPU | pids | 输入 `/workspace` | work `/workspace/work` | `/tmp` |
|---|---|---|---|---|---|---|---|
| linux（默认） | `d3-fixed-readonly-v4` | 2 GiB / 2 GiB | 1.0 | 256 | 128m | 512m | 256m |
| linux-heavy | `linux-heavy-v1` | 4 GiB / 4 GiB | 2.0 | 512 | 128m | 1g | 512m |
| windows | `windows-wine-v3` | 8 GiB / 8 GiB | 2.0 | 512 | 128m | 4g | 512m |

`/tools` 256 MiB、`/home/sandbox` 64 MiB 三档一致。swap 与内存等额，即关闭
swap：命中上限直接 exit 137，不让宿主抖动。tmpfs 页计入容器 memcg，所以
linux 档各项上限合计 1216 MiB < 2048 MiB、heavy 档 1984 MiB < 4096 MiB、
windows 档 5056 MiB < 8192 MiB；旧配置 640 MiB > 512 MiB 是 OOM 的一个来源，
本次一并修正。输入区独立成 `input_size`，因为 T6 附件导入总量上限只有
64 MiB。

windows 档内存从 4 GiB 提到 8 GiB 的依据是 2026-11-03 的真实 OOM 记录
（run `882d0fb1`）：那次容器是 4 GiB，两个 Wine prefix 已建在 work 里
（约 1.9 GB，计入 memcg），再叠 gdb+pwntools 的堆遍历就把 4 GiB 顶穿，
工具结果是 exit 137 + 空输出。所以 windows 档的 4 GiB 对“Wine + 量堆”
这个组合不够，不是 linux 档 512 MiB 的问题。

`Sandbox/settings.py` 的 `SandboxSettings` 是 linux 档，`LinuxHeavySandboxSettings`
是 heavy 档；`Sandbox/windows_sandbox.py` 的 `WindowsSandboxSettings` 是
Windows 档。`agent_adapters/docker_profile.py` 用一张 profile 表把标签、镜像
ID 和 settings/sandbox 类绑在一起；档位或数值变化时标签必须一起改，旧运行的
冻结配置会以 `configuration_mismatch` 显式失配，不会静默复用新上限。

Windows profile 只在显式选择时启用：

  QINYUAN_SANDBOX_PROFILE=windows

它使用 `qinyuan/windows-sandbox:0.3.9`（基于 Linux 0.3.8）、8 GiB 内存、
2 CPU、4 GiB `/workspace/work`、512 MiB `/tmp`，并设置
`WINEPREFIX=/workspace/work/.wine64`、`WINEPREFIX32=/workspace/work/.wine32`、
`TMPDIR=/workspace/work/tmp`。4 GiB work 是上限不是承诺：tmpfs 同样计入
8 GiB 内存，真要写满仍会被 kill。当前是部署级开关（整个 workbench 进程
生效）；按任务选择需要新增请求字段。

Windows 0.3.8 增加 32 位 PE 支持：启用 i386 multiarch，安装
`wine64`、`wine32` 和 `gcc-mingw-w64-i686`（旧的 0.3.7 只有 wine64，32 位
PE 跑不了）。Debian 的 `/usr/bin/wine` 只要检测到 wine32 就优先走 32 位
loader，因此镜像提供三个确定性入口：

  qy-wine64   固定 /usr/lib/wine/wine64 + .wine64
  qy-wine32   固定 /usr/lib/wine/wine   + .wine32
  qy-wine-auto 读 PE 头（PE32/PE32+）自动选

`Wine_Mcp` 的 `wine_init` 一次创建两个 prefix，`wine_run`/`wine_debug` 默认
`arch=auto`，也可显式 `arch=32`/`64`。实测两个 prefix 合计约 1.9 GB，因此
work 从 1 GiB 提到 4 GiB、内存从 2 GiB 提到 4 GiB；32 位 PE 在 win32 prefix
和 win64 的 WoW64 两条路径都能跑。Windows 0.3.9 基于 Linux 0.3.8，额外继承
PARI/SymPy/fpylll 这套 crypto 栈；Wine 相关行为不变。
