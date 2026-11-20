# GrokBot · Stage 1

在本地创建 Bot，编辑名称、角色和系统提示词，并与每个 Bot 单独聊天。

## 启动

在 Windows 上安装 Node.js 22.12 或以上版本和 `uv`。后端从环境变量读取模型配置。使用百炼的 Anthropic 兼容接口时，本机已有的配置可以直接复用。

| 环境变量 | 用途 |
| --- | --- |
| `ANTHROPIC_BASE_URL` | Messages 接口的基础地址，例如 `https://dashscope.aliyuncs.com/apps/anthropic` |
| `ANTHROPIC_AUTH_TOKEN` | 该接口的访问令牌 |
| `ANTHROPIC_MODEL` | 模型标识，省略时读取 `ANTHROPIC_DEFAULT_SONNET_MODEL` |

在项目目录打开 PowerShell，运行以下命令。

```powershell
.\start.ps1
```

脚本安装锁定的依赖，使用 Vite 构建 React 页面，然后启动 FastAPI。打开本地页面即可创建 Bot。[打开 GrokBot](http://127.0.0.1:8000)

## 数据

Bot 档案和各自的对话保存在 `data/grokbot.sqlite3`。模型调用成功后，一条提问和对应回答一起写入数据库。按 Ctrl+C 停止服务，再次运行启动脚本即可恢复档案和历史。
