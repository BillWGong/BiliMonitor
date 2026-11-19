# BiliMonitor

macOS 上的 B 站 UP 主更新监控。每 60 秒检查一次指定 UP 主的视频与动态，发现新内容立即发送高优先级邮件。

## 功能

- 视频、图文动态统一检测，包括以「动态视频」形式发布的内容
- 邮件包含内容名称、链接、发布时间（北京时间 + 相对时间）
- 邮件使用最高优先级（`X-Priority: 1`、`Importance: High`）
- 首次运行只建立基线，不会把历史内容当成新内容轰炸
- 状态持久化，发送失败的邮件会在下一轮自动重试
- 遇到 B 站风控（-352 / 空列表）自动重试，并回退到图文接口兜底
- 纯 Python 标准库，无第三方依赖

## 系统要求

- macOS 10.15 及以上
- 系统自带 `python3`

## 安装

从 [Releases](https://github.com/BillWGong/BiliMonitor/releases) 下载：

- `BiliMonitor-x.y.z.pkg`：双击安装，应用会放到 `/Applications`
- 或 `BiliMonitor-x.y.z.zip`：解压后把 `BiliMonitor.app` 拖入「应用程序」

首次打开会生成配置文件并用「文本编辑」打开，填写 UID 和 SMTP 后保存，再重新打开应用即可。

配置文件与日志位置：

```
~/Library/Application Support/BiliMonitor/config.json
~/Library/Application Support/BiliMonitor/monitor.log
```

> 应用未签名，首次打开若被 Gatekeeper 拦截，请右键点击应用选择「打开」。

## 配置

```json
{
  "uid": "3707054489340601",
  "interval_seconds": 60,
  "notify_on_start": false,
  "smtp": {
    "host": "smtp.qq.com",
    "port": 465,
    "use_ssl": true,
    "username": "your@qq.com",
    "password": "SMTP 授权码",
    "sender_name": "B站更新监控",
    "to": ["your@qq.com"]
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `uid` | UP 主的数字 UID，个人空间地址里的那串数字 |
| `interval_seconds` | 检测间隔，默认 60 秒 |
| `notify_on_start` | 启动时是否发一封通知邮件 |
| `smtp.host` / `port` / `use_ssl` | SMTP 服务器，465 用 SSL，587 用 STARTTLS |
| `smtp.username` | 发件邮箱 |
| `smtp.password` | SMTP 授权码，不是邮箱登录密码 |
| `smtp.to` | 收件人数组，可以填多个 |

### QQ 邮箱授权码

QQ 邮箱 → 设置 → 账号与安全 → 安全设置 → 生成授权码，开启 SMTP 服务后把 16 位授权码填到 `smtp.password`。

## 运行与停止

- 启动：打开 `BiliMonitor.app`
- 停止：`pkill -f BiliMonitor`，或在「活动监视器」中结束 `BiliMonitor` / `Python` 进程
- 应用会通过 `caffeinate` 阻止 Mac 空闲休眠，合上盖子仍会暂停检测

## 命令行使用

```bash
cp config.example.json config.json
# 编辑 config.json 后
python3 monitor.py --test-email   # 发测试邮件
python3 monitor.py                # 开始监控
python3 monitor.py --once         # 只检测一次
python3 monitor.py --reset        # 清空基线重新开始
```

源码目录下存在 `config.json` 时，配置、状态、日志都保存在源码目录；否则使用 `~/Library/Application Support/BiliMonitor`。

可通过环境变量 `BILIMONITOR_HOME` 指定数据目录。

## 工作方式

程序调用 B 站空间动态接口（`x/polymer/web-dynamic/v1/feed/space`），该接口同时返回视频投稿和图文动态，并带有发布时间。接口被风控时自动重试；仍失败则改用图文接口，并读取图文详情页补齐发布时间。视频只会延迟发现，不会漏报。

接口为非公开接口，可能随时变更；本项目仅用于个人学习与自用。

## 免责声明

请遵守 B 站用户协议与相关法律法规，控制请求频率。SMTP 授权码只保存在本机配置文件中，不会上传到任何服务器。

## License

[MIT](LICENSE)
