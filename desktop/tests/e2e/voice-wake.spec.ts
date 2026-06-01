/**
 * 唤醒词匹配单测（纯函数，无浏览器）。
 *
 * 用真链路实测到的 STT 听写形态做正样本，常见口语做负样本，锁住
 * "鲁棒召回 + 不误触" 两条线。相对路径 import，避免依赖 @ alias。
 */
import { expect, test } from "@playwright/test";
import { containsWakeWord, extractEchoWakeCommand } from "../../src/lib/voiceWake";

// [输入, 期望抽取出的指令]
const POSITIVE: Array<[string, string]> = [
  ["echo生成ppt", "生成ppt"],
  ["echo生成 ppt主题是人工智能行业分析", "生成 ppt主题是人工智能行业分析"],
  ["pico生成 ppt主题人工智能行业分析", "生成 ppt主题人工智能行业分析"],
  ["aiko生成word语音验收报告", "生成word语音验收报告"],
  ["aico，帮我查一下今天天气", "帮我查一下今天天气"],
  ["诶，一口生成一个关于人工智能的 ppt", "生成一个关于人工智能的 ppt"],
  ["嘿一口，生成PPT", "生成PPT"],
  ["Echo，帮我查一下天气", "帮我查一下天气"],
  ["好的。Echo 生成报告", "生成报告"],
  ["echodesk 打开设置", "打开设置"],
  ["诶口，总结一下会议", "总结一下会议"],
  // 真链路实测：真人说 "Echo" 被 STT 听成「乱码前缀 + 口/狗」，音节模糊兜底
  ["汉宜口今天星期几", "今天星期几"],
  ["汉语狗今天星期几", "今天星期几"],
  ["艾口，生成一个PPT", "生成一个PPT"],
  ["爱扣 查一下天气", "查一下天气"],
  // 老库音节级：起头音节 + i/u 韵 body（嘿依co / 海鱼口 / 嗨依扣 …）
  ["嘿依co，今天星期几", "今天星期几"],
  ["海鱼口，总结会议", "总结会议"],
  ["嗨依扣 生成PPT", "生成PPT"],
  ["嘿，依co，查天气", "查天气"],
];

// 不应触发唤醒（避免误打断正常对话）
const NEGATIVE = [
  "我吃了一口饭就走了",
  "一口气把这件事做完了",
  "这个eco系统挺复杂的",
  "今天天气不错我们出去走走",
  "他的自我ego太强了",
  "随便聊聊最近的项目进度",
  "", // 空串
  // 口/狗/扣 真实词在句首也不应误触（首字发音不在 i/u/ai 韵集，天然排除）
  "开口说话之前先想想",
  "出口在大楼的右边",
  "小狗很可爱我很喜欢",
  "可口可乐买一瓶",
  "门口有人在等你",
  "随口说说而已别当真",
  "折扣力度还挺大的",
  "胃口不太好",
  "路口左转就到了",
  // 提及/转述 echo（语义后过滤，非真唤醒）
  "我跟echo说过这件事了",
  "刚才提到echo这个产品",
];

test.describe("voice wake matcher", () => {
  for (const [input, cmd] of POSITIVE) {
    test(`命中并抽取指令: ${input}`, () => {
      expect(extractEchoWakeCommand(input)).toBe(cmd);
    });
  }
  for (const input of NEGATIVE) {
    test(`不误触: ${JSON.stringify(input)}`, () => {
      expect(extractEchoWakeCommand(input)).toBeNull();
    });
  }
});

// 跨 chunk 拆分：唤醒词单独出现（指令被切到下个 chunk）时，
// extractEchoWakeCommand 返回 null，但 containsWakeWord 必须为 true。
test.describe("cross-chunk wake stitching", () => {
  const WAKE_ONLY: string[] = ["echo", "Echo。", "诶口", "echodesk", "pico", "嘿 echo"];
  for (const input of WAKE_ONLY) {
    test(`唤醒词单独出现可被检测: ${JSON.stringify(input)}`, () => {
      expect(extractEchoWakeCommand(input)).toBeNull(); // 没有指令
      expect(containsWakeWord(input)).toBe(true); // 但含唤醒词
    });
  }

  const NO_WAKE: string[] = ["今天星期几", "帮我生成一个 PPT", "随便聊聊", ""];
  for (const input of NO_WAKE) {
    test(`非唤醒词不应被检测为唤醒: ${JSON.stringify(input)}`, () => {
      expect(containsWakeWord(input)).toBe(false);
    });
  }
});
