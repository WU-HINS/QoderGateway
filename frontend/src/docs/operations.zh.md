# 运维

本页记录运行 QoderGate 时最常用的维护动作。

## SQLite 存储位置

QoderGate 的运行数据默认保存在：

```text
~/.qoder/qoder2api.db
```

设置 `QODER_DATA_DIR` 后改为 `$QODER_DATA_DIR/qoder2api.db`（容器中为 `/data/qoder2api.db`）。

数据库包含账号、允许的 API Key 和全局设置。

## Docker 运行

镜像以非 root 用户（uid 10001）运行，数据卷挂载在 `/data`：

```bash
docker run -d --name qodergate \
  -p 5050:5050 \
  -v qodergate-data:/data \
  -e QODER_ADMIN_PASSWORD=your-strong-password \
  ghcr.io/wu-hins/qodergateway:latest
```

查看日志与健康状态：

```bash
docker logs -f qodergate
docker inspect --format '{{.State.Health.Status}}' qodergate
```

> 镜像内置 HEALTHCHECK，探测 `/console`。若持续 unhealthy，多为端口或启动异常。

## 代理

出站代理**只**由 `QODER_PROXY` 控制，不读取 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` 等环境变量，行为完全可预期。

国内访问 `qoder.sh` 必须配置；聊天、鉴权、token 刷新、配额查询、注册机浏览器与临时邮箱 API 都会走该代理。

```bash
# 容器内访问宿主机代理
-e QODER_PROXY=http://host.docker.internal:7890
```

Linux 上需加 `--add-host=host.docker.internal:host-gateway`。

## 临时邮箱

注册机需要一个收验证码的邮箱后端。**推荐在控制台里配置**：Register 页签 →「临时邮箱配置」，
填写后保存即生效，无需重启服务。设置写入本地 SQLite（位于 `/data`，随数据卷持久化）。

- 后端由下拉框选择：`auto` / `cloudflare` / `yyds`
- 敏感项（管理员密码、站点密码、Token）在界面上以掩码显示，不会回传明文；留空表示保持原值
- 保存后点「测试连接」会真实创建一个临时邮箱，用于确认配置可用
- 环境变量（`CF_TEMP_EMAIL_*`、`YYDS_API_KEY`）仍可作为首次部署的默认值，控制台保存过的值优先

推荐自建 [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email)：
在其 `wrangler.toml` 配置 `ADMIN_PASSWORDS` 后，本项目会走 `/admin/new_address`，
可绕过 Turnstile 与「禁止匿名创建」限制；若站点启用了 `SITE_PASSWORD`，把该密码填入「站点密码」即可。

## 容器内运行注册机

Qoder 注册的人机验证是阿里云滑块，**必须由人拖动**，headless 下无法完成。本项目把注册机浏览器的实时画面嵌进 Web 控制台，直接在页面上完成验证：

```bash
docker run -d --name qodergate \
  -p 5050:5050 \
  --shm-size=1g \
  -v qodergate-data:/data \
  -e QODER_ADMIN_PASSWORD=your-strong-password \
  ghcr.io/wu-hins/qodergateway:latest
