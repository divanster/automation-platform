# Windows DPAPI secret store (AI-SEC-1b)

Status: **implemented as a backend component; wired ONLY into the managed AI credential API and the AI worker's credential resolution (AI-SEC-2, section 8).** No other setting, API, provider or Request Intake code uses it. It is a
`SecretStore` (see `backend/apps/secret_store/store.py`) whose values are encrypted by Windows itself. No encryption algorithm is implemented here.

Code: `backend/config/secret_store_dpapi.py`. Tests: `backend/tests/test_secret_store_dpapi.py` (store and contract), `test_secret_store_dpapi_concurrency.py` (multiprocess, section 1.1) and `test_secret_store_dpapi_hardening.py` (identity and filesystem, section 1.2).

## 1. Design

| Aspect | Decision |
|---|---|
| Encryption | `CryptProtectData` / `CryptUnprotectData` (crypt32) through `ctypes`. **Current-user scope**: the `CRYPTPROTECT_LOCAL_MACHINE` flag is never set; `CRYPTPROTECT_UI_FORBIDDEN` is always set. |
| Entropy | DPAPI's own optional entropy parameter: a fixed application tag plus the reference. A ciphertext file copied under another reference does not decrypt. The entropy is not a secret. |
| Storage directory | **Explicit.** The caller passes it; there is no default and no environment lookup. It must meet the location rules of section 1.2: absolute, local, on a fixed ACL-capable volume, outside any Git working tree, with no link, junction or other reparse point anywhere in its path. |
| Permissions | `prepare_windows_secret_directory(path)` creates the directory (only the directory itself) with a protected DACL (no inheritance) granting full control to the current identity only. Every operation re-verifies owner and ACL (section 1.2). It never changes or adopts an existing directory that is not already trusted. |
| One file per reference | `sha256(tag + reference).hexdigest() + ".dpapi"`: opaque, fixed length, no secret, no path characters, no case collisions. Content: a 5-byte format tag and the DPAPI blob. |
| Atomic replacement | The ciphertext is written to a new `.<random>.partial` file in the same directory, flushed, then installed with `os.rename` (create: refuses an existing target) or `os.replace` (rotation). On any failure the partial file is removed and the previous file is untouched. The whole sequence runs under the lock of section 1.1. |
| No plaintext on disk | Only the DPAPI blob is ever written, including to the partial file. |
| Operations | `put(ref, value)` creates and refuses an existing reference; `put(ref, value, replace=True)` rotates and refuses a missing one; `delete(ref)`; `get(ref)`; `exists(ref)`. No upsert. The `SecretStore` interface is unchanged. |

One addition to the shared abstraction: the error code `secret_unreadable` ("The stored secret cannot be read."), used for a ciphertext that cannot be decrypted (wrong
Windows identity, damaged, truncated, swapped or oversized file). `secret_store_unavailable` stays for an unusable directory, an unsupported platform or an I/O error. A second code, `secret_store_busy` ("The secret store is busy; try again later."), reports a lock or file-sharing wait that outlasted the deadline (section 1.1).

### 1.1 Cross-process concurrency (AI-SEC-1c)

Django, the worker and an administrator command may use one directory at the same time. Every operation therefore runs under a lock that all processes share:

