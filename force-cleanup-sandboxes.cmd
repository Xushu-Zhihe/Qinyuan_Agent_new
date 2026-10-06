@echo off
echo ============================================
echo 强制清理 Qinyuan Agent Sandbox 容器
echo ============================================
echo.

echo [1/3] 查找所有 qinyuan 相关容器...
docker ps -a --filter "name=qinyuan" --format "table {{.ID}}\t{{.Names}}\t{{.Status}}"
echo.

echo [2/3] 停止所有运行中的 qinyuan 容器...
for /f "tokens=*" %%i in ('docker ps -q --filter "name=qinyuan"') do (
    echo 停止容器: %%i
    docker stop %%i
)
echo.

echo [3/3] 删除所有 qinyuan 容器...
for /f "tokens=*" %%i in ('docker ps -aq --filter "name=qinyuan"') do (
    echo 删除容器: %%i
    docker rm %%i
)
echo.

echo ============================================
echo 清理完成！
echo ============================================
echo.
echo 现在你可以：
echo 1. 在 Workbench 中使用批量清理功能
echo 2. 或者归档这些任务
echo.
pause
