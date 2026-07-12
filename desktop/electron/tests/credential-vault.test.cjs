"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  createCredentialVault,
  normalizedHttpsOrigin,
} = require("../credential-vault.cjs");

const official = "https://echodesk.yoliyoli.uk";
const safeStorage = {
  isEncryptionAvailable: () => true,
  getSelectedStorageBackend: () => "gnome_libsecret",
  encryptString: (value) => Buffer.from(`encrypted:${value}`, "utf8"),
  decryptString: (value) => value.toString("utf8").replace(/^encrypted:/, ""),
};

function fixture(backendBase = official) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-vault-"));
  const target = path.join(root, "credential.bin");
  const vault = createCredentialVault({
    safeStorage,
    target,
    backendBase,
    officialBackendBase: official,
    enabled: true,
  });
  return { root, target, vault };
}

function linuxFixture(storage) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-vault-linux-"));
  const target = path.join(root, "credential.bin");
  const vault = createCredentialVault({
    safeStorage: storage,
    target,
    backendBase: official,
    officialBackendBase: official,
    enabled: true,
    platform: "linux",
  });
  return { root, target, vault };
}

test("origin-bound encrypted envelope roundtrips enrollment identity", () => {
  const { root, target, vault } = fixture();
  try {
    const credential = "c".repeat(48);
    const enrollmentId = "e".repeat(48);
    assert.equal(vault.store(credential, enrollmentId), true);
    assert.deepEqual(vault.readIdentity(), { credential, enrollmentId });
    const credentialFile = fs.statSync(target);
    assert.equal(credentialFile.isFile(), true);
    if (process.platform !== "win32") {
      assert.equal(credentialFile.mode & 0o777, 0o600);
    }
    assert.deepEqual(
      fs.readdirSync(root).filter((name) => name.includes(".tmp-")),
      [],
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("credential rotation is durable and two-phase across a process restart", () => {
  const { root, target, vault } = fixture();
  try {
    const current = "c".repeat(48);
    const enrollmentId = "e".repeat(48);
    const pending = "n".repeat(48);
    vault.store(current, enrollmentId);

    assert.equal(vault.beginRotation(pending), pending);
    assert.equal(vault.beginRotation("x".repeat(48)), pending);
    assert.deepEqual(vault.readIdentity(), { credential: current, enrollmentId });
    assert.deepEqual(vault.readRotationState(), {
      credential: current,
      enrollmentId,
      pendingCredential: pending,
      enrollmentConfirmed: true,
    });

    const reopened = createCredentialVault({
      safeStorage,
      target,
      backendBase: official,
      officialBackendBase: official,
      enabled: true,
    });
    assert.deepEqual(reopened.readRotationState(), {
      credential: current,
      enrollmentId,
      pendingCredential: pending,
      enrollmentConfirmed: true,
    });
    assert.equal(reopened.commitRotation(pending), true);
    assert.deepEqual(reopened.readIdentity(), {
      credential: pending,
      enrollmentId,
    });
    assert.equal(reopened.readRotationState().pendingCredential, null);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("pending enrollment persists before network and confirms without changing identity", () => {
  const { root, target, vault } = fixture();
  try {
    const credential = "c".repeat(48);
    const enrollmentId = "e".repeat(48);
    assert.equal(vault.beginEnrollment(credential, enrollmentId), true);
    assert.deepEqual(vault.readRotationState(), {
      credential,
      enrollmentId,
      pendingCredential: null,
      enrollmentConfirmed: false,
    });

    const reopened = createCredentialVault({
      safeStorage,
      target,
      backendBase: official,
      officialBackendBase: official,
      enabled: true,
    });
    assert.equal(reopened.confirmEnrollment(credential, enrollmentId), true);
    assert.deepEqual(reopened.readRotationState(), {
      credential,
      enrollmentId,
      pendingCredential: null,
      enrollmentConfirmed: true,
    });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("pending enrollment fails closed when encrypted storage is unavailable", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "echodesk-vault-disabled-"));
  const target = path.join(root, "credential.bin");
  try {
    const vault = createCredentialVault({
      safeStorage: { ...safeStorage, isEncryptionAvailable: () => false },
      target,
      backendBase: official,
      officialBackendBase: official,
      enabled: true,
    });
    assert.equal(vault.beginEnrollment("c".repeat(48), "e".repeat(48)), false);
    assert.equal(fs.existsSync(target), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Linux basic_text and unknown safeStorage backends fail closed without reading old files", () => {
  for (const backend of ["basic_text", "unknown", null]) {
    let decryptCalls = 0;
    const storage = {
      ...safeStorage,
      getSelectedStorageBackend: () => backend,
      decryptString: (value) => {
        decryptCalls += 1;
        return safeStorage.decryptString(value);
      },
    };
    const { root, target, vault } = linuxFixture(storage);
    try {
      fs.writeFileSync(target, Buffer.from("existing-insecure-identity"));
      assert.equal(vault.isAvailable(), false);
      assert.equal(vault.readIdentity(), null);
      assert.equal(vault.beginEnrollment("c".repeat(48), "e".repeat(48)), false);
      assert.equal(decryptCalls, 0);
      assert.equal(fs.readFileSync(target, "utf8"), "existing-insecure-identity");
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

test("Linux safeStorage backend detection fails closed on missing or throwing API", () => {
  for (const storage of [
    {
      isEncryptionAvailable: safeStorage.isEncryptionAvailable,
      encryptString: safeStorage.encryptString,
      decryptString: safeStorage.decryptString,
    },
    {
      ...safeStorage,
      getSelectedStorageBackend: () => {
        throw new Error("secret service unavailable");
      },
    },
    {
      ...safeStorage,
      isEncryptionAvailable: () => {
        throw new Error("safeStorage unavailable");
      },
      getSelectedStorageBackend: () => "gnome_libsecret",
    },
  ]) {
    const { root, target, vault } = linuxFixture(storage);
    try {
      assert.equal(vault.isAvailable(), false);
      assert.equal(vault.store("c".repeat(48), "e".repeat(48)), false);
      assert.equal(fs.existsSync(target), false);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

test("Linux libsecret and KWallet backends remain available", () => {
  for (const backend of ["gnome_libsecret", "kwallet", "kwallet5", "kwallet6"]) {
    const { root, vault } = linuxFixture({
      ...safeStorage,
      getSelectedStorageBackend: () => backend,
    });
    try {
      assert.equal(vault.store("c".repeat(48), "e".repeat(48)), true);
      assert.equal(vault.read(), "c".repeat(48));
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

test("macOS and Windows do not require the Linux storage backend API", () => {
  for (const platform of ["darwin", "win32"]) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), `echodesk-vault-${platform}-`));
    try {
      const vault = createCredentialVault({
        safeStorage,
        target: path.join(root, "credential.bin"),
        backendBase: official,
        officialBackendBase: official,
        enabled: true,
        platform,
      });
      assert.equal(vault.store("c".repeat(48), "e".repeat(48)), true);
      assert.equal(vault.read(), "c".repeat(48));
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

test("aborting a pending rotation preserves the current credential", () => {
  const { root, vault } = fixture();
  try {
    const current = "c".repeat(48);
    const pending = "n".repeat(48);
    vault.store(current, "e".repeat(48));
    vault.beginRotation(pending);
    assert.throws(() => vault.abortRotation("z".repeat(48)), /pending credential mismatch/);
    assert.equal(vault.abortRotation(pending), true);
    assert.equal(vault.read(), current);
    assert.equal(vault.readRotationState().pendingCredential, null);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("same encrypted envelope is rejected by a different backend origin", () => {
  const { root, target, vault } = fixture();
  try {
    vault.store("c".repeat(48), "e".repeat(48));
    const other = createCredentialVault({
      safeStorage,
      target,
      backendBase: "https://other.example.test",
      officialBackendBase: official,
      enabled: true,
    });
    assert.equal(other.readIdentity(), null);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("legacy raw credential migrates only on official origin", () => {
  const { root, target, vault } = fixture();
  try {
    fs.writeFileSync(target, safeStorage.encryptString("l".repeat(48)));
    assert.deepEqual(vault.readIdentity(), {
      credential: "l".repeat(48),
      enrollmentId: null,
    });
    const migrated = safeStorage.decryptString(fs.readFileSync(target));
    assert.equal(JSON.parse(migrated).backendOrigin, official);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("credential vault refuses non-HTTPS or credential-bearing backend URLs", () => {
  assert.throws(() => normalizedHttpsOrigin("http://example.test"), /HTTPS origin/);
  assert.throws(
    () => normalizedHttpsOrigin("https://user:pass@example.test"),
    /HTTPS origin/,
  );
});