| Aspect | Decision |
|---|---|
| Primitive | The Windows `LockFileEx` API on `.store.lock` in the (ACL-verified) store directory, 1 byte at offset 0, on a handle each operation opens itself. Not a named mutex: nothing is shared outside the protected directory, there is no session or namespace question, and no other account can hold it. |
| Modes | **Shared** for `get` and `exists` (readers run together); **exclusive** for `put` (create and replace) and `delete`. Readers never meet a rename or an unlink, and writers are serialized. |
| Critical section | The existence check and the install (rename, replace, unlink) are inside one exclusive hold. This gives create-only, replace-only, no lost update and no resurrection of a secret that a delete already removed. DPAPI encryption runs before the lock; decryption runs after the shared lock is released. |
| Release | The lock belongs to the handle. Windows releases it when the process exits or is killed, so there is no stale lock and nothing to clean up. |
| Waiting | One deadline per operation (default 5 s, `WindowsDpapiSecretStore(directory, timeout=...)`, above 0 and at most 60). The wait polls with `LOCKFILE_FAIL_IMMEDIATELY` (2 ms growing to 25 ms). An expired wait is `secret_store_busy`. |
| Third-party handles | A scanner or indexer holding the secret file open makes rename or unlink fail with a sharing violation; the store retries inside the same deadline and then reports `secret_store_busy`. Other file errors are `secret_store_unavailable` at once. |
| Interrupted writes | An exclusive writer first deletes every `.<random>.partial` file. None can belong to a live writer, so each is the ciphertext-only debris of an interrupted write. The previous file is untouched until the atomic install. |
| Lock file | `.store.lock` stays in the directory, empty. It must be a regular file; a directory or link of that name is refused. A reader may create it, inside the already verified directory. |

The lock is cooperative: it protects against other users of this store, not against a process that edits the files directly (which needs the same Windows account anyway).

### 1.2 Identity and filesystem hardening (AI-SEC-1d)

