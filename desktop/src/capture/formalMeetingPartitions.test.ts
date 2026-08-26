import assert from "node:assert/strict";
import test from "node:test";

import {
  fenceFormalMeeting,
  formalMeetingPartitions,
  isFormalMeetingFenced,
  registerFormalMeetingPartition,
  releaseFormalMeetingPartitions,
  type FormalMeetingPartitionRegistry,
} from "./formalMeetingPartitions.ts";

test("formal meeting retains every capture partition across pause/resume sessions", () => {
  const registry: FormalMeetingPartitionRegistry = new Map();

  registerFormalMeetingPartition(registry, "meeting-1", "partition-a");
  registerFormalMeetingPartition(registry, "meeting-1", "partition-b");
  registerFormalMeetingPartition(registry, "meeting-1", "partition-a");

  assert.deepEqual(formalMeetingPartitions(registry, "meeting-1"), [
    "partition-a",
    "partition-b",
  ]);
  assert.deepEqual(formalMeetingPartitions(registry, "missing"), []);
});

test("terminal formal release clears partition ownership without clearing the fence", () => {
  const registry: FormalMeetingPartitionRegistry = new Map();
  registerFormalMeetingPartition(registry, "meeting-1", "partition-a");
  registerFormalMeetingPartition(registry, "meeting-1", "partition-b");

  assert.deepEqual(releaseFormalMeetingPartitions(registry, "meeting-1"), [
    "partition-a",
    "partition-b",
  ]);
  assert.deepEqual(formalMeetingPartitions(registry, "meeting-1"), []);
});

test("terminal formal fence is monotonic and blocks later enqueue decisions", () => {
  const fences = new Set<string>();
  assert.equal(isFormalMeetingFenced(fences, "meeting-1"), false);
  fenceFormalMeeting(fences, "meeting-1");
  assert.equal(isFormalMeetingFenced(fences, "meeting-1"), true);
  fenceFormalMeeting(fences, "meeting-1");
  assert.equal(isFormalMeetingFenced(fences, "meeting-1"), true);
  assert.equal(isFormalMeetingFenced(fences, "meeting-2"), false);
});
