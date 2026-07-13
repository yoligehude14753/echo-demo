"""Focused deterministic memory-association ranking tests."""

from app.memory.ranking import lexical_relevance


def test_long_query_preserves_exact_mixed_identifier_match() -> None:
    query = "请根据历史会议中关于 RTX5080 的讨论给出核心结论，并关联记忆来源"
    content = (
        "会议《RTX5080显卡与超绒服新品座谈》纪要："
        "会议介绍了新款RTX5080显卡的旗舰特性和限量供应情况。"
    )

    assert lexical_relevance(query, content) >= 0.86


def test_nearby_model_number_does_not_false_match() -> None:
    assert lexical_relevance("RTX5080", "RTX5090 是另一款显卡") < 0.28


def test_bare_number_does_not_false_match_product_identifier() -> None:
    assert lexical_relevance("RTX5080", "本次采购预算是 5080 元") < 0.28
