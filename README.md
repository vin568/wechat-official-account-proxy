# 微信公众号 API 出口代理

阿里云函数计算上的 HTTP 函数。调用方把 `https://api.weixin.qq.com` 换成函数地址，路径、方法、query、body 原样转到微信。函数自己申请并缓存稳定版 `access_token`，出口固定为一枚 EIP，供公众号 IP 白名单使用。

代码只有 `index.py`，Python 3.12 标准库，没有依赖，也不要配层。

## 行为

- 调用方带 `Authorization: Bearer <PROXY_KEY>`。这个头会在转发前删掉。
- 只转发 `/cgi-bin/` 和 `/wxa/`。`/cgi-bin/token`、`/cgi-bin/stable_token` 直接 403，AppSecret 不出函数。
- query 里没有 `access_token` 时自动补上。微信返回 `40001` 或 `42001` 时强制刷新一次再重放。调用方自己带了 token 则不替换、也不刷新。
- 不跟随上游重定向，不读取 `HTTP_PROXY` / `HTTPS_PROXY`，避免出口绕过 NAT EIP。

调用方不要再自己请求 token 接口。会自动换 token 的 SDK，关掉它的刷新，或者让它走自定义 HTTP 客户端、且不要带 `access_token`。

## 部署

本机 `~/.aliyun/config.json` 是空的，CLI 不可用。用控制台部署。

### 1. 创建函数

