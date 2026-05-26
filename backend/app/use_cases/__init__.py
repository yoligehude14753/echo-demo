"""Use Cases 层：业务流程编排。

约束（架构 Fitness Function 强制）：
- 只允许 import：app.ports, app.schemas, app.config, 标准库, pydantic, tenacity
- 严禁 import：app.adapters, openai, anthropic, sqlalchemy（直接）, fastapi
"""
