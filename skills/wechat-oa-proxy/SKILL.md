---
name: wechat-oa-proxy
description: 通过已部署的微信公众号出口代理调用 api.weixin.qq.com。Use when 创建或查询公众号草稿、上传封面或正文图片、转发 /cgi-bin 或 /wxa 接口，或用户提到 PROXY_KEY、WECHAT_PROXY_BASE_URL、wechat-oa-proxy、草稿箱。
---

# 使用微信公众号出口代理

代理代替调用方访问 `https://api.weixin.qq.com`，并自己申请 `access_token`。把官方 URL 的 host 换成代理地址，路径、方法、query、body 保持不变。

## 凭据

只从环境变量读取，缺少任一个就停止，让用户补上。

| 变量 | 用途 |
|---|---|
| `WECHAT_PROXY_BASE_URL` | 函数 HTTP 触发器地址，无末尾斜杠 |
| `PROXY_KEY` | 放进 `Authorization: Bearer` |

不要把这两个值写进代码、命令记录、日志、提交或回复。不要读取或索要 `WECHAT_APP_ID`、`WECHAT_APP_SECRET`。不要请求 `/cgi-bin/token` 或 `/cgi-bin/stable_token`，也不要自己拼 `access_token`。

## 请求

```text
https://api.weixin.qq.com/cgi-bin/draft/add
→ $WECHAT_PROXY_BASE_URL/cgi-bin/draft/add
```

每次请求都带：

```text
Authorization: Bearer $PROXY_KEY
```

二进制上传保持原来的 `multipart/form-data`，不要先改成 JSON。只转发 `/cgi-bin/` 和 `/wxa/`。

## 不要调用

代理对以下路径直接返回 403，而且不会向微信取 token：

- `/cgi-bin/token`
- `/cgi-bin/stable_token`
- `/cgi-bin/freepublish/submit`
- `/cgi-bin/freepublish/delete`

后两个会发布文章或删除已发布文章。即使用户要求，也不要绕过代理直接调用微信。`/cgi-bin/freepublish/get` 和 `/cgi-bin/freepublish/batchget` 可以转发。

## 新建草稿

一张草稿由 1 到 8 篇图文组成。按这个顺序，每一步都等上一步返回后再继续：

1. 封面：`POST /cgi-bin/material/add_material?type=image`，表单字段名是 `media`。jpg/png，小于 1 MB。响应里的 `media_id` 就是 `thumb_media_id`。
2. 正文图片：`POST /cgi-bin/media/uploadimg`。只把响应里的微信 URL 写进 HTML。外链图片会被微信丢掉。
3. 草稿：`POST /cgi-bin/draft/add`，body 是 `{"articles":[...]}`。图文字段以[新增草稿](https://developers.weixin.qq.com/doc/subscription/api/draftbox/draftmanage/api_draft_add.html)为准。`title`、`content`、`thumb_media_id` 必填。

`draft/add` 不幂等。超时、连接中断或响应不完整时不要重试，先用 `GET /cgi-bin/draft/batchget` 检查是否已经生成，避免重复草稿。

## 判读

| 结果 | 处理 |
|---|---|
| HTTP 200，业务 JSON 含 `media_id` 或 `total_count` | 成功。草稿 id 是 `draft/add` 返回的 `media_id` |
| HTTP 200，微信 `errcode` 非 0 | 按 `errmsg` 改请求内容。不要重试 `draft/add` |
| HTTP 401 `unauthorized` | `PROXY_KEY` 缺失或错误 |
| HTTP 403 `path not allowed` | 路径不允许，或打到了上面的四个禁路 |
| HTTP 502，`errcode` 40164 | 出口 IP 不在公众号白名单。把 `errmsg` 里的 IP 告诉用户，不要换 IP 重试 |
| HTTP 502，`errcode` 40013 或 40125 | 告诉用户检查函数上的 AppID / AppSecret |
| HTTP 500，`PROXY_KEY is not configured` 或 `wechat credentials are not configured` | 函数环境变量缺失 |

响应头里的 `X-Fc-Request-Id` 可以交给用户查函数日志。不要打印响应里的 `access_token`。
