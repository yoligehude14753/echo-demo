function createBackendLifecycleController({ now = Date.now } = {}) {
  let generation = 0;
  let child = null;
  let ready = false;
  let startedAt = 0;
  let healthFailures = 0;

  function lease() {
    return Object.freeze({ generation, child });
  }

  function isCurrent(candidate) {
    return Boolean(
      candidate &&
      candidate.generation === generation &&
      candidate.child === child,
    );
  }

  function beginSpawn() {
    generation += 1;
    child = null;
    ready = false;
    startedAt = now();
    healthFailures = 0;
    return lease();
  }

  function attachChild(spawnLease, nextChild) {
    if (!isCurrent(spawnLease) || !nextChild) return null;
    child = nextChild;
    return lease();
  }

  function invalidate(expectedLease = null) {
    if (expectedLease && !isCurrent(expectedLease)) return null;
    const previous = { generation, child, ready, startedAt, healthFailures };
    generation += 1;
    child = null;
    ready = false;
    startedAt = 0;
    healthFailures = 0;
    return Object.freeze({ ...previous, invalidatedByGeneration: generation });
  }

  function settleHealth(candidate, healthy) {
    if (!isCurrent(candidate)) return { state: "stale", failures: 0 };
    if (!healthy) {
      if (!ready) return { state: "starting", failures: 0 };
      healthFailures += 1;
      return { state: "failed", failures: healthFailures };
    }

    healthFailures = 0;
    if (ready) return { state: "healthy", failures: 0 };
    ready = true;
    return { state: "ready", failures: 0 };
  }

  return Object.freeze({
    attachChild,
    beginSpawn,
    currentChild: () => child,
    currentLease: lease,
    elapsedMs: (candidate) => isCurrent(candidate) && startedAt > 0
      ? Math.max(0, now() - startedAt)
      : 0,
    invalidate,
    isCurrent,
    isGenerationCurrent: (candidateGeneration) => candidateGeneration === generation,
    isReady: (candidate) => isCurrent(candidate) && ready,
    settleHealth,
    snapshot: () => ({ generation, child, ready, startedAt, healthFailures }),
  });
}

module.exports = { createBackendLifecycleController };
