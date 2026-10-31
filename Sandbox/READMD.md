构建 Linux 沙箱：

docker build `
  --progress=plain `
  --provenance=false `
  --sbom=false `
  --file Sandbox/dockerfile `
  --tag qinyuan/pentest-sandbox:0.3.6 `
  .

构建 Windows/Wine 专用 profile：

docker build `
  --progress=plain `
  --file Sandbox/windows.dockerfile `
  --tag qinyuan/windows-sandbox:0.3.6 `
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

Windows profile 只在显式选择时启用：

  QINYUAN_SANDBOX_PROFILE=windows

它使用 `qinyuan/windows-sandbox:0.3.6`、2 GiB 内存、1.5 CPU、1 GiB
`/workspace/work`、256 MiB `/tmp`，并设置 `WINEPREFIX=/workspace/work/.wine`。
默认 Linux profile（512 MiB / 128 MiB work）和其安全边界完全不变。当前是
部署级开关（整个 workbench 进程生效）；按任务选择需要新增请求字段。