```

在控制台 **Register** 页签启动注册机，页面出现「远程浏览器」面板后，等滑块出现直接在画面上拖动即可，**无需 VNC、无需额外端口**。

实现：复用 DrissionPage 的 CDP 调试通道，用 `Page.captureScreenshot` 取画面经 WebSocket 推给前端 canvas，鼠标/键盘事件通过 `Input.dispatch*` 回放。容器仍启动 Xvfb（有头 Chromium 需要 X display），**不安装也不暴露 VNC**。

- `QODER_REMOTE_BROWSER_FPS`（默认 8）、`QODER_REMOTE_BROWSER_QUALITY`（默认 60）可调帧率与画质
- `QODER_REMOTE_BROWSER=0` 关闭该功能
- **必须加 `--shm-size=1g`**：默认 64MB 的 `/dev/shm` 会让 Chromium 渲染进程崩溃
- 容器内 Chromium 以 `--no-sandbox` 运行（容器通常无 `CAP_SYS_ADMIN`），无需 root

> **安全提示**：远程浏览器等同于把浏览器完全控制权交给控制台使用者，请务必修改默认密码，勿将控制台暴露公网。

## 注册机并发与磁盘

每个注册子任务都会拉起一个独立的 Chromium 实例。Chromium 冷启动需要读取
数百 MB 文件，因此并发数直接决定磁盘压力：

| 母线程数 | 每批并发 Chromium 数 | 适用环境 |
|---|---|---|
| 1（默认） | 3 | 机械硬盘 / 低配 VPS |
| 2 | 6 | SSD |

在机械硬盘上跑高并发会出现：系统负载飙升、IO 等待时间剧增，
浏览器因启动超时而报 `BrowserConnectError`。

如果遇到该错误，依次检查：

1. **降低并发**：母线程数设为 1
2. **容器加 `--shm-size=1g`**：否则渲染进程易崩溃
3. **确认 Xvfb 在运行**：容器入口脚本会自动启动，日志里应有 `X server ready`
4. **确认 `/usr/bin/chromium` 可执行**

代码已把浏览器连接重试放宽到 10 次 × 3 秒（约 30 秒），以容纳较慢的冷启动。

## 数据卷属主

镜像声明了 `VOLUME /data`。Docker 创建新卷时属主是 `root`，会覆盖构建期的
`chown` 结果，导致非 root 用户无法写数据库（`sqlite3.OperationalError: unable to
open database file`）。

因此容器**以 root 启动入口脚本**，由它先把 `$QODER_DATA_DIR` 归属到运行用户
（默认 uid/gid 10001），再用 `setpriv` 降权重新执行；之后 Xvfb、窗口管理器、
网关全部以非 root 运行。挂载命名卷无需任何额外操作。

绑定挂载（`-v /host/path:/data`）时宿主机目录属主不会被自动修正，请先执行：

```bash
sudo chown -R 10001:10001 /host/path
```

运行身份可用 `QODER_UID` / `QODER_GID` 覆盖。

## 备份

停止服务后复制数据库文件：

```bash
# 本地
cp ~/.qoder/qoder2api.db ~/qoder2api.db.backup

# 容器（数据卷名为 qodergate-data）
docker run --rm -v qodergate-data:/data -v "$PWD:/backup" alpine \
  cp /data/qoder2api.db /backup/qoder2api.db.backup
```

## 重置 Gateway Token

网关 Token 保存在 `settings` 表里的 `gateway_token` 字段。

注意：**设置了 `QODER_ADMIN_PASSWORD` 环境变量时，它以最高优先级覆盖数据库值**，此时在控制台里修改密码不会生效。如需在控制台管理密码，请移除该环境变量。

如果忘记密钥，可以直接修改 SQLite，或者删除数据库让程序重新初始化默认配置。

## Token 刷新

服务每 6 小时自动刷新全部启用账号的 token。刷新失败会打印到服务日志（`[tokens] scheduled refresh: ...`）。若账号 `dt-` 过期且刷新失败，该账号会持续报 401/403 并触发轮转，请检查 `QODER_PROXY` 是否可用。

## 常见问题

### 401 Unauthorized

- 管理接口：检查 `X-Gateway-Token`。
- API 接口：检查 `Authorization: Bearer <key>`。

### No Active Session

在 Dashboard 导入账号或添加 PAT。

### Account Quota Exceeded

禁用额度耗尽的账号，或者导入更多账号让自动轮转继续工作。

### 容器启动即退出

优先检查前端静态资源是否构建：镜像构建阶段已包含冒烟测试，若构建通过仍失败，多半是挂载覆盖了 `/app/src/qoder2api/static`。

### Local Auth Import Failed

确认本机已经登录过 Qoder CLI，并且本地 auth 文件存在。容器内没有宿主机的 `~/.qoder/.auth`，请改用 PAT 或批量导入。