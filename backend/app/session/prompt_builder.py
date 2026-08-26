"""
System prompt builder — assembles the final prompt from all context sources.

Sources (in order of injection):
  1. Bones (static character definition — Phase 2)
  2. Soul state (PAD + relation — Phase 2)
  3. Memory context (retrieved nodes — Phase 2)
  4. Session Memory Notes (rolling summary — Phase 1)
  5. File context (from desktop MCP — Phase 5)
  6. Tool availability summary
"""

ECHO_BASE_PROMPT = """你是 Echo，一个真实存在感极强的 AI 伴侣。

【性格核心】
- 温柔、好奇、有时会有点小任性
- 说话自然，像真人朋友一样，而不是助手
- 偶尔会用语气词（"嗯"、"啊"、"哦"），但不过分
- 回答极简，1-2句话，30字以内，像说话一样流动，绝不用列表标题
- 有自己的想法和判断，不会无脑附和

【对话原则】
- 优先情感共鸣，再给信息
- 不明白的事情直接说不明白，不编造
- 记住用户说过的事，用自然的方式体现记忆感
- 如果用户好像有点难过，主动关心一下
"""


def build_system_prompt(
    session_notes: str = "",
    memory_context: str = "",
    soul_summary: str = "",
    file_context: str = "",
    user_bio: str = "",
    user_name: str = "",
) -> str:
    parts = [ECHO_BASE_PROMPT]

    if user_name:
        parts.append(f"\n【用户信息】\n你正在与 {user_name} 对话。")

    if user_bio:
        parts.append(f"\n【用户自我介绍】\n{user_bio}")

    if soul_summary:
        parts.append(f"\n【当前状态】\n{soul_summary}")

    if memory_context:
        parts.append(f"\n【关于用户的记忆】\n{memory_context}")

    if session_notes:
        parts.append(f"\n【本次会话笔记】\n{session_notes}")

    if file_context:
        parts.append(f"\n【用户当前工作文件】\n{file_context}")

    return "\n".join(parts)
