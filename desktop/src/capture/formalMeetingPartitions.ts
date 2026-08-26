export type FormalMeetingPartitionRegistry = Map<string, Set<string>>;
export type FormalMeetingFenceRegistry = Set<string>;

export function registerFormalMeetingPartition(
  registry: FormalMeetingPartitionRegistry,
  meetingId: string,
  partition: string,
): void {
  if (!meetingId || !partition) return;
  const partitions = registry.get(meetingId) ?? new Set<string>();
  partitions.add(partition);
  registry.set(meetingId, partitions);
}

export function formalMeetingPartitions(
  registry: FormalMeetingPartitionRegistry,
  meetingId: string,
): string[] {
  return [...(registry.get(meetingId) ?? [])];
}

export function releaseFormalMeetingPartitions(
  registry: FormalMeetingPartitionRegistry,
  meetingId: string,
): string[] {
  const partitions = formalMeetingPartitions(registry, meetingId);
  registry.delete(meetingId);
  return partitions;
}

export function fenceFormalMeeting(
  registry: FormalMeetingFenceRegistry,
  meetingId: string,
): void {
  if (meetingId) registry.add(meetingId);
}

export function isFormalMeetingFenced(
  registry: FormalMeetingFenceRegistry,
  meetingId: string | null,
): boolean {
  return meetingId !== null && registry.has(meetingId);
}
