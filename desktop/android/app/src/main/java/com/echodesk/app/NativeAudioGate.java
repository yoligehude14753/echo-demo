package com.echodesk.app;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/**
 * 原生 PCM 分段与准入门控。
 *
 * <p>自由模式逐块处理：每个输入块切成 20 ms 帧，只输出有效语音段，单段绝不
 * 超过 640 ms。正式会议保留全部 PCM，但也按同一 640 ms 上限发送，避免长会议
 * 累积数秒上传延迟。</p>
 */
final class NativeAudioGate {
  static final int DEFAULT_FRAME_MS = 20;
  static final int DEFAULT_PRE_ROLL_FRAMES = 6; // 120 ms
  static final int DEFAULT_POST_ROLL_FRAMES = 8; // 160 ms
  static final int DEFAULT_MAX_SEGMENT_FRAMES = 32; // 640 ms
  static final int DEFAULT_PRE_ROLL_MS = DEFAULT_PRE_ROLL_FRAMES * DEFAULT_FRAME_MS;
  static final int DEFAULT_POST_ROLL_MS = DEFAULT_POST_ROLL_FRAMES * DEFAULT_FRAME_MS;
  static final int DEFAULT_MAX_SEGMENT_MS = DEFAULT_MAX_SEGMENT_FRAMES * DEFAULT_FRAME_MS;
  static final double DEFAULT_RMS_THRESHOLD = 180.0;
  static final int DEFAULT_PEAK_THRESHOLD = 800;
  static final int DEFAULT_MIN_SPEECH_FRAMES = 2;

  static final class Result {
    final byte[] pcm;
    final boolean accepted;
    final boolean formalMode;
    final String meetingId;
    final int sampleRate;
    final int observedFrames;
    final int speechFrames;
    final int durationMs;
    final double speechFrameRatio;
    final double rms;
    final int peak;

    Result(
        byte[] pcm,
        boolean accepted,
        boolean formalMode,
        String meetingId,
        int sampleRate,
        int observedFrames,
        int speechFrames,
        int durationMs,
        double speechFrameRatio,
        double rms,
        int peak
    ) {
      this.pcm = pcm;
      this.accepted = accepted;
      this.formalMode = formalMode;
      this.meetingId = meetingId;
      this.sampleRate = sampleRate;
      this.observedFrames = observedFrames;
      this.speechFrames = speechFrames;
      this.durationMs = durationMs;
      this.speechFrameRatio = speechFrameRatio;
      this.rms = rms;
      this.peak = peak;
    }
  }

  private final List<byte[]> preRoll = new ArrayList<>();
  private final List<byte[]> active = new ArrayList<>();
  private byte[] pending = new byte[0];
  private byte[] formalPending = new byte[0];
  private int activeVoiceFrames;
  private int trailingSilentFrames;
  private int activeSampleRate;
  private boolean formalMode;
  private String formalMeetingId = "";

  /**
   * 消费任意长度的 PCM 块。调用方可以传入 20-80 ms 读取；本门控负责跨读取缓冲并
   * 返回零个或多个可上传片段。任何结果都不长于 {@link #DEFAULT_MAX_SEGMENT_MS}。
   */
  synchronized List<Result> process(byte[] pcm, int sampleRate, boolean nextFormalMode) {
    return process(pcm, sampleRate, nextFormalMode, "");
  }

  synchronized List<Result> process(
      byte[] pcm,
      int sampleRate,
      boolean nextFormalMode,
      String nextMeetingId
  ) {
    int safeSampleRate = sampleRate > 0 ? sampleRate : 16_000;
    List<Result> results = new ArrayList<>();
    if (activeSampleRate != 0 && activeSampleRate != safeSampleRate) {
      results.addAll(finishInternal());
      clearState();
    }
    activeSampleRate = safeSampleRate;

    byte[] safePcm = pcm == null ? new byte[0] : pcm;
    String safeMeetingId = nextMeetingId == null ? "" : nextMeetingId.trim();
    if (nextFormalMode != formalMode) {
      if (formalMode) {
        drainFormal(results, true);
      } else {
        finishFree(results);
      }
      formalMode = nextFormalMode;
      formalMeetingId = nextFormalMode ? safeMeetingId : "";
    } else if (formalMode && !formalMeetingId.equals(safeMeetingId)) {
      // 正式会议切换到另一场时，先提交前一场的短尾段，不能把两场录音混入同一
      // 关联标识。
      drainFormal(results, true);
      formalMeetingId = safeMeetingId;
    }

    if (formalMode) {
      formalPending = concat(formalPending, safePcm);
      drainFormal(results, false);
    } else {
      processFree(safePcm, results);
    }
    return results;
  }

