package com.echodesk.app;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.List;

import org.junit.Test;

public class NativeAudioGateTest {
  private static final int SAMPLE_RATE = 16_000;

  @Test
  public void freeModeDropsSilenceButFormalModeRetainsItInBoundedSegments() {
    NativeAudioGate freeGate = new NativeAudioGate();
    byte[] silence = pcm(6_000, 0);

    assertTrue(freeGate.process(silence, SAMPLE_RATE, false).isEmpty());
    assertTrue(freeGate.finish().isEmpty());

    NativeAudioGate formalGate = new NativeAudioGate();
    List<NativeAudioGate.Result> formal = formalGate.process(silence, SAMPLE_RATE, true);
    formal.addAll(formalGate.finish());

    assertFalse(formal.isEmpty());
    assertTrue(formal.stream().allMatch(result -> result.accepted && result.formalMode));
    assertTrue(formal.stream().allMatch(result -> result.durationMs <= NativeAudioGate.DEFAULT_MAX_SEGMENT_MS));
    assertArrayEquals(silence, join(formal));
  }

  @Test
  public void freeModeEmitsAfterPostRollWithTwentyMillisecondVadAndBoundedLatency() {
    NativeAudioGate gate = new NativeAudioGate();
    byte[] source = concat(
        pcm(NativeAudioGate.DEFAULT_PRE_ROLL_MS, 0),
        pcm(100, 2_000),
        pcm(NativeAudioGate.DEFAULT_POST_ROLL_MS, 0)
    );

    List<NativeAudioGate.Result> results = new ArrayList<>();
    for (byte[] input : split(source, 80)) {
      results.addAll(gate.process(input, SAMPLE_RATE, false));
    }
    results.addAll(gate.finish());

    assertEquals(1, results.size());
    NativeAudioGate.Result result = results.get(0);
    assertFalse(result.formalMode);
    assertEquals(5, result.speechFrames);
    assertEquals(19, result.observedFrames);
    assertEquals(20, NativeAudioGate.DEFAULT_FRAME_MS);
    assertTrue(result.durationMs <= NativeAudioGate.DEFAULT_MAX_SEGMENT_MS);
  }

  @Test
  public void sixSecondsOfContinuousVoiceIsSplitWithoutLossOrOverlap() {
    NativeAudioGate gate = new NativeAudioGate();
    byte[] source = pcm(6_000, 2_000);
    List<NativeAudioGate.Result> results = new ArrayList<>();
    for (byte[] input : split(source, 80)) {
      results.addAll(gate.process(input, SAMPLE_RATE, false));
    }
    results.addAll(gate.finish());

    assertTrue(results.size() > 1);
    assertTrue(results.stream().allMatch(result -> !result.formalMode));
    assertTrue(results.stream().allMatch(result -> result.durationMs <= NativeAudioGate.DEFAULT_MAX_SEGMENT_MS));
    assertArrayEquals(source, join(results));
  }

  @Test
  public void formalModeFlushesItsShortTailWhenReturningToFreeMode() {
    NativeAudioGate gate = new NativeAudioGate();
    byte[] formal = pcm(300, 1_000);
    List<NativeAudioGate.Result> results = new ArrayList<>();
    results.addAll(gate.process(formal, SAMPLE_RATE, true, "meeting-a"));
    results.addAll(gate.process(pcm(40, 2_000), SAMPLE_RATE, false));

    assertEquals(1, results.size());
    assertTrue(results.get(0).formalMode);
    assertEquals("meeting-a", results.get(0).meetingId);
    assertArrayEquals(formal, results.get(0).pcm);
  }

  private static byte[] pcm(int durationMs, int value) {
    int samples = SAMPLE_RATE * durationMs / 1000;
    byte[] bytes = new byte[samples * 2];
    for (int i = 0; i < samples; i++) writeSample(bytes, i, value);
    return bytes;
  }

  private static void writeSample(byte[] bytes, int index, int value) {
    int offset = index * 2;
    bytes[offset] = (byte) (value & 0xff);
    bytes[offset + 1] = (byte) ((value >>> 8) & 0xff);
  }

  private static byte[] concat(byte[]... values) {
    ByteArrayOutputStream output = new ByteArrayOutputStream();
    for (byte[] value : values) output.write(value, 0, value.length);
    return output.toByteArray();
  }

  private static List<byte[]> split(byte[] value, int durationMs) {
    int bytes = SAMPLE_RATE * 2 * durationMs / 1000;
    List<byte[]> values = new ArrayList<>();
    for (int offset = 0; offset < value.length; offset += bytes) {
      int end = Math.min(value.length, offset + bytes);
      byte[] piece = new byte[end - offset];
      System.arraycopy(value, offset, piece, 0, piece.length);
      values.add(piece);
    }
    return values;
  }

  private static byte[] join(List<NativeAudioGate.Result> results) {
    ByteArrayOutputStream output = new ByteArrayOutputStream();
    for (NativeAudioGate.Result result : results) output.write(result.pcm, 0, result.pcm.length);
    return output.toByteArray();
  }
}
