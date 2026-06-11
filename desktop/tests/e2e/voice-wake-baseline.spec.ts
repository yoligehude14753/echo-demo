/**
 * 语音唤醒量化基线（纯函数，无浏览器交互）。
 *
 * 目的不是替代真人语音 E2E，而是把当前唤醒词规则的「文本层」表现固定成
 * 可重复指标：召回率、误唤醒率、续聊判定、今日回顾意图。后续每次调参都要
 * 先过这组 baseline，避免只靠主观听感。
 */
import { expect, test } from "@playwright/test";
import {
  containsWakeWord,
  extractEchoWakeCommand,
  isDailyRecapCommand,
  isLikelyEchoFollowup,
} from "../../src/lib/voiceWake";

interface Case {
  text: string;
  note?: string;
}

const WAKE_RECALL: Case[] = [
  { text: "echo帮我总结今天" },
  { text: "Echo，查一下 AI PC 市场" },
  { text: "echodesk 打开设置" },
  { text: "pico生成一个PPT" },
  { text: "aiko生成word语音验收报告" },
  { text: "aico，帮我查一下今天天气" },
  { text: "eiko 继续说" },
  { text: "ecko 帮我整理" },
  { text: "诶口，总结一下会议" },
  { text: "哎扣，提醒我下午三点开会" },
  { text: "艾口，生成一个PPT" },
  { text: "爱扣 查一下天气" },
  { text: "衣扣帮我写一份报告" },
  { text: "依口今天星期几" },
  { text: "伊扣继续" },
  { text: "易狗给我解释一下" },
  { text: "宜口打开今日回顾" },
  { text: "语狗今天星期几" },
  { text: "汉宜口今天星期几", note: "真链路实测乱码前缀" },
  { text: "汉语狗今天星期几", note: "真链路实测乱码前缀" },
  { text: "嘿一口，生成PPT" },
  { text: "诶，一口生成一个关于人工智能的 ppt" },
  { text: "嘿依co，今天星期几" },
  { text: "海鱼口，总结会议" },
  { text: "嗨依扣 生成PPT" },
  { text: "嘿，依co，查天气" },
  { text: "喂 echo 你在吗" },
  { text: "好的。Echo 生成报告", note: "句中下一句唤醒" },
  { text: "刚才那个方案不错。pico 帮我记一下", note: "句中下一句唤醒" },
  { text: "hey eco summarize it", note: "带 head 的歧义英文允许" },
];

const FALSE_POSITIVE_GUARD: Case[] = [
  { text: "我吃了一口饭就走了" },
  { text: "一口气把这件事做完了" },
  { text: "这个eco系统挺复杂的" },
  { text: "他的自我ego太强了" },
  { text: "今天天气不错我们出去走走" },
  { text: "随便聊聊最近的项目进度" },
  { text: "开口说话之前先想想" },
  { text: "出口在大楼的右边" },
  { text: "小狗很可爱我很喜欢" },
  { text: "可口可乐买一瓶" },
  { text: "门口有人在等你" },
  { text: "随口说说而已别当真" },
  { text: "折扣力度还挺大的" },
  { text: "胃口不太好" },
  { text: "路口左转就到了" },
  { text: "我跟echo说过这件事了" },
  { text: "刚才提到echo这个产品" },
  { text: "关于 EchoDesk 的文档在这里" },
  { text: "我们聊到 echo 这个词" },
  { text: "eco 模式会省电" },
  { text: "ego 不是一个好习惯" },
  { text: "iqoo 这款手机怎么样", note: "手机品牌，不应在非句首命令中误触" },
  { text: "我买了 iQOO 手机" },
  { text: "pico 是一款 VR 设备", note: "提及品牌，不是对 Echo 下指令" },
  { text: "aiko 这个名字挺可爱", note: "提及名字，不是对 Echo 下指令" },
  { text: "爱狗人士很多", note: "真实词，不是唤醒" },
  { text: "哎呀这个方案还行" },
  { text: "一句话说完就行" },
  { text: "语音口令需要重新设置" },
  { text: "入口在右边" },
];

