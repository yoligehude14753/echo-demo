package com.echodesk.app;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class NativeAudioGateTest {
  private static final int SAMPLE_RATE = 16_000;

  @Test
  public void freeModeDropsSilenceButFormalModeRetainsIt() {
    NativeAudioGate gate = new NativeAudioGate();
    byte[] silence = new byte[SAMPLE_RATE * 2 * 6];

    NativeAudioGate.Result free = gate.process(silence, SAMPLE_RATE, false);
    NativeAudioGate.Result formal = gate.process(silence, SAMPLE_RATE, true);

    assertFalse(free.accepted);
    assertEquals(0, free.pcm.length);
    assertTrue(formal.accepted);
    assertArrayEquals(silence, formal.pcm);
  }

  @Test
  public void acceptedFreeChunkIncludesPreviousHalfSecondPreRoll() {
    NativeAudioGate gate = new NativeAudioGate();
    byte[] prior = pcm(6_000, 100);
    byte[] speech = pcm(6_000, 2_000);

    assertFalse(gate.process(prior, SAMPLE_RATE, false).accepted);
    NativeAudioGate.Result accepted = gate.process(speech, SAMPLE_RATE, false);

    assertTrue(accepted.accepted);
    assertEquals(
        speech.length + SAMPLE_RATE * 2 * NativeAudioGate.DEFAULT_PRE_ROLL_MS / 1000,
        accepted.pcm.length
    );
  }

  @Test
  public void shortSpikeDoesNotPassSpeechFrameGate() {
    NativeAudioGate gate = new NativeAudioGate();
    byte[] pcm = new byte[SAMPLE_RATE * 2 * 6];
    writeSample(pcm, 0, 4_000);

    NativeAudioGate.Result result = gate.process(pcm, SAMPLE_RATE, false);

    assertFalse(result.accepted);
    assertEquals(1, result.speechFrames);
  }

  private static byte[] pcm(int durationMs, int value) {
    int samples = SAMPLE_RATE * durationMs / 1000;
    byte[] bytes = new byte[samples * 2];
    for (int i = 0; i < samples; i++) {
      writeSample(bytes, i, value);
    }
    return bytes;
  }

  private static void writeSample(byte[] bytes, int index, int value) {
    int offset = index * 2;
    bytes[offset] = (byte) (value & 0xff);
    bytes[offset + 1] = (byte) ((value >>> 8) & 0xff);
  }
}
