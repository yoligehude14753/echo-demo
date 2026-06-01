/**
 * 显式产物路由单测（纯函数）——锁住"确定性场景"的路由分流：
 *
 * 场景：帮我总结最近 AI PC 市场的状态（语音/文字；总结/HTML/PPT 三种诉求）。
 * - 纯总结 → 不是产物指令 → 返回 null（交给 agent 直答，永不硬失败）
 * - 生成 HTML / PPT → 命中显式产物 → 直接生成（跳过脆弱的 agent 编排）
 * - 含"调研/搜索/最新新闻"等联网信号 → 返回 null（交给 agent 先检索再生成）
 *
 * 覆盖各种自然口语前缀（请/帮我/麻烦/echo 唤醒词残留）确保不漏判。
 */
import { expect, test } from "@playwright/test";
import { extractExplicitArtifactCommand } from "../../src/lib/explicitArtifactCommand";

// [输入, 期望产物类型] —— 应命中直接生成
const ARTIFACT_CASES: Array<[string, string]> = [
  ["帮我生成一个AI PC市场状态的HTML", "html"],
  ["请生成一个AI PC市场状态的单页网页", "html"],
  ["echo 帮我把最近AI PC市场状态做成PPT", "pptx"],
  ["请你生成一份AI PC市场分析的ppt", "pptx"],
  ["帮我写一份AI PC市场状态的word文档", "word"],
  ["生成一个AI PC市场的excel表格", "xlsx"],
  // 自然口语前缀都不能漏判
  ["麻烦生成一个AI PC市场HTML", "html"],
  ["echo echo，做一个AI PC市场的幻灯片", "pptx"],
];

// 应返回 null —— 交给 agent（纯总结 or 需要联网调研）
const NON_ARTIFACT_CASES: string[] = [
  "帮我总结最近AI PC市场的状态", // 纯总结，无产物名词
  "echo 总结一下最近AI PC市场怎么样", // 纯总结
  "调研一下AI PC市场并生成PPT", // 含"调研" → agent 先检索
  "搜索最新AI PC市场新闻", // 联网信号
  "帮我查一下AI PC的价格", // 查询
  "你重新测试一下", // 与产物无关
  "", // 空
];

test.describe("explicit artifact routing · 确定性场景", () => {
  for (const [input, type] of ARTIFACT_CASES) {
    test(`命中直接生成[${type}]: ${input}`, () => {
      const r = extractExplicitArtifactCommand(input);
      expect(r).not.toBeNull();
      expect(r?.artifactType).toBe(type);
      // brief 必须保留完整指令（含主题），供 skill 生成有内容的产物
      expect((r?.brief.length ?? 0)).toBeGreaterThan(3);
    });
  }
  for (const input of NON_ARTIFACT_CASES) {
    test(`交给 agent(返回 null): ${JSON.stringify(input)}`, () => {
      expect(extractExplicitArtifactCommand(input)).toBeNull();
    });
  }
});
