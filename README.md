# GrokBot

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

## 任务

在「任务」页填写描述和验收条件，再选择 Engineer 和 Reviewer。Engineer 提交完整的 Python 源码或文本，以及由原始验收条件得出的测试。后端在 Monty 沙箱中执行自包含的 Python 代码和测试。文本测试可以用 `result_text` 检查交付内容。[打开任务页](http://127.0.0.1:8000/#tasks)

Reviewer 读取结果与实测记录，并逐条给出证据。第一次 FAIL 会把完整评审和运行记录交还 Engineer。后端用相同条件和相同测试检查修复结果。测试和评审都通过后，任务完成。第二次 FAIL 后，任务状态为 `NEEDS_HUMAN_REVIEW`。模型错误、无效评审或服务中断会停止任务。

每个 Bot 只读取自己的单聊记录。SQLite 的 `task_attempts` 表保存每次尝试。每次交付的文件和测试保存在 `data/tasks/<任务 ID>/attempt-<次数>/`。任务页可以查看两次结果、测试记录和评审。旧任务保留原有结果和评审。

运行 `uv run --locked python -m unittest discover -s tests -v` 可验证任务流程。测试使用临时数据库和模拟模型回复。

原群聊可独立启动。运行 `uv run --with streamlit==1.67.0 streamlit run app.py --server.address 127.0.0.1 --server.port 8501`。[打开群聊](http://127.0.0.1:8501)