  /** AudioRecord 停止时提交最后一个受限长度的正式/自由片段。 */
  synchronized List<Result> finish() {
    List<Result> results = finishInternal();
    clearState();
    return results;
  }

  synchronized void reset() {
    clearState();
  }

  private List<Result> finishInternal() {
    if (activeSampleRate <= 0) return Collections.emptyList();
    List<Result> results = new ArrayList<>();
    if (formalMode) {
      drainFormal(results, true);
    } else {
      finishFree(results);
    }
    return results;
  }

  private void processFree(byte[] pcm, List<Result> results) {
    byte[] source = concat(pending, pcm);
    int frameBytes = frameBytes(activeSampleRate);
    int offset = 0;
    while (offset + frameBytes <= source.length) {
      observeFreeFrame(
          Arrays.copyOfRange(source, offset, offset + frameBytes),
          results
      );
      offset += frameBytes;
    }
    pending = Arrays.copyOfRange(source, offset, source.length);
  }

  private void observeFreeFrame(byte[] frame, List<Result> results) {
    AudioStats frameStats = stats(frame);
    boolean voiced = isVoiced(frameStats);
    if (active.isEmpty()) {
      if (!voiced) {
        rememberPreRoll(frame);
        return;
      }
      active.addAll(preRoll);
      preRoll.clear();
    }

    active.add(frame);
    if (voiced) {
      activeVoiceFrames += 1;
      trailingSilentFrames = 0;
    } else {
      trailingSilentFrames += 1;
    }

    if (
        activeVoiceFrames >= DEFAULT_MIN_SPEECH_FRAMES
            && active.size() >= DEFAULT_MAX_SEGMENT_FRAMES
    ) {
      emitFree(results);
      return;
    }
    if (
        activeVoiceFrames >= DEFAULT_MIN_SPEECH_FRAMES
            && trailingSilentFrames >= DEFAULT_POST_ROLL_FRAMES
    ) {
      emitFree(results);
      return;
    }
    if (
        activeVoiceFrames < DEFAULT_MIN_SPEECH_FRAMES
            && trailingSilentFrames >= DEFAULT_POST_ROLL_FRAMES
    ) {
      resetFreeToIdle();
    }
  }

  private void finishFree(List<Result> results) {
    int frameBytes = frameBytes(activeSampleRate);
    if (pending.length > 0) {
      byte[] padded = new byte[frameBytes];
      System.arraycopy(pending, 0, padded, 0, Math.min(pending.length, padded.length));
      pending = new byte[0];
      observeFreeFrame(padded, results);
    }
    if (activeVoiceFrames >= DEFAULT_MIN_SPEECH_FRAMES) {
      emitFree(results);
    } else {
      resetFreeToIdle();
    }
  }

  private void emitFree(List<Result> results) {
    if (active.isEmpty()) return;
    byte[] pcm = concat(active);
    AudioStats summary = stats(pcm);
    int observed = active.size();
    results.add(
        new Result(
            pcm,
            true,
            false,
            "",
            activeSampleRate,
            observed,
            activeVoiceFrames,
            durationMs(pcm.length, activeSampleRate),
            activeVoiceFrames / (double) Math.max(1, observed),
            summary.rms,
            summary.peak
        )
    );
    active.clear();
    activeVoiceFrames = 0;
    trailingSilentFrames = 0;
  }