const FOLLOWUP_RECALL: Case[] = [
  { text: "那它的价格是多少" },
  { text: "帮我把这个总结成 PPT" },
  { text: "再查一下竞品" },
  { text: "为什么会这样" },
  { text: "继续" },
  { text: "换一个主题" },
  { text: "这个怎么用" },
  { text: "麻烦整理成表格" },
  { text: "能不能讲简单一点" },
  { text: "给我生成一份 word" },
];

const FOLLOWUP_FALSE_POSITIVE_GUARD: Case[] = [
  { text: "嗯" },
  { text: "啊" },
  { text: "好的" },
  { text: "对对" },
  { text: "这个" },
  { text: "然后" },
  { text: "ok" },
  { text: "我觉得这个方案不错" },
  { text: "昨天我们去吃饭了" },
  { text: "外面好像下雨了" },
];

const RECAP_RECALL: Case[] = [
  { text: "今日回顾" },
  { text: "回顾一下今天" },
  { text: "帮我总结今天发生了什么" },
  { text: "今天都聊了什么" },
  { text: "梳理一下今天" },
  { text: "过一遍今天做了什么" },
];

const RECAP_FALSE_POSITIVE_GUARD: Case[] = [
  { text: "总结一下英伟达财报" },
  { text: "今天天气怎么样" },
  { text: "生成一个 PPT" },
  { text: "今天星期几" },
  { text: "帮我查一下今天的 AI 新闻" },
  { text: "总结一下这个会议" },
];

function rate(pass: number, total: number): number {
  return total === 0 ? 1 : pass / total;
}

function failedTexts(cases: Case[], predicate: (text: string) => boolean): string[] {
  return cases.filter((c) => !predicate(c.text)).map((c) => `${c.text}${c.note ? ` (${c.note})` : ""}`);
}

test.describe("voice wake quantified baseline", () => {
  test("wake recall >= 95% on STT text variants", () => {
    const failed = failedTexts(WAKE_RECALL, (text) => extractEchoWakeCommand(text) !== null);
    const recall = rate(WAKE_RECALL.length - failed.length, WAKE_RECALL.length);

    expect({ recall, failed }).toMatchObject({ recall: expect.any(Number) });
    expect(recall, `wake recall failed cases:\n${failed.join("\n")}`).toBeGreaterThanOrEqual(0.95);
  });

  test("wake false positive rate <= 5% on common speech", () => {
    const falsePositives = FALSE_POSITIVE_GUARD.filter(
      (c) => extractEchoWakeCommand(c.text) !== null || containsWakeWord(c.text),
    ).map((c) => `${c.text}${c.note ? ` (${c.note})` : ""}`);
    const fpRate = rate(falsePositives.length, FALSE_POSITIVE_GUARD.length);

    expect(fpRate, `wake false positives:\n${falsePositives.join("\n")}`).toBeLessThanOrEqual(0.05);
  });

  test("follow-up gating keeps high recall with low false positive rate", () => {
    const missed = failedTexts(FOLLOWUP_RECALL, isLikelyEchoFollowup);
    const falsePositives = FOLLOWUP_FALSE_POSITIVE_GUARD.filter((c) =>
      isLikelyEchoFollowup(c.text),
    ).map((c) => c.text);

    const recall = rate(FOLLOWUP_RECALL.length - missed.length, FOLLOWUP_RECALL.length);
    const fpRate = rate(falsePositives.length, FOLLOWUP_FALSE_POSITIVE_GUARD.length);
    expect(recall, `follow-up missed:\n${missed.join("\n")}`).toBeGreaterThanOrEqual(0.9);
    expect(fpRate, `follow-up false positives:\n${falsePositives.join("\n")}`).toBeLessThanOrEqual(0.1);
  });

  test("daily recap intent is conservative", () => {
    const missed = failedTexts(RECAP_RECALL, isDailyRecapCommand);
    const falsePositives = RECAP_FALSE_POSITIVE_GUARD.filter((c) =>
      isDailyRecapCommand(c.text),
    ).map((c) => c.text);

    const recall = rate(RECAP_RECALL.length - missed.length, RECAP_RECALL.length);
    const fpRate = rate(falsePositives.length, RECAP_FALSE_POSITIVE_GUARD.length);
    expect(recall, `daily recap missed:\n${missed.join("\n")}`).toBeGreaterThanOrEqual(0.9);
    expect(fpRate, `daily recap false positives:\n${falsePositives.join("\n")}`).toBe(0);
  });
});
