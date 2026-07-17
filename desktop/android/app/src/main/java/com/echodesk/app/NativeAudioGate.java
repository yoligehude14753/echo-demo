package com.echodesk.app;

import java.util.Arrays;

/**
 * Pure-Java endpoint gate for the native capture runtime.
 *
 * Free mode keeps a short pre-roll and only persists chunks that contain
 * speech-like frames. Formal mode bypasses the gate and preserves every byte.
 */
final class NativeAudioGate {
  static final int DEFAULT_FRAME_MS = 20;
  static final int DEFAULT_PRE_ROLL_MS = 500;
  static final double DEFAULT_RMS_THRESHOLD = 180.0;
  static final int DEFAULT_PEAK_THRESHOLD = 800;
  static final int DEFAULT_MIN_SPEECH_FRAMES = 2;

  static final class Result {
    final byte[] pcm;
    final boolean accepted;
    final int observedFrames;
    final int speechFrames;
    final double rms;
    final int peak;

    Result(
        byte[] pcm,
        boolean accepted,
        int observedFrames,
        int speechFrames,
        double rms,
        int peak
    ) {
      this.pcm = pcm;
      this.accepted = accepted;
      this.observedFrames = observedFrames;
      this.speechFrames = speechFrames;
      this.rms = rms;
      this.peak = peak;
    }
  }

  private byte[] preRoll = new byte[0];

  Result process(byte[] pcm, int sampleRate, boolean formalMode) {
    byte[] safe = pcm == null ? new byte[0] : pcm;
    AudioStats total = stats(safe, 0, safe.length);
    if (formalMode) {
      rememberTail(safe, sampleRate);
      return new Result(
          Arrays.copyOf(safe, safe.length),
          true,
          frameCount(safe.length, sampleRate),
          frameCount(safe.length, sampleRate),
          total.rms,
          total.peak
      );
    }

    int frameBytes = Math.max(2, sampleRate * 2 * DEFAULT_FRAME_MS / 1000);
    int observed = 0;
    int speech = 0;
    for (int offset = 0; offset + 1 < safe.length; offset += frameBytes) {
      int length = Math.min(frameBytes, safe.length - offset);
      length -= length % 2;
      if (length <= 0) continue;
      AudioStats frame = stats(safe, offset, length);
      observed += 1;
      if (
          frame.rms >= DEFAULT_RMS_THRESHOLD
              && frame.peak >= DEFAULT_PEAK_THRESHOLD
      ) {
        speech += 1;
      }
    }

    boolean accepted = speech >= DEFAULT_MIN_SPEECH_FRAMES;
    byte[] previousPreRoll = preRoll;
    rememberTail(safe, sampleRate);
    if (!accepted) {
      return new Result(
          new byte[0],
          false,
          observed,
          speech,
          total.rms,
          total.peak
      );
    }
    byte[] withPreRoll = new byte[previousPreRoll.length + safe.length];
    System.arraycopy(previousPreRoll, 0, withPreRoll, 0, previousPreRoll.length);
    System.arraycopy(safe, 0, withPreRoll, previousPreRoll.length, safe.length);
    return new Result(
        withPreRoll,
        true,
        observed,
        speech,
        total.rms,
        total.peak
    );
  }

  private void rememberTail(byte[] pcm, int sampleRate) {
    int wanted = Math.max(0, sampleRate * 2 * DEFAULT_PRE_ROLL_MS / 1000);
    int length = Math.min(wanted, pcm.length);
    length -= length % 2;
    preRoll = Arrays.copyOfRange(pcm, pcm.length - length, pcm.length);
  }

  private static int frameCount(int bytes, int sampleRate) {
    int frameBytes = Math.max(2, sampleRate * 2 * DEFAULT_FRAME_MS / 1000);
    return (bytes + frameBytes - 1) / frameBytes;
  }

  private static AudioStats stats(byte[] pcm, int offset, int length) {
    long sumSquares = 0;
    int peak = 0;
    int safeStart = Math.max(0, Math.min(offset, pcm.length));
    int safeEnd = Math.max(
        safeStart,
        Math.min(pcm.length, safeStart + Math.max(0, length))
    );
    int samples = (safeEnd - safeStart) / 2;
    for (int i = safeStart; i + 1 < safeEnd; i += 2) {
      int lo = pcm[i] & 0xff;
      int hi = pcm[i + 1];
      int value = (short) ((hi << 8) | lo);
      int absolute = Math.abs(value);
      peak = Math.max(peak, absolute);
      sumSquares += (long) value * (long) value;
    }
    double rms =
        samples == 0 ? 0.0 : Math.sqrt((double) sumSquares / samples);
    return new AudioStats(rms, peak);
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
