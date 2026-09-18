# GitHub 私有仓库初始化

## Git 与 GitHub 的区别

- Git 是本机版本数据库。即使没有网络，也能提交、比较和回滚。
- GitHub 是远程托管、异地备份、Issue、Pull Request 和自动检查平台。
- 本项目已经有本地 Git；尚未创建 GitHub 远程仓库。

## 推荐设置

| 项目 | 设置 |
|---|---|
| 所有者 | `z13141557217-web` |
| 仓库名 | `steadyroute` |
| 可见性 | Private |
| 默认分支 | `main` |
| 协议 | HTTPS，由 GitHub CLI 管理凭据 |
| Issues | 开启 |
| Actions | 开启 |
| Wiki/Pages | 暂不需要 |

## 第一次登录

当前 GitHub CLI 令牌失效，需要重新认证：

```bash
gh auth login -h github.com
```

依次选择：

1. GitHub.com
2. HTTPS
3. 使用浏览器登录
4. 同意让 GitHub CLI 配置 Git 凭据

验证：

```bash
gh auth status
```

不要把网页密码、验证码或令牌粘贴到聊天、文档或源码中。

## 创建私有仓库并首次推送

在项目目录执行：

```bash
cd /Users/nurture/Projects/steadyroute
gh repo create steadyroute --private --source=. --remote=origin --push
git push origin v0.1.0
```

验证：

```bash
git remote -v
git status --short --branch
gh repo view --web
```

预期远程地址：

```text
https://github.com/z13141557217-web/steadyroute
```

## 安全检查

首次推送前必须确认：

```bash
./scripts/check.sh
git status --short
git log --oneline --decorate -5
```

不得包含：

- 订阅 URL
- 访问令牌和密码
- `state.json`
- 生产日志
- 节点服务器地址或认证参数
- 临时备份和发布包

如果秘密曾经进入 Git 历史，仅删除当前文件不够；应先撤销/轮换秘密，再清理历史。

## 建议的 GitHub 设置

创建后进入仓库 Settings：

1. 确认 Visibility 为 Private。
2. 开启 Issues。
3. 保持 Actions 开启，以运行现有 CI。
4. 开启可用的 secret scanning / push protection。
5. 为 `main` 建立规则：合并前要求检查通过；个人项目可先不要求第二人审批。
6. 不启用 GitHub Pages，避免误发布本地看板。

## 远程并不是唯一备份

GitHub 保存代码历史，但不保存生产 `state.json`、订阅凭据和系统配置。正式发布仍要保留本地版本化生产备份与恢复演练。

