@echo off
echo === 测试 Dirsearch MCP ===
echo.
echo 发送初始化请求...
echo {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}} > test_input.txt
echo {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"dirsearch_check","arguments":{}}} >> test_input.txt
echo.
echo 运行 MCP 服务器...
python C:\Users\15131\Desktop\Dirsearch_Mcp\src\server.py < test_input.txt
echo.
echo === 测试完成 ===
pause