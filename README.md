# Grokbot MVP

一个本地运行的双成员群聊原型。你在 Streamlit 页面提交任务后，任务负责人和审阅者通过 OpenCode 依次提出方案、反馈、执行和验收。

## 启动

在 Windows 上安装 `uv`，并为 `opencode` 配置可用的模型。在项目目录打开 PowerShell，运行以下命令。

```powershell
.\start.ps1
```

打开 <http://127.0.0.1:8501>，首次使用时填写两名成员的名称和职责，然后发送任务。

聊天记录保存在 `group-data/group.db`，任务文件保存在 `group-data/work/`。
