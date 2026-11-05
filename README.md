# Receive Mail Tool · 收码台

收码台是一款在本机运行的 Hotmail / Outlook.com 邮件查收工具。它通过 Microsoft Graph 或 IMAP 读取已授权的邮箱，按批监听并显示最近送达的一条验证码。

- **批量管理**。导入 TXT，按编号或邮箱地址搜索，分批监听。每批最多 20 个邮箱。
- **查收验证码**。检查收件箱和垃圾箱，自动提取验证码。支持一键复制邮箱或验证码，也可以打开邮件详情核对。
- **本机运行**。桌面窗口采用双栏布局，窄窗口使用上方验证码、中间可滚动列表、底部操作区。运行只需要 Python 标准库。

## 项目缘起

我们最初从 wuemail.cn 购买了一批 Hotmail / Outlook.com 邮箱。为了集中查收这些邮箱的验证码，我们开发了收码台，并适配了该服务商的四段式账号格式。[服务商商品页（商品 120）](https://wuemail.cn/item/120)

## 快速开始

安装 [Python 3.9 或更高版本](https://www.python.org/downloads/)，然后克隆仓库，也可以在 GitHub 的 **Code → Download ZIP** 下载源码并解压。

```sh
git clone https://github.com/qianyubtc/Receive-Mail-Tool.git
cd Receive-Mail-Tool
```

| 平台 | 启动方式 |
| --- | --- |
| Windows | 双击 `启动收码工具.bat`，或运行 `py -3 mailtool.py --open`。 |
| macOS / Linux | 运行 `python3 mailtool.py --open`。macOS 也可双击 `启动收码工具.command`。 |

启动后访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。保留终端窗口即可持续收信。如果端口已被占用，在启动命令末尾加 `--port 8766`。

## 导入邮箱并监听

使用本人拥有或获授权管理的 Microsoft 个人邮箱。准备同一个 OAuth 应用的 `client_id` 和该邮箱的 `refresh_token`，再点击 **导入邮箱**，粘贴内容或选择 TXT 文件。每行一个邮箱，四段用 `----` 分隔。

```text
demo@example.com----unused----YOUR_CLIENT_ID----YOUR_REFRESH_TOKEN
```

第二段是兼容字段，可以留空。程序不使用邮箱密码登录，也不保存密码。示例中的邮箱、应用 ID 和令牌仅作占位。

Graph 需要能读取邮件正文的委派权限，例如 `Mail.Read`。IMAP 需要 Outlook 的 `IMAP.AccessAsUser.All` 权限。获取刷新令牌时申请 `offline_access`。详见 [Microsoft Graph 邮件权限](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0#permissions) 和 [Microsoft IMAP OAuth 指南](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)。

1. 勾选本批要查收的邮箱，点击 **开始监听**。切换到其他标签页时，本机服务仍会继续查收。
2. 在 **本批最新验证码** 查看结果。复制邮箱后，列表会高亮最近复制的那一行。
3. 本批用完后点击 **完成本批 → 下一批**。工具会标记本批已用并选中下一批未用邮箱。如果监听正在运行，工具会自动监听下一批。
4. 点击邮箱行可查看最近邮件，也可单独查收。在 **管理邮箱** 里可删除、恢复或彻底移除本地记录。

## 收信设置与排查

| 情况 | 操作 |
| --- | --- |
| 想缩短查收等待 | 在 **收信设置** 将间隔从默认的 10 秒改为 5 秒，再点 **重启本批**。间隔按单个邮箱完成查询后计算，实际送达时间还受网络、排队和微软服务影响。 |
| 邮件在启动监听前已收到 | 勾选 **显示监听前 10 分钟的邮件**，或打开该邮箱的最近邮件。 |
| 授权失效或缺少权限 | 核对所选协议的授权，重新获取刷新令牌并导入同一个邮箱。 |
| 邮件到了，验证码未显示 | 打开邮件详情核对正文。工具按内容识别常见的数字或字母数字验证码，部分格式可能无法识别。 |

同一个浏览器会记住上次成功启动监听时使用的协议和间隔。收到微软限流响应后，后台会按服务端要求延后重试。

## 数据与诊断

主页面仅在本机开放。收信请求直接发往微软，读取邮件时不会标记已读。账号、刷新令牌、邮件缓存和邮箱状态保存在项目目录下的 `.mailtool/accounts.sqlite3`。数据库未加密，请勿上传或共享 `.mailtool`。升级时先关闭工具，再把旧版 `.mailtool` 复制到新版程序目录。

Windows 下可通过 `启动局域网诊断.bat` 临时开启只读诊断。诊断默认使用 8767 端口，读取日志需要启动时生成的访问码。诊断日志记录操作阶段、耗时和错误类别，排除邮箱地址、令牌、验证码和邮件内容。

## 构建 Windows 免安装包

```sh
python3 scripts/build_windows.py
```

Windows 上可将 `python3` 换成 `py -3`。构建脚本下载官方 Python x64 嵌入式运行时并校验 SHA-256，输出 `dist/收码台-Windows-简洁版-x64.zip`。将整个 ZIP 解压后双击 `启动收码工具.bat`，即可在 Windows 10 / 11 x64 上使用内置 Python。详细步骤见 [Windows 使用说明](Windows使用说明.txt)。

## 开发与测试

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Python 测试使用模拟邮箱和临时数据库，覆盖验证码提取、批次切换、令牌刷新和诊断脱敏。Node.js 仅用于检查前端脚本语法。

## 许可证

项目源码采用 [MIT 许可证](LICENSE)。Windows 包内的 Python 运行时许可位于 `runtime/LICENSE.txt`。
