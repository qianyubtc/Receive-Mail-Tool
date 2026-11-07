# 收码台服务器版

网页可导入邮箱、手动监听并显示验证码，也可以配置收信和 Webhook。业务端提交一个已导入的邮箱后，服务端会监听并通过 Webhook 发送 `{"code":"042681"}`。网页和 API 共用一个当前任务，开启新任务会替换上一个任务。

## 启动

服务器需要 Python 3.9 或更新版本，推荐 Python 3.14，以使用标准库的 IMAP IDLE。程序默认监听 `127.0.0.1:8770`。业务程序在同一台机器时可直接调用。跨机器调用时，通过服务器现有的 HTTPS 反向代理转发到这个地址。每个数据库运行一个服务进程。

分别生成 API 令牌和管理令牌。以下命令运行两次。

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

把生成的值分别配置到服务器进程的环境变量。

```sh
export MAILTOOL_API_TOKEN='YOUR_RANDOM_API_TOKEN'
export MAILTOOL_ADMIN_TOKEN='YOUR_RANDOM_ADMIN_TOKEN'
python3 mailtool_server.py serve
```

## Web 管理页

同机打开 `http://127.0.0.1:8770/admin`，跨机器访问时打开 HTTPS 域名下的 `/admin`。用 `MAILTOOL_ADMIN_TOKEN` 登录。

1. 点 **导入邮箱**，粘贴邮箱清单或选择 TXT 文件。每行一个邮箱，格式与桌面版相同。
2. 选中邮箱，点 **开始监听**，再触发验证码发送。收到后可在页面复制验证码。
3. 点 **收信设置** 修改协议、补查间隔、监听时长、Webhook 地址或签名密钥。设置保存到数据库，从下一次监听开始生效。

```text
demo@example.com----unused----YOUR_CLIENT_ID----YOUR_REFRESH_TOKEN
```

网页手动监听只在管理页显示结果。业务 API 启动的监听在管理页显示结果，并向 Webhook 推送。验证码在当前任务中最多显示 5 分钟。导入同一个邮箱会更新它的凭证，并停止该邮箱正在运行的任务。

### 命令行导入

```sh
python3 mailtool_server.py import /path/to/accounts.txt
```

默认数据库位于 `.mailtool/server.sqlite3`。导入和启动也可以传同一个 `--data /path/to/server.sqlite3`，参数放在子命令之前。数据库内含邮箱刷新令牌与保存的回调配置。

### Webhook 配置

业务 API 收码前，在管理页设置 Webhook 地址和独立的签名密钥。也可以用以下环境变量提供初始设置。

```sh
export MAILTOOL_WEBHOOK_SECRET='YOUR_RANDOM_WEBHOOK_SECRET'
export MAILTOOL_WEBHOOK_URL='https://your-service.example/webhooks/mail-code'
```

签名密钥可以用前面的随机令牌命令单独生成。远程回调使用 HTTPS，同机联调可以用 `http://127.0.0.1`。网页保存过的配置会覆盖初始配置。启动时省略管理令牌可关闭管理页，此时须预先配置 Webhook。

默认先尝试 IMAP，IMAP 无法使用时切换到 Graph。Python 3.14 下，如果邮箱连接支持 IDLE，收件箱变更会触发查收，同时定期补查垃圾邮件。较早的 Python 使用同一条 IMAP 连接做增量查询。Graph 也按任务开始时间查新邮件。默认补查间隔 2 秒，监听时长 120 秒，可通过 `serve --interval` 和 `serve --timeout` 调整。也可以用 `--protocol graph` 或 `--protocol imap` 固定协议。

## 调用

```sh
curl -fsS -X POST http://127.0.0.1:8770/v1/watches \
  -H "Authorization: Bearer $MAILTOOL_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"email":"demo@example.com"}'
```

服务端返回 HTTP 202 和任务 ID。业务端保存该 ID，再触发验证码发送。

```json
{"watch_id":"11111111111111111111111111111111"}
```

收到码就结束收信。提交下一个邮箱时，上一个任务的结果和后续重试作废。已经发起的旧回调可能晚到，业务端只处理 `X-Watch-ID` 等于当前任务的请求。

服务端按微软记录的收件时间筛选，只回传明确识别的验证码。IMAP 收件时间精确到秒，任务启动同一秒到达的邮件会进入筛选范围。服务运行期间会对近期处理过的邮件去重。每次成功创建任务都会分配新的 ID，包括重复提交同一个邮箱。

使用同一个 Bearer 令牌调用 `GET /v1/watches/{watch_id}` 查询状态。状态为 `listening`、`delivering`、`received`、`delivered`、`replaced`、`cancelled`、`timeout`、`failed` 或 `delivery_failed`。未配置 Webhook 时，创建业务 API 任务返回 HTTP 409 和 `webhook_not_configured`。任务状态和待重试回调保存在进程内存，重启后需要重新创建任务。

创建任务时可提供一个新的 32 位小写十六进制 `watch_id`。使用业务 API 密钥调用 `DELETE /v1/watches/{watch_id}`，可停止该 ID 对应的当前业务监听。已被替换的任务和管理页手动监听不会受到影响。关闭连接早于创建响应时，调用方也可以用预分配的 ID 取消。

## 接收回调

Webhook 接收一个 JSON POST，正文只有字符串类型的验证码，保留前导零。

```json
{"code":"042681"}
```

请求头带 `X-Watch-ID`、`X-Webhook-Timestamp`（Unix 秒）和 `X-Webhook-Signature`。用 `MAILTOOL_WEBHOOK_SECRET` 对以下字节计算 HMAC-SHA256，十六进制结果前加 `sha256=`。签名校验必须使用收到的原始正文。

```python
import hashlib
import hmac

signed = f"{timestamp}.{watch_id}.".encode("ascii") + raw_body
expected = "sha256=" + hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
valid = hmac.compare_digest(expected, signature)
```

接收方还应核对时间戳在允许范围内，并按 `X-Watch-ID` 去重。处理后返回任意 2xx 状态码。请求失败时服务端最多重试三次，重试使用同一个任务 ID。
