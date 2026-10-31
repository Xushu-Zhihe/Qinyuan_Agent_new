构建 Linux 沙箱：

docker build `
  --progress=plain `
  --provenance=false `
  --sbom=false `
  --file Sandbox/dockerfile `
  --tag qinyuan/pentest-sandbox:0.3.7 `
  .

构建 Windows/Wine 专用 profile（必须在 Linux 镜像之后）：

docker build `
  --progress=plain `
  --file Sandbox/windows.dockerfile `
  --tag qinyuan/windows-sandbox:0.3.8 `
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

Windows profile 只在显式选择时启用：

  QINYUAN_SANDBOX_PROFILE=windows

它使用 `qinyuan/windows-sandbox:0.3.8`（基于 Linux 0.3.7）、4 GiB 内存、
2 CPU、4 GiB `/workspace/work`、512 MiB `/tmp`，并设置
`WINEPREFIX=/workspace/work/.wine64`、`WINEPREFIX32=/workspace/work/.wine32`、
`TMPDIR=/workspace/work/tmp`。默认 Linux profile（512 MiB / 128 MiB work）
和其安全边界完全不变。当前是部署级开关（整个 workbench 进程生效）；按任务
选择需要新增请求字段。

0.3.8 的 Windows profile 增加 32 位 PE 支持：启用 i386 multiarch，安装
`wine64`、`wine32` 和 `gcc-mingw-w64-i686`（旧的 0.3.7 只有 wine64，32 位
PE 跑不了）。Debian 的 `/usr/bin/wine` 只要检测到 wine32 就优先走 32 位
loader，因此镜像提供三个确定性入口：

  qy-wine64   固定 /usr/lib/wine/wine64 + .wine64
  qy-wine32   固定 /usr/lib/wine/wine   + .wine32
  qy-wine-auto 读 PE 头（PE32/PE32+）自动选

`Wine_Mcp` 的 `wine_init` 一次创建两个 prefix，`wine_run`/`wine_debug` 默认
`arch=auto`，也可显式 `arch=32`/`64`。实测两个 prefix 合计约 1.9 GB，因此
work 从 1 GiB 提到 4 GiB、内存从 2 GiB 提到 4 GiB；32 位 PE 在 win32 prefix
和 win64 的 WoW64 两条路径都能跑。
