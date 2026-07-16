package com.echodesk.app;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.io.File;
import java.nio.file.Files;
import java.util.List;
import java.util.Properties;
import org.junit.Test;

public class NativeCaptureQueueTest {
  @Test
  public void queueCommitsCompleteRecordAndRemovesItAfterAck() throws Exception {
    File root = Files.createTempDirectory("echo-native-queue").toFile();
    NativeCaptureQueue queue = new NativeCaptureQueue(root, 1024, 60_000);
    Properties metadata = metadata("segment-1", System.currentTimeMillis());
    byte[] wav = new byte[] {1, 2, 3, 4};

    NativeCaptureQueue.Record record = queue.enqueue(wav, metadata);
    List<NativeCaptureQueue.Record> records = queue.records();

    assertEquals(1, records.size());
    assertEquals("segment-1", record.id());
    assertArrayEquals(wav, Files.readAllBytes(record.audio.toPath()));
    assertFalse(record.directory.getName().startsWith(".tmp-"));

    queue.remove(record);
    assertEquals(0, queue.count());
    NativeCaptureQueue.deleteRecursively(root);
  }

  @Test
  public void cleanupRemovesInterruptedTempDirectory() throws Exception {
    File root = Files.createTempDirectory("echo-native-queue").toFile();
    File temporary = new File(root, ".tmp-interrupted");
    assertTrue(temporary.mkdirs());
    Files.write(new File(temporary, "audio.wav").toPath(), new byte[] {1});
    NativeCaptureQueue queue = new NativeCaptureQueue(root, 1024, 60_000);

    queue.cleanup(System.currentTimeMillis());

    assertFalse(temporary.exists());
    NativeCaptureQueue.deleteRecursively(root);
  }

  @Test
  public void cleanupEnforcesRetentionAndCapacity() throws Exception {
    File root = Files.createTempDirectory("echo-native-queue").toFile();
    NativeCaptureQueue queue = new NativeCaptureQueue(root, 6, 60_000);
    long now = System.currentTimeMillis();
    NativeCaptureQueue.Record old =
        queue.enqueue(new byte[] {1, 2, 3, 4}, metadata("old", now - 1_000));
    old.directory.setLastModified(now - 1_000);
    queue.enqueue(new byte[] {5, 6, 7, 8}, metadata("new", now));

    queue.cleanup(now);

    assertEquals(1, queue.count());
    assertEquals("new", queue.records().get(0).id());
    NativeCaptureQueue.deleteRecursively(root);
  }

  private static Properties metadata(String id, long createdAt) {
    Properties metadata = new Properties();
    metadata.setProperty("segmentId", id);
    metadata.setProperty("createdAt", String.valueOf(createdAt));
    metadata.setProperty("deviceId", "device-test");
    metadata.setProperty("sampleRate", "16000");
    return metadata;
  }
}
