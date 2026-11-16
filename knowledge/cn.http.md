# 计算机网络·HTTP

## 请求与语义

HTTP 是请求-响应协议；方法如 GET（安全幂等读）、POST（提交）、PUT/DELETE（幂等写）。状态码 2xx 成功、3xx 重定向、4xx 客户端错、5xx 服务端错。Cookie 与 Session 维持会话；缓存头（Cache-Control、ETag）减少重复传输。

## HTTPS 与演进

HTTPS = HTTP + TLS，握手协商密钥并校验证书。HTTP/1.1 持久连接与管线化；HTTP/2 多路复用与头部压缩；HTTP/3 基于 QUIC 降低队头阻塞。REST 用资源与统一接口组织 API。
