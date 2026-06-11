"""echo-gateway: 面向外部用户的 OpenAI 兼容鉴权反向代理。

把 yunwu(主 LLM) + heyi-bj(fast LLM / STT / TTS) 统一收口到一个带 Bearer token
鉴权 + 限流的服务端网关。客户端只持有自己的 client token，真实上游凭证（yunwu key、
heyi 地址）只存在于网关运行环境，永不下发到客户端。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
