# GitHub 仓库设置

## Git 与 GitHub 的区别

- Git 是本机版本数据库。即使没有网络，也能提交、比较和回滚。
- GitHub 是远程托管、异地备份、Issue、Pull Request、自动检查和 Release 平台。

## 仓库设置

| 项目 | 设置 |
|---|---|
| 所有者 | `z13141557217-web` |
| 仓库名 | `steadyroute` |
| 可见性 | Private |
| 默认分支 | `main` |
| 协议 | HTTPS，由 GitHub CLI 管理凭据 |
| Issues | 开启 |
| Actions | 开启 |
| Wiki/Pages | 不需要 |

远程地址：

```text
https://github.com/z13141557217-web/steadyroute
```

## 登录 GitHub CLI

```bash
gh auth login -h github.com
```

依次选择 GitHub.com、HTTPS、使用浏览器登录，并同意让 GitHub CLI 配置 Git 凭据。验证：

```bash
gh auth status
```

不要把网页密码、验证码或令牌粘贴到聊天、文档或源码中。

## 在新电脑上开始

```bash
git clone https://github.com/z13141557217-web/steadyroute ~/Projects/steadyroute
cd ~/Projects/steadyroute
./scripts/check.sh
```

需要在这台 Mac 上运行稳航时，在仓库目录执行 `./install.command`，见
[DEPLOYMENT.md](DEPLOYMENT.md)。

## 推送前的安全检查

```bash
./scripts/check.sh
git status --short
git log --oneline --decorate -5
```

仓库中不得包含：

- 订阅 URL、访问令牌和密码
- `state.json`、`clash-applied.json`、`ai-rules.json` 和日志
- 节点服务器地址或认证参数
- 真实节点名、本机用户名和本机路径
- 临时备份和发布包（`dist/`）

`scripts/leak-scan.py` 在检查中扫描密钥；发布包构建时 `scripts/build_package.py` 另外扫描
个人信息。如果秘密曾经进入 Git 历史，仅删除当前文件不够；应先撤销或轮换秘密，再清理历史。

## 建议的仓库选项

在仓库 Settings 中：

1. 确认 Visibility 为 Private。
2. 开启 Issues。
3. 保持 Actions 开启，运行 `.github/workflows/ci.yml`。
4. 开启可用的 secret scanning / push protection。
5. 为 `main` 建立规则：合并前要求检查通过；个人项目可先不要求第二人审批。
6. 不启用 GitHub Pages，避免误发布本地看板。

## 远程不是唯一备份

GitHub 保存代码历史和发布包，但不保存运行状态、订阅凭据和 Clash 配置。这些只在本机，
由安装器和设置页的本地备份保护。
