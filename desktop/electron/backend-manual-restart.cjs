const { spawn } = require("node:child_process");

function stopWindowsProcessTree(
  proc,
  { spawnProcess = spawn } = {},
) {
  if (!proc || !Number.isSafeInteger(proc.pid) || proc.pid <= 0) {
    return Promise.reject(new Error("backend child has no valid Windows pid"));
  }
  return new Promise((resolve, reject) => {
    let taskkill;
    try {
      // PyInstaller one-file executables use a bootloader parent plus the real
      // application child. Killing only the process returned by Node can leave
      // the server child listening after Electron exits, so terminate the
      // complete descendant tree and wait for taskkill itself to finish.
      taskkill = spawnProcess(
        "taskkill.exe",
        ["/PID", String(proc.pid), "/T", "/F"],
        { windowsHide: true, stdio: "ignore" },
      );
    } catch (error) {
      reject(error);
      return;
    }
    taskkill.once("error", reject);
    taskkill.once("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`taskkill failed for backend process tree (exit ${code})`));
    });
  });
}

function stopBackendProcess(
  proc,
  {
    schedule = setTimeout,
    cancel = clearTimeout,
    graceMs = 3_000,
    killWaitMs = 1_000,
    platform = process.platform,
    stopWindowsTree = stopWindowsProcessTree,
  } = {},
) {
  if (!proc || proc.exitCode !== null) return Promise.resolve();
  if (platform === "win32") return stopWindowsTree(proc);
  return new Promise((resolve, reject) => {
    let finished = false;
    let forceTimer = null;
    let failureTimer = null;
    const finish = (error = null) => {
      if (finished) return;
      finished = true;
      if (forceTimer) cancel(forceTimer);
      if (failureTimer) cancel(failureTimer);
      proc.removeListener("exit", onExit);
      if (error) reject(error);
      else resolve();
    };
    const onExit = () => finish();
    proc.once("exit", onExit);
    try {
      proc.kill("SIGTERM");
    } catch (error) {
      finish(error);
      return;
    }
    if (proc.exitCode !== null) {
      finish();
      return;
    }
    forceTimer = schedule(() => {
      if (proc.exitCode !== null) {
        finish();
        return;
      }
      try {
        proc.kill("SIGKILL");
      } catch (error) {
        finish(error);
        return;
      }
      failureTimer = schedule(() => {
        if (proc.exitCode !== null) finish();
        else finish(new Error("backend child did not exit after SIGKILL"));
      }, killWaitMs);
    }, graceMs);
  });
}

function createManualBackendRestart(options) {
  const {
    isPublicDemo,
    healthcheckOnce,
    emitStatus,
    resetRestartState,
    stopHealthWatcher,
    stopExternalHealthWatcher,
    stopBackendProc,
    killBackendProc,
    spawnBackendAndWatch,
    isShuttingDown,
    schedule = setTimeout,
    restartDelayMs = 500,
  } = options;

  let inFlight = null;
  let generation = 0;

  function waitForDelay() {
    return new Promise((resolve) => schedule(resolve, restartDelayMs));
  }

  return function manualRestartBackend() {
    if (inFlight) return inFlight;
    const currentGeneration = ++generation;
    const operation = (async () => {
      if (isPublicDemo()) {
        const ok = await healthcheckOnce();
        emitStatus(
          ok
            ? { state: "ready", mode: "public-demo" }
            : {
                state: "degraded",
                reason: "public backend unhealthy",
                attempts: 0,
                last_error: "healthz failed",
              },
        );
        return { ok, generation: currentGeneration };
      }

      resetRestartState();
      stopHealthWatcher();
      stopExternalHealthWatcher();
      emitStatus({
        state: "restarting",
        attempt: 1,
        backoff_ms: restartDelayMs,
        reason: "manual restart",
      });
      if (typeof stopBackendProc === "function") {
        await stopBackendProc();
      } else {
        killBackendProc?.();
      }
      if (isShuttingDown()) {
        return { ok: false, reason: "shutting-down", generation: currentGeneration };
      }
      await waitForDelay();
      if (isShuttingDown()) {
        return { ok: false, reason: "shutting-down", generation: currentGeneration };
      }
      // The supervisor owns launch selection.  It checks the packaged binary
      // before even considering source-mode Python discovery.
      await spawnBackendAndWatch();
      return { ok: true, generation: currentGeneration };
    })();
    inFlight = operation;
    void operation.then(
      () => {
        if (inFlight === operation) inFlight = null;
      },
      () => {
        if (inFlight === operation) inFlight = null;
      },
    );
    return operation;
  };
}

module.exports = {
  createManualBackendRestart,
  stopBackendProcess,
  stopWindowsProcessTree,
};