  private void resetFreeToIdle() {
    preRoll.clear();
    int start = Math.max(0, active.size() - DEFAULT_PRE_ROLL_FRAMES);
    for (int index = start; index < active.size(); index += 1) {
      preRoll.add(active.get(index));
    }
    active.clear();
    activeVoiceFrames = 0;
    trailingSilentFrames = 0;
  }

  private void rememberPreRoll(byte[] frame) {
    preRoll.add(frame);
    if (preRoll.size() > DEFAULT_PRE_ROLL_FRAMES) {
      preRoll.remove(0);
    }
  }

  private void drainFormal(List<Result> results, boolean flushTail) {
    int maxBytes = maxSegmentBytes(activeSampleRate);
    while (formalPending.length >= maxBytes || (flushTail && formalPending.length > 0)) {
      int length = Math.min(maxBytes, formalPending.length);
      byte[] pcm = Arrays.copyOfRange(formalPending, 0, length);
      formalPending = Arrays.copyOfRange(formalPending, length, formalPending.length);
      AudioStats summary = stats(pcm);
      int observed = frameCount(pcm.length, activeSampleRate);
      results.add(
          new Result(
              pcm,
              true,
              true,
              formalMeetingId,
              activeSampleRate,
              observed,
              observed,
              durationMs(pcm.length, activeSampleRate),
              observed == 0 ? 0.0 : 1.0,
              summary.rms,
              summary.peak
          )
      );
    }
  }

  private void clearState() {
    preRoll.clear();
    active.clear();
    pending = new byte[0];
    formalPending = new byte[0];
    activeVoiceFrames = 0;
    trailingSilentFrames = 0;
    activeSampleRate = 0;
    formalMode = false;
    formalMeetingId = "";
  }

  private boolean isVoiced(AudioStats frame) {
    return frame.rms >= DEFAULT_RMS_THRESHOLD && frame.peak >= DEFAULT_PEAK_THRESHOLD;
  }

  private static int frameBytes(int sampleRate) {
    return Math.max(2, sampleRate * 2 * DEFAULT_FRAME_MS / 1000);
  }

  private static int maxSegmentBytes(int sampleRate) {
    return frameBytes(sampleRate) * DEFAULT_MAX_SEGMENT_FRAMES;
  }

  private static int frameCount(int bytes, int sampleRate) {
    int frameBytes = frameBytes(sampleRate);
    return (bytes + frameBytes - 1) / frameBytes;
  }

  private static int durationMs(int bytes, int sampleRate) {
    if (sampleRate <= 0) return 0;
    return Math.round((bytes / 2f) * 1_000f / sampleRate);
  }

  private static byte[] concat(byte[] left, byte[] right) {
    byte[] merged = new byte[left.length + right.length];
    System.arraycopy(left, 0, merged, 0, left.length);
    System.arraycopy(right, 0, merged, left.length, right.length);
    return merged;
  }

  private static byte[] concat(List<byte[]> frames) {
    int length = 0;
    for (byte[] frame : frames) length += frame.length;
    byte[] merged = new byte[length];
    int offset = 0;
    for (byte[] frame : frames) {
      System.arraycopy(frame, 0, merged, offset, frame.length);
      offset += frame.length;
    }
    return merged;
  }

  private static AudioStats stats(byte[] pcm) {
    long sumSquares = 0;
    int peak = 0;
    int samples = pcm.length / 2;
    for (int offset = 0; offset + 1 < pcm.length; offset += 2) {
      int low = pcm[offset] & 0xff;
      int high = pcm[offset + 1];
      int value = (short) ((high << 8) | low);
      int absolute = Math.abs(value);
      peak = Math.max(peak, absolute);
      sumSquares += (long) value * (long) value;
    }
    return new AudioStats(
        samples == 0 ? 0.0 : Math.sqrt((double) sumSquares / samples),
        peak
    );
  }

  private static final class AudioStats {
    final double rms;
    final int peak;

    AudioStats(double rms, int peak) {
      this.rms = rms;
      this.peak = peak;
    }
  }
}