| Aspect | Decision |
|---|---|
| SID comparison | Windows writes some SIDs back as SDDL aliases (`SY` for `S-1-5-18`, `LS` for `S-1-5-19`, `NS` for `S-1-5-20`, `LA` for the local administrator). Every owner and trustee is resolved to a canonical SID string, by Windows (`ConvertStringSidToSid`), before it is compared. An owner or trustee that does not resolve is untrusted. |
| Supported identities | Ordinary local, domain and group-managed service accounts (`S-1-5-21-…`, RID 500 or at least 1000), Azure AD accounts (`S-1-12-1-…`) and virtual service accounts (`S-1-5-80-…`). The identity is the thread token when the thread impersonates (the one DPAPI uses), else the process token. |
| Built-in service identities | LocalSystem `S-1-5-18`, LocalService `S-1-5-19` and NetworkService `S-1-5-20` are **refused unless the operator names that exact SID**: `WindowsDpapiSecretStore(directory, approved_identity_sids=["S-1-5-19"])` and the same argument of `prepare_windows_secret_directory`. Every other identity (built-in groups, Guest, anonymous, Everyone, AppContainer) is refused and cannot be approved by accident: an approval must be a well-formed SID string. An approval only allows the identity; the directory must still have been prepared for it. |
| Pinned identity | `expected_identity_sid` (`AP_SECRET_STORE_EXPECTED_IDENTITY_SID` in the composition) names the one SID the process must run as: any other identity fails closed (`secret_store_unavailable`) at construction and on every check. Set it identically for the web process and the worker so both use the intended account (section 8). It must be a well-formed SID; it is not an approval of a built-in identity. |
| All or nothing set-up | `prepare_windows_secret_directory` validates the identity and the location **before** it creates anything, creates only the directory itself (its parent must exist and be free of links; no parent is created), and removes the directory again if its own verification fails. A refusal leaves nothing behind. |
| Existing directories | Never changed and never adopted unless already trusted (trusted owner, protected restrictive ACL, no links in the path) **and** holding only this store's own files (`<64 hex>.dpapi`, partial files, `.store.lock`). An existing loose or foreign directory is refused, and its permissions are not touched. |
| Directory owner | The owner must be the current identity, SYSTEM or Administrators (an elevated administrator's new objects are owned by Administrators). A directory somebody else owns can have its ACL rewritten by that owner, so its ACL cannot be trusted. |
| Directory ACL | Protected (no inherited entries), allow entries only, trustees limited to the current identity, SYSTEM and Administrators, and at least one entry for the current identity. |
| Location | A plain local path on a fixed volume that keeps ACLs (`DRIVE_FIXED` and `FILE_PERSISTENT_ACLS`): no UNC, device (`\\.\`) or extended-length (`\\?\`) prefix, no drive-relative path, no stream (`:`) or wildcard syntax, no trailing dot or space, no removable, network, optical or RAM drive, no FAT. A UNC or device path is refused before any file system call, so nothing reaches the share. A path with a component starting with `OneDrive` is refused as a heuristic for cloud-synced folders. |
| Links and reparse points | Every component of the path, from the drive root to the directory, is checked on every operation: a symbolic link, junction, mount point or any other reparse point is refused (a junction in a parent is refused, not only one at the directory). A secret file or the lock file that is a link, a directory, a reparse point or has a second name is refused. |
| Secret files | The file is opened first, then checked **through the open handle**: regular file, exactly one name (a hard link to or from anywhere else is refused), no reparse point, trusted owner, and an ACL that names only the current identity, SYSTEM and Administrators with allow entries (inherited entries are expected). Only then is a byte read. The new file of a write is checked the same way before the ciphertext is written. A tampered file is refused, can be deleted, and is replaced by an ordinary rotation (the new file inherits the directory ACL again). |
| Errors | Every refusal is `secret_store_unavailable` with a fixed message. A backend error, such as a file error that names a path, is not chained to it: the context and cause are cleared. No local variable holds the decrypted text on the failure path of `get`. |

## 2. Service identity

* **Encrypting account:** the Windows account the process runs as when it calls `put`. DPAPI derives the protection from that account's master key.
* **Decrypting account:** exactly the same account. Another account, including an administrator or SYSTEM, gets `secret_unreadable`; the directory ACL also keeps other accounts from opening the file.
* **Django and the worker:** whichever process stores a secret and whichever process reads it must run as the **same Windows account**, with that account's profile loaded. If the
  Django process writes and the worker reads, they must share one account. If they run under different accounts, this store cannot serve both; a design for that is outside this slice.
* **Development and production accounts are not assumed identical.** A secret stored by a developer's account is unreadable by a service account and the reverse. Each environment
  stores its own secrets under its own account and its own directory.
* **Changing the service account**, resetting its password (rather than changing it), deleting or recreating its profile, or changing its SID makes every stored secret unreadable.
  Re-enter them under the new account (`put(..., replace=True)` by the new identity overwrites an unreadable file). Domain accounts may be recoverable through domain key backup, which is not
  tested or relied on here.
* **Reinstallation and machine migration:** the files are not portable. Copying them to another machine, or to the same machine after an OS reinstall or a new profile, does not work; secrets must be re-entered.
* **Service-account profile.** DPAPI's current-user scope uses the profile of the account the process runs as, and its master key lives there. The profile must exist and load for the service: a service
  that runs as a user needs "Log on as a service" and the user profile; virtual accounts (`NT SERVICE\<name>`) and group-managed service accounts get a profile when the service starts; LocalSystem,
  LocalService and NetworkService each have their own profile (not the interactive user's), so a secret stored interactively is NOT readable by them, and the reverse. A service whose profile does not load
  fails at the first DPAPI call (`secret_store_unavailable` on write, `secret_unreadable` on read). If a service impersonates a user, DPAPI uses the impersonated user. The check of this section
  was exercised with an ordinary user account; **service runs under LocalSystem, LocalService, NetworkService, a virtual account and a gMSA were not tested**.
* **Unsupported identity fails closed** before any directory or file is touched (section 1.2). To run as LocalSystem, LocalService or NetworkService the operator must name the SID explicitly.

## 3. Failure safety

* Wrong identity, corrupted, truncated, empty, oversized, wrong-format, wrong-version and swapped files all fail with `secret_unreadable`. Nothing is returned, defaulted or repaired.
* No fallback to environment variables, another store or another reference.
* Every error has a fixed message. No reference, path, secret or Windows error text is in a message, a `repr`, a log line or a chained exception (the context and cause of a store error are cleared). The module logs and prints nothing. Local variables of a failing frame (for example the plaintext bytes inside a failing DPAPI call) can still be seen by a debugger or by a handler that captures locals; do not enable such capture around the store.
* An unverifiable directory fails every operation, including `exists`, with `secret_store_unavailable`.
* A lock or file-sharing wait that outlasts the deadline fails with `secret_store_busy` and changes nothing; the caller may retry.
* On any platform but Windows the Windows backend fails closed at construction.

## 5. Backup, restore and machine migration

Secrets are bound to a Windows identity on one machine. This has consequences for every kind of copy:

* **DPAPI identity binding.** A ciphertext decrypts only for the account that wrote it (and, for a local account, only with that profile's master key) and only with the same reference. A copy restored under another account, another profile or another machine is `secret_unreadable`; the store never substitutes an environment value or another reference.
* **Machine migration.** The files are not portable. A new machine, a reinstalled Windows, a recreated profile or a changed SID cannot read them. Domain key backup might recover a domain account's keys; that is neither tested nor relied on here.
* **Rollback risk.** The store keeps no version and no high-water mark outside the directory, so **restoring an older backup is not detected**: an older ciphertext of the same account decrypts and is returned, a rotated credential silently reverts to its old value (a revoked one may be reactivated), and a secret that was deleted comes back. Two tests state this limitation on purpose. A backup of the secret directory is therefore a copy of old credentials: protect it like them and do not keep one.
* **No automatic restoration.** A missing secret is `secret_not_found`. Nothing is restored, regenerated or read from a backup, from the environment or from another store.
* **Restored permissions.** A restored or copied directory normally has inherited permissions and is refused (and not adopted) by `prepare_windows_secret_directory`. Do not restore into place. Prepare a new directory and put the secrets in it again.
* **Recommended policy.** Do not back up or migrate the secret directory. After a restore, a migration or any change of service account: prepare a new directory under the target account and **re-enter every secret** (`put`), then revoke the old credentials at their source if the old directory or a backup may have leaked.

## 6. Operating it

Nothing calls these yet. Intended use, from a process running as the target account:

```python
from config.secret_store_dpapi import WindowsDpapiSecretStore, prepare_windows_secret_directory

prepare_windows_secret_directory(r"C:\ProgramData\AutomationPlatform\secrets")   # once; the parent must exist; creates only this directory, current identity only
store = WindowsDpapiSecretStore(r"C:\ProgramData\AutomationPlatform\secrets")    # add approved_identity_sids=["S-1-5-19"] only to run as LocalService
store.put("AP_AI_CRED_CORP_LLM", value)                           # create
store.put("AP_AI_CRED_CORP_LLM", new_value, replace=True)         # rotate
```

Choose a local fixed-disk path that is not in the repository, not under a OneDrive or other synced folder and not behind a junction or `subst` drive.

## 7. Limits and not done

* DPAPI does **not** protect against code running as the same Windows user: it can call `CryptUnprotectData` itself. It protects against other accounts and against offline copying of the files.
* Memory is not erased beyond the `ctypes` buffers the module zeroes; Python strings holding a secret are not wiped.
* The lock is cooperative and has no fairness guarantee. A steady stream of readers can delay a writer until its deadline (`secret_store_busy`); callers should treat `secret_store_busy` as retryable.
* Durability across a power loss is the file system's: the file is flushed before it is installed, but the directory entry is not separately flushed (Windows offers no portable call for it).
* Administrators and SYSTEM stay in the accepted ACL, so a local administrator can read, delete or replace the ciphertext (not decrypt it without the account's key), take ownership, or read process memory. Rollback by an administrator or by a restore is not detected (section 5).
* A `subst` or mapped drive is judged by `GetDriveType` only; a `subst` drive over a network or removable location may not be recognized. The `OneDrive` rule is a name heuristic, not detection of sync clients.
* The secret store is not part of a database transaction, so the credential API cannot make a store change and its audit event atomic. It orders them (audit INSERT first, store change last, then COMMIT), never deletes or overwrites a credential to guess what a failed commit did, and reports an uncertain outcome in a fixed log line. There is an **accepted crash window** and an **uncertain-commit** case in which a credential changes without a committed audit event; section 9 describes both and the operator's reconciliation.
* No migration of environment credentials, no UI, no per-company policy, no list or export of stored secrets, no automatic rotation.
* Concurrency was tested with real child processes on one account and one local NTFS volume (including killed lock holders and a writer killed mid-write); network drives, other file systems and heavy antivirus load were not tried.
* Tested on one developer account only: **a real wrong-account decryption was not performed** (it is simulated with a fake protector, a patched SID and the owner of `C:\Windows`; a real blob failing on a different entropy exercises the same failure path).
  A foreign owner on a directory the test account controls cannot be created without privileges, so that rule is tested with the real owner of the Windows directory and the pure rules.
  The symbolic-link refusal test is skipped when the account cannot create links (junctions are tested). Behaviour on ReFS, FAT, network drives and non-English Windows was not tried.

## 8. Wiring: the managed AI credential API and the worker (AI-SEC-2)

The store is used by exactly two things, both in the backend, and nothing else may import it (a boundary test pins the importers):

* **The credential API** (`POST`, `PUT`, `DELETE`, `GET /api/ai-credentials/...`, administrator only, session and CSRF) creates, replaces, deletes and reports the status of a credential under a reference `AP_AI_CRED_[A-Z0-9_]{1,48}`. Create never overwrites, replace never creates. Each success records one audit event (`ai_credential.created`, `.replaced`, `.deleted`: actor, time, an opaque subject id, the backend identifier; no value, no reference name, no path). A value is never returned, logged, put in an error or stored in the database. Contract and errors: [ai-gateway.md](ai-gateway.md), section 20; transport: [rest-api-slice.md](../development/rest-api-slice.md), section 20.
* **The AI worker and the status probe** read a managed credential when an AI connection's `credential_source` is `managed`: once at start-up, from the store the process is configured for. A missing or unreadable credential stops the worker. **There is no fallback**: a managed credential never reads the environment and an environment credential never reads the store.

**Configuration (backend environment only; the SAME values for the web process and the worker):** `AP_SECRET_STORE_BACKEND` (`disabled`, the default, or `windows_dpapi`), `AP_SECRET_STORE_DIRECTORY`, and optionally `AP_SECRET_STORE_TIMEOUT_SECONDS`, `AP_SECRET_STORE_APPROVED_IDENTITY_SIDS` and `AP_SECRET_STORE_EXPECTED_IDENTITY_SID`. With the default every operation fails with `secret_store_disabled` (the API answers `503`) and a managed connection stops the worker.

**Service identity requirements.** The web process writes and the worker reads, so both must run as the same Windows account; set `AP_SECRET_STORE_EXPECTED_IDENTITY_SID` to that account's SID in both so any other account refuses to use the store. If they differ, the worker cannot decrypt (`secret_unreadable`) and refuses to start, and the status shows `unreadable`. Changing the service account, resetting its password or recreating its profile makes every stored secret unreadable: re-enter them (section 5). A credential changed through the API reaches a running worker only when the worker is restarted. The store has no support for two accounts that must both read and write.

## 9. Credential operations, the database transaction and failure recovery (AI-SEC-2)

The credential API changes TWO systems that cannot commit together: the PostgreSQL audit event and the secret store (DPAPI files). This section is the contract for how they are kept consistent and what is NOT guaranteed.

**Order of one create, replace or delete** (`apps/ai_credentials/services.py`): (1) authorization, (2) reference and value validation, (3) the store object is built (no I/O), (4) `BEGIN`, (5) **the reference's transaction-level advisory lock** (`pg_advisory_xact_lock`, keyed on the opaque subject id; for a delete then also the settings row lock, in that order only), (6) for create and replace the list of connections that read the credential is read (inside the transaction), (7) the audit event is INSERTed, (8) the secret store is changed (durable at once), (9) `COMMIT`, (10) a log line. No database access happens after the commit.

**What this guarantees.**

* A refusal, a validation error, an audit failure, a failed lock or read, and every failure of the store call itself are **definite**: nothing changed in the store, the audit INSERT is rolled back, nothing is logged as uncertain. A store call fails before any change (its install is one atomic rename or unlink).
* **Audit without a change is impossible** on any path of this code: the audit event can only commit after the store change succeeded, and nothing deletes or overwrites a credential afterwards.
* **Concurrent operations on one reference run one after the other** (the advisory lock is held to commit or rollback), so the audit order of two rotations is the order of their store writes and the last audited writer is the writer of the current value. Different references have different keys and are not delayed by each other. A settings save takes only the settings row lock and no advisory lock, and a delete takes the reference lock before the row lock, so there is no lock cycle and no global lock. A waiting operation blocks in PostgreSQL while the holder performs the store call (bounded by the store's own 5 s lock wait and retries); no `lock_timeout` is configured.
* A failed commit never causes a compensating action. **The outcome of a failed COMMIT is unknown to the process**: the server may have committed and the acknowledgement been lost, so a delete after "failure" could remove a credential whose creation was correctly committed and audited. Nothing is deleted, restored or overwritten to guess it; the original exception and traceback propagate unchanged (any `BaseException`: `KeyboardInterrupt`, `SystemExit`, cancellation).

**What is NOT guaranteed (accepted limitations).**

1. **Uncertain commit.** If the commit fails after the store changed, the credential has changed but the audit event may or may not exist. The process logs ONE fixed ERROR line: `AI credential <created|replaced|deleted>: the secret store may have changed but the audit commit was not confirmed; reconcile (actor=<public id> subject=<opaque id> store=<backend>).` It never contains a reference, value, path or exception text. The same line is logged when the store call itself is interrupted by a `BaseException` (the change may or may not have happened). The API answers an error although the store may have changed.
2. **Crash window.** If the process is killed (or the machine loses power) between the store change and the commit, the server rolls the audit INSERT back and the credential stays changed with no event and no log line (nothing was alive to write one). The window is the time of one round trip to the database; a crash during the store call leaves at most a partial ciphertext file, which the next writer sweeps.
3. A replace cannot be undone: the previous value is gone from the moment the store changed.

**Operator reconciliation** (after any `5xx` from a credential write, after an ERROR line above, or after any crash or power loss during one; the runbook, section 5.11, has the steps):

1. `GET /api/ai-credentials/<reference>/` shows what the store holds now (`set`, `not_set`, `unreadable`) and which connections read it. It never shows the value.
2. Look for the event in the audit table (`audit_auditevent`, read-only, by the database owner or the Django shell; there is no API for it): `action` `ai_credential.created`, `.replaced` or `.deleted` and `subject_id` equal to the opaque id in the log line.
3. Make the audit trail match reality with a NEW, audited operation: if the credential is `set` and no event exists, record it with `PUT` (an audited `replace`; supply the value the provider currently accepts, which you must know: the API never returns it); if it is `not_set` and a connection still names it, re-create it with `POST` or remove it from the connection; if an event exists and the store disagrees, `PUT` or `DELETE` as intended.
4. Restart the worker (it reads a credential once, at start-up) and treat the unexplained change as a security-relevant event: if the new value is not the one an administrator intended, rotate the credential at the provider.

**Residual and tested.** The DB-free tests prove the ordering, the lock order, the absence of any compensating call (including a static check that only the requested operation touches the store), the preserved exception, the fixed log line and the single log per uncertain failure, with `BaseException`. The PostgreSQL tests (concurrent rotations and `pg_locks`, a real definite and a real ambiguous COMMIT failure, a killed child process before and after the store change) are **written and NOT run**; until the owner runs them none of this is evidence on a real server. The check of a crash during a real DPAPI write, a power loss, and a PostgreSQL failover was not made.
