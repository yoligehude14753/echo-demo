export type MeetingDetailSource = "local-cache" | "local-legacy" | "remote";

export type MeetingDetailRequestSource = Extract<
  MeetingDetailSource,
  "local-legacy" | "remote"
>;

export interface MeetingDetailRequestToken {
  meetingId: string;
  source: MeetingDetailRequestSource;
  generation: number;
}

export interface MeetingDetailMetadata {
  meetingDetailLoaded: Record<string, boolean>;
  meetingDetailErrors: Record<string, string>;
  meetingDetailSources: Record<string, MeetingDetailSource>;
  meetingDetailGenerations: Record<string, number>;
}

export type MeetingDetailRetryAction =
  | "clear-stale-local-error"
  | "reload-local"
  | "reload-remote";

export type MeetingDetailLoadTarget = "none" | "local-ipc" | "remote-api";

/** 将运行时数据边界映射为唯一合法的详情传输，避免 local id 落到公网 API。 */
export function resolveMeetingDetailLoadTarget(
  localHistoryOnly: boolean,
  loaded: boolean,
): MeetingDetailLoadTarget {
  if (loaded) return "none";
  return localHistoryOnly ? "local-ipc" : "remote-api";
}

function withoutKey<T>(
  record: Record<string, T>,
  key: string,
): Record<string, T> {
  const next = { ...record };
  delete next[key];
  return next;
}

/**
 * 从 localStorage 或 legacy IPC 恢复的会议已经携带完整详情。恢复详情与更新
 * source/generation 必须是同一个原子状态转换，借此让更早启动的远端请求失效。
 */
export function hydrateMeetingDetailMetadata(
  meetingIds: Iterable<string>,
  current: MeetingDetailMetadata,
  source: Extract<MeetingDetailSource, "local-cache" | "local-legacy">,
): MeetingDetailMetadata {
  const meetingDetailLoaded = { ...current.meetingDetailLoaded };
  const meetingDetailErrors = { ...current.meetingDetailErrors };
  const meetingDetailSources = { ...current.meetingDetailSources };
  const meetingDetailGenerations = { ...current.meetingDetailGenerations };

  for (const meetingId of meetingIds) {
    meetingDetailLoaded[meetingId] = true;
    delete meetingDetailErrors[meetingId];
    meetingDetailSources[meetingId] = source;
    meetingDetailGenerations[meetingId] =
      (meetingDetailGenerations[meetingId] ?? 0) + 1;
  }

  return {
    meetingDetailLoaded,
    meetingDetailErrors,
    meetingDetailSources,
    meetingDetailGenerations,
  };
}

/** 为一次详情 I/O 分配单调递增 generation，并清除上一轮错误/完成标记。 */
export function beginMeetingDetailRequest(
  meetingId: string,
  source: MeetingDetailRequestSource,
  current: MeetingDetailMetadata,
): { token: MeetingDetailRequestToken; metadata: MeetingDetailMetadata } {
  const generation = (current.meetingDetailGenerations[meetingId] ?? 0) + 1;
  return {
    token: { meetingId, source, generation },
    metadata: {
      meetingDetailLoaded: {
        ...current.meetingDetailLoaded,
        [meetingId]: false,
      },
      meetingDetailErrors: withoutKey(current.meetingDetailErrors, meetingId),
      meetingDetailSources: {
        ...current.meetingDetailSources,
        [meetingId]: source,
      },
      meetingDetailGenerations: {
        ...current.meetingDetailGenerations,
        [meetingId]: generation,
      },
    },
  };
}

/**
 * Promise 完成后提交成功或失败前的最后一道 fence。任何 source/generation 变化，
 * 或详情已由另一权威来源完成，都表示当前结果已经 stale。
 */
export function canCommitMeetingDetailRequest(
  token: MeetingDetailRequestToken,
  current: MeetingDetailMetadata,
): boolean {
  return (
    current.meetingDetailSources[token.meetingId] === token.source &&
    current.meetingDetailGenerations[token.meetingId] === token.generation &&
    current.meetingDetailLoaded[token.meetingId] !== true
  );
}

function isLocalSource(source: MeetingDetailSource | undefined): boolean {
  return source === "local-cache" || source === "local-legacy";
}

/**
 * 重试策略以当前运行时的数据边界为准。远端重试必须清 loaded 后重拉；本地已加载
 * 卡片上的 error 已被 source+loaded 证伪，只清 stale error，不制造公网请求。
 */
export function retryMeetingDetailMetadata(
  meetingId: string,
  localHistoryOnly: boolean,
  current: MeetingDetailMetadata,
): {
  action: MeetingDetailRetryAction;
  metadata: MeetingDetailMetadata;
  refetch: boolean;
} {
  const source = current.meetingDetailSources[meetingId];
  if (
    localHistoryOnly &&
    current.meetingDetailLoaded[meetingId] === true &&
    isLocalSource(source)
  ) {
    return {
      action: "clear-stale-local-error",
      refetch: false,
      metadata: {
        ...current,
        meetingDetailErrors: withoutKey(current.meetingDetailErrors, meetingId),
      },
    };
  }

  const requestSource: MeetingDetailRequestSource = localHistoryOnly
    ? "local-legacy"
    : "remote";
  const generation = (current.meetingDetailGenerations[meetingId] ?? 0) + 1;
  return {
    action: localHistoryOnly ? "reload-local" : "reload-remote",
    refetch: true,
    metadata: {
      meetingDetailLoaded: {
        ...current.meetingDetailLoaded,
        [meetingId]: false,
      },
      meetingDetailErrors: withoutKey(current.meetingDetailErrors, meetingId),
      meetingDetailSources: {
        ...current.meetingDetailSources,
        [meetingId]: requestSource,
      },
      meetingDetailGenerations: {
        ...current.meetingDetailGenerations,
        [meetingId]: generation,
      },
    },
  };
}