打开 [函数计算 3.0 控制台](https://fcnext.console.aliyun.com)，地域选国内，建议华东 1（杭州），和调用方同地域。

| 项 | 值 |
|---|---|
| 函数名 | `wechat-oa-proxy` |
| 运行环境 | 内置运行时 Python 3.12 |
| 请求处理程序 | `index.handler` |
| 触发器 | HTTP，方法全选，认证方式「无需认证」 |
| 内存 | 512 MB |
| 执行超时 | 60 秒 |
| 单实例并发 | 1 |

匿名触发器本身没有鉴权，密钥校验在函数里。内置域名 `*.fcapp.run` 只给服务端调用，不要当网站用。

### 2. 上传代码

在线编辑器新建 `index.py`，用仓库里的文件覆盖示例代码。或者打 zip，**根目录直接是 `index.py`**，不要再套一层文件夹。

处理程序必须是 `index.handler`。文件名和模块名对不上时，调用会 502。

### 3. 环境变量

| 键 | 值 |
|---|---|
| `WECHAT_APP_ID` | 公众号 AppID |
| `WECHAT_APP_SECRET` | AppSecret |
| `PROXY_KEY` | 长随机串。调用方放在 `Authorization: Bearer` 里 |

不要把这三个值写进仓库。改完环境变量后重新部署，避免旧实例还拿着旧密钥。

生成 `PROXY_KEY`：

```bash
openssl rand -hex 32
```

### 4. 固定出口 IP

配置 → 高级配置 → 编辑 → 网络：

1. 允许访问 VPC：是。没有现成 VPC 就让控制台新建。
2. 固定公网 IP：开启。确认弹窗。这一步会创建公网 NAT 网关、EIP 和 SNAT，大约 1–2 分钟。
3. 允许函数默认网卡访问公网：先保持「是」。

NAT 和 EIP 只在部分可用区可买。和函数不在同一可用区时，按控制台提示配跨可用区交换机。同地域的其他函数可以共用这枚 EIP。

然后临时把函数代码换成下面这段，部署后点「测试函数」：

```python
import urllib.request

def handler(event, context):
    with urllib.request.urlopen("https://myip.ipip.net", timeout=10) as res:
        return res.read().decode()
```

返回的 IP 必须等于网络配置里那枚 EIP。对不上就再等一两分钟；仍然对不上，到专有网络控制台看 NAT 网关和 SNAT 是否已经绑上这枚 EIP。

这段探测没有鉴权。看完 IP 立刻把代码换回 `index.py` 并部署。

白名单填的是这枚 **出口 EIP**。函数触发器域名解析出来的是入口 IP，会轮换，填进去没有用。

公众号后台：

- 旧版：设置与开发 → 基本配置 → IP 白名单
- 新版微信开发者平台：对应业务的开发信息 → API IP 白名单

### 5. 关掉默认网卡的公网

白名单保存后，回到函数网络配置，把「允许函数默认网卡访问公网」改成「否」，部署。

不改的话，一部分请求仍从动态 IP 出去，`40164` 会间歇出现。改完再跑一次探测，确认出口还是同一枚 EIP，然后务必换回 `index.py`。

## 验收

触发器页的公网地址形如 `https://wechat-oa-proxy-xxxx.cn-hangzhou.fcapp.run`。

```bash
# 401，代理自己的拒绝
curl -sS -D- -o- \
  -H "Content-Type: application/json" \
  -d '{"articles":[]}' \
  "https://wechat-oa-proxy-xxxx.cn-hangzhou.fcapp.run/cgi-bin/draft/add"

# 403，token 接口不转发
curl -sS -H "Authorization: Bearer $PROXY_KEY" \
  "https://wechat-oa-proxy-xxxx.cn-hangzhou.fcapp.run/cgi-bin/stable_token"

# 链路通：微信的业务错误 JSON。不应是 401、403，也不应是 40164
curl -sS -H "Authorization: Bearer $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"articles":[]}' \
  "https://wechat-oa-proxy-xxxx.cn-hangzhou.fcapp.run/cgi-bin/draft/add"
```

判读：

| 结果 | 含义 |
|---|---|
| HTTP 401，`unauthorized` | `PROXY_KEY` 不对或没带 |
| HTTP 500，`PROXY_KEY is not configured` 或 `wechat credentials are not configured` | 环境变量没配上 |
| HTTP 403，`path not allowed` | 路径不在 `/cgi-bin/`、`/wxa/`，或打到了 token 接口 |
| HTTP 502，body 里 `errcode` 40164 | 出口 IP 不在白名单，或默认网卡仍在走动态 IP。`errmsg` 里的 IP 才是微信看到的出口 |
| HTTP 502，body 里 `errcode` 40013 | AppID 或 AppSecret 错 |
| HTTP 200，body 里是微信业务 `errcode` | 链路通了。空 `articles` 会报参数错误，这是预期 |

函数日志只记 path、状态码、`errcode`、request id，不记 token 和 secret。502 时用响应头 `X-Fc-Request-Id` 去对日志。

## 调用

```text
https://api.weixin.qq.com/cgi-bin/draft/add
→ https://<函数域名>/cgi-bin/draft/add
```

加上 `Authorization: Bearer <PROXY_KEY>`。不要自己拼 `access_token`，除非你明确想用另一套凭证。

封面、正文图、草稿都走同一个前缀，例如：

- `POST /cgi-bin/media/uploadimg`
- `POST /cgi-bin/material/add_material`
- `POST /cgi-bin/draft/add`

二进制上传保持原来的 `multipart/form-data`，不要先改成 JSON。

## 限制

- 这不是 CONNECT 隧道。微信 API 是 HTTPS，函数计算解不开 TLS，只能换 host。
- 同步请求 body 最大 32 MB。请求头合计最大 8 KB，path 加 query 最大 4 KB。
- 函数计算会把同名 query 和同名请求头合并成逗号分隔的一个值。微信 API 不依赖重复字段。
- 响应头里的 `content-length`、`date`、`server`、`content-disposition` 会被函数计算丢掉。默认 `*.aliyuncs.com` / `*.fcapp.run` 域名还会强制 `content-disposition: attachment`，服务端调用不受影响。
- `draft/add` 不幂等。超时后不要自动重试，否则会出重复草稿。token 失效时函数内部的那一次重放是唯一例外。
- 冷启动会让第一次调用多几百毫秒到数秒。草稿频率不必开最小实例。

## 费用

函数调用费可以忽略，最小实例保持 0。公网 NAT 网关按实例费加 CU 持续计费，EIP 另计，都跟有没有调用无关。只为白名单养这套网络时，先看一眼 NAT 和 EIP 的月费。
