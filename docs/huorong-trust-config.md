# 火绒信任区配置清单（等用户在场操作）

> 用户不在电脑前时 UAC 弹窗无法点，火绒主界面打不开。用户回来后操作。
> 步骤：打开火绒 → 防护中心 → 信任区（或 设置 → 信任区）→ 添加

## 要添加的信任项

### 目录（推荐，一次覆盖所有子文件）

| 路径 | 用途 |
|---|---|
| E:\Projects | 所有项目目录（mock 脚本、子进程都在这里跑） |
| C:\Users\31619\AppData\Local\hermes | Hermes Agent 本体 |
| C:\Users\31619\AppData\Local\Temp | 临时脚本执行 |
| C:\Users\31619\.claude | Claude Code 配置与技能 |
| E:\BCRJ\Git | git-bash（Hermes 的 shell，实际安装位置，非 Program Files） |
| C:\Program Files\nodejs | Node.js（Claude Code CLI） |

### 程序（如果目录信任不够，再加）

| 路径 | 用途 |
|---|---|
| C:\Users\31619\AppData\Local\Programs\Python\Python312\python.exe | Python（如果存在） |
| C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12*\python.exe | Windows Store Python（verify_env 用的） |
| C:\Program Files (x86)\Huorong\Sysdiag\bin\HipsMain.exe | 火绒自身（防止误拦自己） |

## 操作要点

1. 如果打开火绒主界面弹 UAC 提权 → 点"是"
2. 信任区添加目录时选"添加目录/文件夹"（不是"添加文件"）
3. WindowsApps 路径带版本随机后缀，如果找不到，先加前两个目录即可（E:\Projects 和 hermes 目录覆盖 90% 场景）
4. 添加完确认列表里有这些项，关掉即可

## 为什么需要

Hermes 通过终端执行命令时会启动子进程（python 脚本、node、git 等），火绒默认拦截"系统加固/主防"里未信任程序的行为（起子进程、写文件、kill 进程等）。信任区加白后不再弹拦截，项目开发不卡壳。

## 备选（如果信任区不好用）

- 火绒 → 防护中心 → 关闭"系统加固"里的"命令行拦截"相关项（不推荐，降低防护）
- 或者被拦时点火绒弹窗的"允许/信任"按钮（按次放行，不持久）
