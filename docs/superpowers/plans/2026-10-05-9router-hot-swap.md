# 9router Hot-Swap Releases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After one explicitly approved enrollment, deploy and roll back compatible 9router releases without interrupting accepted HTTP requests, SSE streams, or WebSocket sessions.

**Architecture:** Keep an installed Caddy process on `127.0.0.1:20128`. Run two independently pinned release workers, each with a private Unix-socket bridge and loopback application port. Switch a Unix-socket symlink atomically without reloading Caddy; existing streams retain their established backend connections, and the old worker remains alive until its bridges drain.

**Tech Stack:** Existing Caddy, Node built-ins (`net`, `http`, `child_process`, `node:sqlite`), Python standard library, launchd, existing Vitest and pytest.

## Global Constraints

- User request, 2026-10-05: "I want hot swap capability built so that releases moving forward dont require downtime /writing-plans".
- Current operator answers, 2026-10-05, question `architecture`: `Selected option(s) caddy`; question `drain`: `Selected option(s) preserve`; question `enrollment`: `Selected option(s) explicit`.
- These answers select the planning architecture only. First enrollment requires a separate, explicitly approved maintenance step.
- Never terminate accepted requests or WebSocket sessions because a release drain took too long.
- Keep exactly two release slots. Refuse another deployment while the inactive slot still drains.
- Keep the client URL, port `20128`, API keys, and dashboard session secrets unchanged after enrollment.
- No new npm packages. Caddy is installed; do not install a second web proxy.
- Production changes and Namespace startup are NOT authorized by this planning session.
- No provider/model changes from upstream PRs belong in this feature.
- Maintain exact-tarball qualification. A missing, stale, unreadable, or incomplete receipt refuses deployment.
- Never overwrite release directories or restore an old database during rollback.
- No request replay after an upstream request may have been sent.
- No Caddy reload, proxy restart, `launchctl kickstart -k`, CLI `killAllAppProcesses`, or forced worker termination on the healthy deployment path.
- Both workers use the same live native SQLite database. Refuse `sql.js` in managed mode.
- Schema-changing releases refuse hot swap in the first implementation. Use a separate migration workflow. Do not advertise this as zero downtime for arbitrary incompatible releases.
- Enrollment starts with a newly built, qualified protocol-1 release containing ALL managed-mode protections. A legacy `.7` or `.8` package does not become safe for overlap merely by changing its environment.
- Do not implement host-failure high availability. This feature prevents release-induced downtime, not failures of the host, provider, proxy, or operating system.
- Do not create tests or credentials in the real home directory. Use isolated temporary directories.

---

## Verified evidence and boundaries

**Verified source baseline:** Local `lfenergy/master` resolves to `a6e5f9e83737c77a9f06ad9f0c09672df327008a`; measured in this planning turn. This is not a fresh remote-state measurement. The version served by production has not been re-read in this turn. Re-read both before implementation; do not infer live version from a previously qualified `.8` tarball.

**Verified downtime cause:** `scripts/mac/9router_deploy.py:83-96, 296-327` switches the package symlink, calls `launchctl kickstart -k`, then probes. `cli/cli.js:558-559, 669-711` invokes destructive launcher cleanup. Do not use that CLI to start parallel workers.

**Verified native primitive:** Caddy `2.11.4`, Node `26.6.0`, measured in this turn. `node tests/mac/9router_hotswap.check.cjs` passed in this turn. Its isolated loopback checks cover 100 new requests on the original client keepalive socket, A/B SSE and WebSocket preservation across swap and rollback, retained static assets, exact POST/query/Host forwarding, forged-header stripping, cancellation, and a 4 MiB streamed response. No Caddy reload occurred. This proves the fake-app routing primitive, NOT packaged-app continuity, indefinite drain, launchd recovery, concurrent credential safety, or production readiness.

**Verified persistence constraints:** `src/lib/db/schema.js:8-17` enables WAL and a five-second busy timeout. `src/lib/db/adapters/sqljsAdapter.js` is an in-memory fallback that must not participate in shared concurrent writes. `open-sse/services/tokenRefresh/dedup.js` only deduplicates within one process. `src/lib/db/repos/connectionsRepo.js:262-275` merges a connection inside a transaction, but this does not serialize external OAuth refresh calls.

**Verified singleton side effects:** `src/shared/services/initializeApp.js:52-120` registers tunnel cleanup, auto-resumes tunnels/MITM, cleans connections, and starts schedulers per process. Managed workers must not duplicate infrastructure ownership. Existing native adapters exit on SIGTERM; signal only after the bridge has fully drained.

**Product documentation:**
- [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy): Unix upstreams, `keepalive off`, and streaming configuration.
- [Caddy API](https://caddyserver.com/docs/api): reloads can change configuration without listener downtime.
- [Caddy stream close delay](https://caddyserver.com/docs/json/apps/http/servers/routes/handle/gbox/reverse_proxy/reverse_proxy/stream_close_delay): config unload can close long-lived sessions. A finite delay does not satisfy indefinite preservation. Therefore do NOT reload this reverse-proxy handler for releases.
- [Node IPC](https://nodejs.org/api/net.html#ipc-support): Unix-socket pathname limits apply. Keep runtime paths short and validate encoded length.

**Deliberate ceiling:** `ponytail: two slots, same-schema releases, one host. Add migration compatibility and multi-host failover only as separate requested features.`

## File map

Create:
- `scripts/mac/9router_worker.cjs`: pinned app child, private bridge, private control socket, drain state.
- `scripts/mac/9router_hotswap.py`: deployment lock, slot state, route transaction, recovery and worker lifecycle.
- `scripts/mac/templates/9router.Caddyfile`: stable proxy configuration; generated paths, no per-release config reload.
- `scripts/mac/templates/com.lfenergy.9router-proxy.plist`: persistent proxy label.
- `scripts/mac/templates/com.lfenergy.9router-worker.plist`: per-slot launchd label, concrete release path.
- `tests/mac/9router_hotswap.check.cjs`: assert-based native Caddy/worker integration check.
- `tests/mac/test_9router_hotswap.py`: deploy/recovery failure tests in temporary state.
- `tests/unit/token-refresh-cross-process.check.mjs`: two-process OAuth deduplication proof.

Modify:
- `scripts/mac/9router_deploy.py`: delegate enrolled deployments/rollback; retain legacy enrollment path and existing package/receipt validations.
- `scripts/mac/9router_vm_qualify.py`: qualify swapping under traffic; hash runtime bundle; stop guest compute on every exit path.
- `scripts/mac/9router_devbox_baseline.sh` and `scripts/mac/9router_devbox_restore.sh`: provision/restore proxy and slots after approved devbox activation.
- `scripts/mac/9router_path_watchdog.py`: distinguish stable proxy and active worker; never restart a healthy draining worker.
- `open-sse/services/tokenRefresh/dedup.js`: extend the existing dedup boundary across managed workers.
- `src/lib/db/driver.js`: enforce native adapter and prohibit startup migration in managed mode.
- `custom-server.js`: report accepted response/upgrade work and quiescence over private child IPC.
- `src/shared/services/initializeApp.js`: bypass singleton infrastructure startup/cleanup in managed workers.
- `src/sse/services/backgroundTokenRefresh.js`: only the active slot runs proactive refresh ticks.
- `src/sse/services/tokenRefresh.js` and `src/lib/db/repos/connectionsRepo.js`: persist rotated credential updates using a monotonic generation/compare-and-swap contract.
- `src/app/api/providers/[id]/models/route.js`, `src/app/api/providers/[id]/test/testUtils.js`, and `src/app/api/v1/models/route.js`: audit direct refresh calls; route token-backed calls through the shared dedup boundary where they currently bypass it.
- `cli/scripts/build-cli.js`: emit a compatibility manifest from schema/migration inputs; bundle worker-required artifacts.
- `src/app/api/version/update/route.js` and `src/app/api/version/shutdown/route.js`: refuse legacy destructive updater/shutdown operations in managed mode before any side effect.
- `tests/unit/managed-update.test.js`: prove managed dashboard update/shutdown calls do not kill, spawn an updater, or schedule process exit.
- `tests/mac/test_9router_deploy.py`, `tests/mac/test_9router_vm_qualify.py`, `tests/mac/test_9router_path_watchdog.py`: retain legacy assertions and add enrolled dispatch tests.

Do NOT modify `cli/app/` generated output by hand, production home files, live launchd configuration, Tailscale settings, provider models, or unrelated upstream fixes.

## Execution order and current proof

Execute Task 1, Task 3, Task 2, Task 4, Task 4b, Task 5, then Task 6. Shared refresh/migration protections must exist before real packaged workers overlap. Task 2 may use fake workers to develop lifecycle tests before Task 3; it may not attach two unprotected app processes to the live database.

Planning-branch proof command: `node tests/mac/9router_hotswap.check.cjs` (run from this worktree). GREEN in this turn. The check uses temporary fake applications, isolated Caddy state, and ephemeral ports. It does not start Namespace or production. All runtime feature tasks remain unimplemented; existing proof files are not deployment machinery.

## Runtime contract

Persistent configuration under `~/.9router/hotswap/`; private runtime sockets under `/tmp/9r-<uid>/` with directory mode `0700`. The persistent directory uses mode `0700`. State, receipts, generated configuration, and OAuth coordination data use mode `0600`. No token appears in argv, filenames, logs, or test failure messages.

Slots:
- A application port `21128`; socket `a.sock`; control `a.ctl`.
- B application port `21130`; socket `b.sock`; control `b.ctl`.
- Stable `active.sock` is a symlink to one ready slot socket.
- Loopback `20129` remains the combo helper; do not appropriate it.
- Reject occupied slot ports rather than killing their owners.
- Validate the runtime directory's owner, mode, and symlink status before writing.

State JSON stores `schema: 1`, active slot, each slot's release path/version/tarball hash and mode, and an optional pending transaction. It stores no credentials. The concrete release path must resolve beneath `~/.9router/releases/`. Validate the package version with fullmatch `[A-Za-z0-9][A-Za-z0-9._-]*` before joining paths.

Modes: `starting`, `ready`, `active`, `draining`, `stopped`, `failed`. Unknown control state is NOT zero connections. Only the slot bridge's actual tracked connections determine drain completion, including upgraded connections. Server-side task completion must also be proven before terminating the app: canceled clients can leave async cleanup or OAuth persistence pending.

Control uses a filesystem-protected Unix socket and newline-delimited JSON:
- Request `{"op":"status"}` → `{"slot":"a","version":"...","mode":"active","connections":2,"appPid":123}`.
- Request `{"op":"drain"}` marks the slot draining after public routing switches away. It preserves the listener and pipes so a Caddy dial already in flight can still finish.
- Request `{"op":"resume"}` reopens a draining slot only if its app is healthy; used for rollback.
- Request `{"op":"stop"}` refuses while any bridge, app request cleanup, or OAuth coordination operation remains active.
- Limit one control command to 4096 bytes. Reject unknown operations and malformed JSON.

The WebSocket self-fetch path still uses the worker's private loopback port. Do not redirect it through Caddy or the active symlink: an old WebSocket must execute subsequent turns against its original release.

### Task 1: Prove the stable proxy switching primitive

**Files:** Existing on this planning branch: `scripts/mac/templates/9router.Caddyfile`, `tests/mac/9router_hotswap.check.cjs`. Reuse them; do not overwrite with the older Appendix A sketch. Counterfactual: `tests/mac/9router_hotswap_counterfactual.check.cjs`.

**Progress, 2026-10-05:** The original check and byte-identical temporary mirror pass. Removing only the mirror's atomic symlink replacement fails the B-routing assertion with actual `a`, expected `b`, and `ERR_ASSERTION`. Task 1 proves the fake-app switching primitive only. Packaged-app continuity and full qualification belong to later tasks; neither is proven here. No deployment controller, asset-staging implementation, enrollment, or production change is included.

**Interfaces:** Consumes installed Caddy and ephemeral fake applications. Produces a runnable proof of request-level switching without proxy reload.

- [x] **Step 1: Write failing native integration assertions.**

Use Node `assert`, `http`, `net`, `fs`, `child_process`, and `events`. Start two fake HTTP applications on ephemeral loopback ports. Each bridge listens on its own temporary Unix socket. Emit SSE `data: a-start\n\n`, hold it open, and implement a standards-compliant WebSocket echo handshake. Reserve a random proxy port; never use production `20128` in the local test.

Reuse the existing `tests/mac/9router_hotswap.check.cjs`. For RED, copy the check and its template into a temporary mirror preserving their relative paths, prove that mirror passes unchanged, then remove the mirror's symlink replacement and require the B-routing assertion to fail. Never mutate the checkout to manufacture RED. The check uses a persistent client agent and tests original SSE/WebSocket sessions after switching. Extend this real-process check for the task-specific assertions below. Bind Caddy to `http://:<test-port>` plus `bind 127.0.0.1`, not a host-specific matcher that accidentally refuses other Host headers.

- [x] **Step 2: Run the check against the unswitched route.**

Run `node tests/mac/9router_hotswap_counterfactual.check.cjs`. It first runs the unchanged mirror GREEN, then removes only the mirror's atomic replacement and requires the specific B-routing assertion to fail (`a` instead of `b`). An unrelated nonzero exit does not pass this counterfactual. The checkout is never mutated.

- [x] **Step 3: Add the template and atomic switch.**

The existing template and fake-app check already implement this primitive and are retained unchanged. The controller, validated runtime/state paths, and asset staging described below remain later runtime work, not Task 1 implementation.

```caddyfile
{
    admin off
    auto_https off
}
http://:20128 {
    bind 127.0.0.1
    handle_path /_next/static/* {
        root * __STATE__/assets
        file_server
    }
    handle {
    reverse_proxy unix/__RUNTIME__/active.sock {
        flush_interval -1
        header_up -X-Real-IP
        header_up -X-9r-Real-Ip
        header_up -X-9r-Peer-Token
        header_up -X-9r-Via-Proxy
        transport http {
            keepalive off
            dial_timeout 3s
            versions 1.1
        }
    }
    }
}
```

Generate `__RUNTIME__` from the validated short runtime directory and `__STATE__` from the validated persistent directory. Asset staging copies exact `.next-cli-build/static/` relative paths beneath `__STATE__/assets/`. Reject symlinks, traversal, non-regular inputs, and existing same-name files with different bytes. Write new assets atomically before switching and retain old immutable assets. The proxy remains unchanged during staging. Caddy's forwarding-header defaults remain enabled. Do not trust client-provided forwarding headers. Do not set `stream_timeout`, `read_timeout`, retry policies, compression, or response buffering. Existing authenticated outer proxies require a separate verified trusted-proxy contract; do not broaden trust implicitly.

```python
def replace_route(runtime: Path, slot: str) -> None:
    if slot not in {"a", "b"}:
        raise ValueError("invalid slot")
    target = runtime / f"{slot}.sock"
    if not stat.S_ISSOCK(target.lstat().st_mode):
        raise RuntimeError("slot socket is not ready")
    temporary = runtime / f".active-{uuid.uuid4().hex}.sock"
    try:
        temporary.symlink_to(target)
        os.replace(temporary, runtime / "active.sock")
    finally:
        temporary.unlink(missing_ok=True)
```

Import `stat` and `uuid` in the new controller. The parent directory validation must run before this function.

- [x] **Step 4: Run the complete check.**

Run `node tests/mac/9router_hotswap.check.cjs`. Expect all assertions to pass, including post-switch HTTP on the same CLIENT keepalive connection. Test another swap back to A without changing the proxy PID. Add forged forwarding headers, POST bodies, query strings, non-200 status, cancellation, and large streamed bodies; compare against direct app responses. All these assertions already exist and passed unchanged in this Task 1 turn.

- [x] **Step 5: Commit.**

```bash
git add docs/superpowers/plans/2026-10-05-9router-hot-swap.md tests/mac/9router_hotswap_counterfactual.check.cjs
git commit -m "test(mac): prove stream-safe proxy route switching"
```

### Task 2: Add pinned workers with fail-closed draining

**Files:** Create `scripts/mac/9router_worker.cjs`, both launchd templates. Extend `tests/mac/9router_hotswap.check.cjs`. Modify `src/shared/services/initializeApp.js`, `src/sse/services/backgroundTokenRefresh.js`, and `cli/scripts/build-cli.js`.

**Interfaces:** Worker invocation `node 9router_worker.cjs <slot.json>`. Slot configuration supplies concrete `release`, `version`, `port`, `runtime`, `dataDir`, and shared secret environment loaded by its launcher. Expose the control protocol above. Never use `9router/cli.js`.

- [ ] **Step 1: Write failing drain tests.**

```javascript
const before = await control('a', 'status');
assert.equal(before.connections, 2);
await control('a', 'drain');
assert.equal(await request('/version'), 'b');
await oldWebSocket.expect('a');
await assert.rejects(() => control('a', 'stop'));
await oldSse.complete();
await oldWebSocket.close();
await waitForConnections('a', 0);
assert.equal((await control('a', 'stop')).mode, 'stopped');
```

`control(slot, op)` sends one bounded newline command and rejects malformed/error responses. `connectToSlot(slot)` connects to the slot bridge, not its app port. `waitForConnections(slot, count)` polls validated control status with a test deadline. Add missing-control and invalid-slot tests: neither may call stop.

- [ ] **Step 2: Run it red.**

Run `node tests/mac/9router_hotswap.check.cjs`. Expect missing worker/control protocol failure.

- [ ] **Step 3: Implement the worker's minimal bridge and lifecycle.**

Use this bridge body inside the worker; retain both incoming and outgoing sockets until their closure. Do not reject a racing connection that Caddy selected before the route switch. Mark draining for status and background ownership, not as an immediate socket rejection.

```javascript
const pipes = new Set();
let draining = false;
const bridge = net.createServer((incoming) => {
  const outgoing = net.connect({ host: '127.0.0.1', port: config.port });
  const pair = { incoming, outgoing };
  pipes.add(pair);
  const remove = () => {
    incoming.destroy();
    outgoing.destroy();
    pipes.delete(pair);
  };
  incoming.once('error', remove);
  outgoing.once('error', remove);
  incoming.once('close', remove);
  outgoing.once('close', remove);
  incoming.pipe(outgoing);
  outgoing.pipe(incoming);
});
```

Spawn the standalone entry using the same DNS-order and memory flags as the existing launcher, but pinned directly:

```javascript
const app = spawn(process.execPath, [
  '--dns-result-order=ipv4first', '--max-old-space-size=6144',
  path.join(config.release, 'app', 'custom-server.js'),
], {
  cwd: path.join(config.release, 'app'),
  stdio: ['inherit', 'inherit', 'inherit', 'ipc'],
  env: {
    ...process.env,
    NODE_ENV: 'production', HOSTNAME: '127.0.0.1', PORT: String(config.port),
    DATA_DIR: config.dataDir,
    NINEROUTER_ATTACHED_SERVER: '1', NINEROUTER_MANAGED_WORKER: '1',
    NINEROUTER_SLOT: config.slot,
    NINEROUTER_HOTSWAP_RUNTIME: config.runtime,
  },
});
```

Validate every configuration field before spawning, including path ownership, concrete release version, and socket pathname byte length. Wait for the private app version endpoint before listening on the bridge. Never claim ready based on TCP alone. The worker owns only its app child; it never scans or kills other PIDs. On unexpected app exit, mark failed, preserve evidence, and let launchd restart only that slot. Do not silently route a failed candidate.

The `drain` operation sets `draining = true` but leaves the bridge listening for already-selected dials. `resume` clears it only after private version verification. To retire a zero-work slot, also prove the route has pointed away continuously longer than Caddy's explicitly configured `dial_timeout 3s` and the measured scheduling grace. Use a 10-second quiet interval restarted by every accepted bridge connection. This is a routing-quiescence check, NOT a stream deadline: any positive work count waits indefinitely. Qualification must hold a pre-switch dial at a barrier, switch, then complete it successfully before retirement. If scheduler stalls make dial quiescence unprovable, retain the slot and refuse the next release; do not guess. Do not call `server.closeAllConnections()`, set a force-close timer, or signal the child during drain. Before `stop`, prove `pipes.size === 0` AND app-side async work is quiescent. Extend `custom-server.js` with a private managed-status IPC message reporting active responses/upgrades and tracked token-refresh operations; do not expose a public HTTP control API. Count completion/cancellation separately from asynchronous persistence completion. Unknown IPC status refuses stop.

In `initializeApp()`, add the explicit early managed-mode branch before infrastructure signal handlers. Validate stored settings and refuse managed enrollment if app-owned `tunnelEnabled`, `tailscaleEnabled`, or `mitmEnabled` is active; externally owned ingress must be verified during enrollment rather than assumed. Do not silently disable enabled features. Preserve ordinary unmanaged behavior. Continue required background work through an explicitly active-slot-gated startup path; an early return must not silently disable quota refresh or token refresh for both workers.

In `runBackgroundTokenRefreshTick()`, managed workers check whether `active.sock` targets their own slot before starting a tick. Draining workers stop starting background ticks, but an already-running tick may finish. Use the shared refresh serialization from Task 3. No secret is read from the active route file. Status must report active refresh tasks, including background ticks.

Worker plists use concrete absolute wrapper/config paths, `KeepAlive: true`, `ThrottleInterval: 5`, and per-slot logs. Proxy plist uses a concrete Caddy binary/config and separate logs. Render with Python `plistlib`, not unescaped XML substitutions. Planned labels are `com.lfenergy.9router-proxy`, `com.lfenergy.9router-worker-a`, and `com.lfenergy.9router-worker-b`. To stop a zero-work draining slot, bootout THAT slot label before signaling; otherwise KeepAlive would respawn it. Enrollment disables the legacy label only with current-turn approval.

- [ ] **Step 4: Test startup isolation, drain, and recovery.**

Run `node tests/mac/9router_hotswap.check.cjs`. Expect no active app requests or sockets to be killed. Test an old WebSocket sending multiple new turns after cutover. Test private self-fetch stays at its original port. Test aborted clients leave cleanup counted until completion. Test candidate crash, old worker crash, missing control status, and resume after drain. Track exact child PIDs; never use `pgrep` to select one among two workers.

- [ ] **Step 5: Commit.**

```bash
git add scripts/mac/9router_worker.cjs scripts/mac/templates/com.lfenergy.9router-proxy.plist scripts/mac/templates/com.lfenergy.9router-worker.plist tests/mac/9router_hotswap.check.cjs src/shared/services/initializeApp.js src/sse/services/backgroundTokenRefresh.js custom-server.js cli/scripts/build-cli.js
git commit -m "feat(mac): run pinned workers with preserved-session draining"
```

### Task 3: Protect shared state across overlapping releases

**Files:** Modify `src/lib/db/driver.js`, `open-sse/services/tokenRefresh/dedup.js`, relevant direct refresh dispatches, `cli/scripts/build-cli.js`, and `custom-server.js` work reporting. Create `tests/unit/token-refresh-cross-process.check.mjs`.

**Interfaces:** Preserve `dedupRefresh(provider, oldToken, fn, log): Promise<object|null>`. Managed workers share a private native SQLite coordination file, distinct from the app schema. Emit `app/hotswap-manifest.json` with protocol version and a canonical persistence fingerprint.

- [ ] **Step 1: Write a two-process regression check with a fake single-use issuer.**

```javascript
const [first, second] = await Promise.all([
  runRefreshChild('same-provider', 'old-token'),
  runRefreshChild('same-provider', 'old-token'),
]);
assert.deepEqual(first, second);
assert.equal(issuer.refreshCalls, 1);
assert.equal(first.refreshToken, 'rotated-token');
assert.equal((await runRefreshChild('same-provider', 'old-token')).refreshToken, 'rotated-token');
assert.equal(issuer.refreshCalls, 1);
```

The check imports the actual `dedupRefresh` in child processes. Each child sets its own managed slot and the SAME isolated coordination path. The issuer only accepts one refresh for a token. No real OAuth calls or credentials. Also hold one child mid-refresh, terminate it, and assert a successor refuses uncertain replay rather than issuing another refresh.

- [ ] **Step 2: Run it red.**

Run `node tests/unit/token-refresh-cross-process.check.mjs`. Expect the duplicate refresh counter or failed second result to demonstrate the current process-local defect.

- [ ] **Step 3: Extend the EXISTING dedup primitive, not each executor.**

Use native SQLite transactions for a flight claim. Key by SHA-256 of the length-delimited `(provider, oldToken)` tuple. Store no raw old token. Persist successful refresh output so a late old-worker callback cannot consume the original token again. Protect result data like the provider database. Resolve file paths from managed environment and reject unsafe directories.

```sql
CREATE TABLE IF NOT EXISTS refresh_flights (
  key TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','done','uncertain')),
  result TEXT,
  generation INTEGER,
  started_at TEXT NOT NULL
);
```

Claim under `BEGIN IMMEDIATE`/`COMMIT`, execute `fn()` OUTSIDE the transaction, then store `done` with the complete result before resolving callers. Another process waits asynchronously and reads the completed result. Use conditional updates on both key and owner. A thrown refresh, failed durable result write, or issuer response with uncertain outcome sets `uncertain`; do not delete the claim and replay. A pending owner crash also remains unresolved until operator recovery. Waiters have a finite request-level waiting deadline and fail explicitly; this does not terminate unrelated draining streams. Do not automatically reclaim a flight using a lease timeout while its refresh may still execute.

Retain the original in-memory behavior when managed mode is absent. In managed mode reject non-native coordination drivers. Keep successful flights for as long as any live worker may retain the corresponding old token; the first implementation retains them until all workers are stopped for maintenance. Do not use the existing ten-second in-memory TTL as a cross-process safety guarantee.

Audit the actual refresh dispatches using one scoped search:

```bash
rg -n 'dedupRefresh|refreshProviderCredentials|refreshTokenByProvider|refreshCodexToken|refreshGoogleToken' open-sse src/sse src/app/api
```

The planning census verified calls in chat, embeddings, search, fetch, image/video generation, models, provider tests, and background refresh. Enter tests through these dispatches. Use the shared primitive for token-backed bypasses; keep provider response translation unchanged. Prove direct calls cannot bypass shared coordination with an AST/import-aware test. Do not rely on text matching alone.

For shared app writes, retain native WAL and the current transaction merge. The deduplicated refresh result alone is insufficient: a delayed old worker could overwrite a newer token generation. Assign a durable monotonically increasing generation to each completed refresh. Return it as `refreshGeneration` with the result. In `updateProviderCredentials` pass the generation to a credential-specific compare-and-swap transaction in `connectionsRepo.js`; update token fields only when the incoming generation is greater than the stored generation, and return the current row otherwise. Store the generation in the EXISTING `providerConnections.data` JSON, not a new app-table column. `rowToConn()` and `connToRow()` already preserve JSON extras. The coordination database owns the monotonic sequence and persists it across worker restarts; deleting it while retaining credential generations is prohibited. Re-read current credentials before further refreshes. Keep unrelated project ID/usage patches independent. Pin the credential-update statement and its generation comparison with a test. Test both callback orderings: old-result persistence after new-result persistence must leave the new token intact. Add a contention check that writes usage and credential patches from two processes and confirms both fields survive. Refuse `sql.js` BEFORE it opens/exports a shared file, rather than detecting it afterward. In managed mode bypass automatic migration entirely after matching the build manifest to the enrolled database fingerprint. Never test a candidate's incompatible migrations on the live DB.

Manifest creation hashes sorted relative paths AND exact bytes of `src/lib/db/schema.js`, `version.js`, `migrate.js`, and every migration file. Include `protocol: 1`, `schemaVersion`, supported adapter, and the resulting digest. Fail if a required input cannot be read. Verify the actual enrolled SQLite layout against expected tables, columns, indexes, and applied migration metadata; a matching source manifest alone cannot prove the live database matches. Qualification mutates a database mirror to add/remove a column and requires readiness refusal. Do not treat equal schema version numbers alone as compatibility proof. This deliberately over-rejects benign migration-code changes rather than under-rejecting destructive ones.

- [ ] **Step 4: Run the safety checks.**

Run `node tests/unit/token-refresh-cross-process.check.mjs`. Expect one issuer call per token across both workers; uncertain owner death refuses replay. Run the targeted SQLite adapter tests and worker check. Verify unsafe directory, corrupted result, write failure, unsupported runtime, and changed persistence fingerprint all refuse managed readiness. Assert logs and receipt output contain no credentials.

- [ ] **Step 5: Commit.**

```bash
git add open-sse/services/tokenRefresh/dedup.js src/lib/db/driver.js cli/scripts/build-cli.js custom-server.js tests/unit/token-refresh-cross-process.check.mjs
```

Stage only direct refresh-dispatch files actually changed by this task, then `git commit -m "fix(auth): serialize refreshes across release workers"`.

### Task 4: Implement transactional deployment, rollback, and crash reconciliation

**Files:** Create `scripts/mac/9router_hotswap.py`, `tests/mac/test_9router_hotswap.py`. Modify `scripts/mac/9router_deploy.py`, `tests/mac/test_9router_deploy.py`.

**Interfaces:** Controller `deploy_release(dest: Path, digest: str) -> int`, `rollback_release(version: str|None) -> int`, `status() -> dict`, `reconcile() -> int`. Private helpers `control(slot: str, op: str) -> dict`, `verify_at(base: str, version: str) -> str|None`, `replace_route(runtime: Path, slot: str) -> None`. All failures return nonzero with a scope-specific reason.

- [ ] **Step 1: Add ordering and refusal tests.**

```python
def test_failed_candidate_never_changes_route(controller):
    controller.fail_probe("b")
    assert controller.deploy_release(controller.release_b, "b-hash") != 0
    assert controller.active_slot() == "a"
    assert controller.worker_alive("a")


def test_success_switches_only_after_verification(controller):
    assert controller.deploy_release(controller.release_b, "b-hash") == 0
    assert controller.events.index("verified:b") < controller.events.index("route:b")
    assert controller.events.index("route:b") < controller.events.index("drain:a")
    assert controller.worker_alive("a")
    assert "proxy-restart" not in controller.events


def test_busy_inactive_slot_refuses_next_deploy(controller):
    controller.set_draining("a", connections=1)
    assert controller.deploy_release(controller.release_c, "c-hash") != 0
    assert controller.active_slot() == "b"
    assert controller.worker_alive("a")
```

The `controller` fixture imports the actual module with temporary paths and fake launchd/control/probe implementations; events are collected at its side-effect boundary. Fake status must cover missing and malformed responses, not just zero and positive counts.

- [ ] **Step 2: Run the new tests red.**

Run `python3 -m pytest tests/mac/test_9router_hotswap.py -q` using an interpreter with existing pytest. Expect missing controller functions.

- [ ] **Step 3: Implement the state transaction.**

Acquire `fcntl.flock(LOCK_EX | LOCK_NB)` over a private deployment file descriptor. Hold it through install, validation, candidate startup, switch, route verification, and state commit. Return an explicit busy refusal on contention. Reconcile at entry before consuming a slot. Use atomic JSON writes plus file and parent-directory fsync for durable journal checkpoints.

Deployment order:
1. Validate receipt, hash, manifest, compatible live schema, release path, disk space, runtime directories, and inactive-slot availability.
2. Snapshot the live database with the existing SQLite backup method. Do not restore it later. Stage immutable dashboard assets with the conflict checks from Task 1.
3. Install to a new release directory. An install failure leaves A untouched.
4. Persist a `prepared` transaction with old/new slot and release identities.
5. Start B with concrete paths. Probe B directly on its loopback port for correct version, nonempty models, and a complete authenticated stream.
6. Persist `verified` transaction. Reconfirm A and B identity and health.
7. Atomically switch `active.sock` to B. Persist `switched` transaction.
8. Verify B through public `20128`, then mark A draining while preserving its listener for racing pre-switch dials.
9. Write committed state and log. Return success with `old_slot: draining` if sessions remain; do not wait forever or report a fake completed drain.
10. Reconciliation may stop A only after all bridge/cleanup/refresh work is positively zero. A busy slot remains allocated.

On public verification failure, resume A's bridge before switching back. Verify A directly, replace route to A, then verify publicly. Existing B sessions remain attached to B and drain naturally. Never force-kill B because rollback happened. If A cannot resume or verify, keep the last serving route and return an explicit rollback failure; do not manufacture a healthy verdict.

Make the socket symlink the authoritative live route on recovery. State JSON and package symlink are reconciled against it, not blindly restored. Recovery tests cover termination after every numbered side effect, including route replacement before JSON commit. When route and state conflict, identify the release via slot configuration and private control/version checks. If neither is provable, refuse mutation and preserve both workers. Keep diagnostics usable even when a probe fails.

The existing `/opt/homebrew/lib/node_modules/9router` symlink remains a convenience pointer to the committed active release. Workers NEVER launch through it. An update failure after route commit is a reported bookkeeping fault, not permission to restart a working service. Record enough pending state to repair it on reconciliation.

Refactor the existing HTTP helper to accept `base=BASE` explicitly and propagate the candidate base through `verify`, `stream_probe`, and `stream_once`. Do not mutate global `BASE` across threads. Keep legacy tests and receipt enforcement. Enrolled `deploy` and `rollback` call the controller; legacy un-enrolled deployment remains available only before the separately approved enrollment.

Controller commands: `enroll`, `deploy`, `rollback`, `status`, `reconcile`. `enroll` refuses without an explicit maintenance acknowledgement flag. That flag is friction, not operator authorization. Runtime enroll execution still requires current-turn approval.

- [ ] **Step 4: Prove failure paths.**

Run `python3 -m pytest tests/mac/test_9router_deploy.py tests/mac/test_9router_hotswap.py -q`. Include invalid version traversal, occupied port, simultaneous deployment, missing receipt, stale runtime hash, candidate auth failure, corrupted journal, failed symlink rename, failed state fsync, public verification failure, rollback failure, and unexpected worker disappearance. Run the native check to prove draining is real, not a mocked counter.

- [ ] **Step 5: Commit.**

```bash
git add scripts/mac/9router_hotswap.py scripts/mac/9router_deploy.py tests/mac/test_9router_hotswap.py tests/mac/test_9router_deploy.py
git commit -m "feat(mac): transact hot-swap deployment and rollback"
```

### Task 4b: Block dashboard release paths that kill managed workers

**Files:** Modify `src/app/api/version/update/route.js`, `src/app/api/version/shutdown/route.js`. Create `tests/unit/managed-update.test.js`.

**Interfaces:** Preserve both existing `POST()` signatures. Managed calls return HTTP 409 with a deployment-controller instruction before any process mutation. Unmanaged behavior stays unchanged. The supported release entry remains `9router_deploy.py deploy <qualified-tarball>`; this task does not expose a shell-running dashboard deployment endpoint.

- [ ] **Step 1: Add a dispatch-level regression test.**

```javascript
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const effects = vi.hoisted(() => ({
  killAppProcesses: vi.fn(async () => {}),
  spawnUpdaterAndExit: vi.fn(),
}));
vi.mock('@/lib/appUpdater', () => effects);
vi.mock('next/server', () => ({
  NextResponse: { json: (body, options = {}) => Response.json(body, options) },
}));
import { POST as update } from '../../src/app/api/version/update/route.js';
import { POST as shutdown } from '../../src/app/api/version/shutdown/route.js';

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  vi.stubEnv('NODE_ENV', 'production');
  vi.stubEnv('NINEROUTER_MANAGED_WORKER', '1');
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

describe('managed release endpoints', () => {
  for (const [name, post] of [['update', update], ['shutdown', shutdown]]) {
    it(`${name} refuses before process side effects`, async () => {
      const response = await post();
      expect(response.status).toBe(409);
      expect(await response.json()).toEqual({
        success: false,
        message: 'Managed releases use 9router_deploy.py with a qualified tarball.',
      });
      expect(effects.killAppProcesses).not.toHaveBeenCalled();
      expect(effects.spawnUpdaterAndExit).not.toHaveBeenCalled();
      expect(vi.getTimerCount()).toBe(0);
    });
  }
  it('retains unmanaged production update behavior', async () => {
    vi.stubEnv('NINEROUTER_MANAGED_WORKER', '0');
    expect((await update()).status).toBe(200);
    expect(effects.killAppProcesses).toHaveBeenCalledTimes(1);
    expect(effects.spawnUpdaterAndExit).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run RED.**

From `tests/`, run `npx vitest run unit/managed-update.test.js`. Expect managed status assertions to fail against the current 200 responses. Fake timers prevent the shutdown callback from terminating the test process.

- [ ] **Step 3: Insert this guard at the start of EACH existing `POST()` body.**

```javascript
if (process.env.NINEROUTER_MANAGED_WORKER === '1') {
  return NextResponse.json(
    { success: false, message: 'Managed releases use 9router_deploy.py with a qualified tarball.' },
    { status: 409 }
  );
}
```

The update guard precedes the production check. The shutdown guard precedes `killAppProcesses()`. Do NOT modify auth middleware or disable existing access controls.

- [ ] **Step 4: Run GREEN.**

From `tests/`, run `npx vitest run unit/managed-update.test.js`. Expect all three tests to pass. Test dashboard update and manual-shutdown refusal again through the packaged candidate during qualification.

- [ ] **Step 5: Commit.**

```bash
git add src/app/api/version/update/route.js src/app/api/version/shutdown/route.js tests/unit/managed-update.test.js
git commit -m "fix(mac): refuse destructive dashboard updates in managed mode"
```

### Task 5: Integrate watchdog, qualification, and enrollment

**Files:** Modify `scripts/mac/9router_path_watchdog.py`, `scripts/mac/9router_vm_qualify.py`, `scripts/mac/9router_devbox_baseline.sh`, `scripts/mac/9router_devbox_restore.sh`, and their existing `tests/mac/` modules.

**Interfaces:** Qualification receipt includes `protocol: 1`, exact tarball digest, deploy/controller/worker/template hashes, persistence digest, proxy version, per-check measured counts, and `result`. Host cleanup stops the named devbox and verifies stopped state via a non-activating metadata read.

- [ ] **Step 1: Write qualification and watchdog refusal assertions.**

```python
def test_watchdog_does_not_restart_healthy_draining_worker(watchdog):
    watchdog.set_slot("a", mode="draining", connections=1, healthy=True)
    watchdog.set_slot("b", mode="active", healthy=True)
    watchdog.cycle()
    assert watchdog.kickstarts == []


def test_zero_swap_subjects_cannot_pass(qualifier):
    record = qualifier.finish({"swaps": 0, "requests": 100, "errors": 0})
    assert record["result"] != "pass"


def test_runtime_bundle_hash_change_refuses_receipt(deployer):
    deployer.qualify_current_bundle()
    deployer.change_worker_bytes()
    assert deployer.qualification_reason() is not None
```

Fixtures exercise actual verdict-producing functions with temporary artifacts. Do not mutate source in the checkout; copy runtime bundles into a mirror for mutation tests.

- [ ] **Step 2: Run them red.**

Run `python3 -m pytest tests/mac/test_9router_vm_qualify.py tests/mac/test_9router_path_watchdog.py -q`. Expect missing hot-swap subject counts and runtime-hash binding failures.

- [ ] **Step 3: Add deterministic under-load qualification.**

Keep the existing provider stream and concurrency checks. Add a deterministic fake-provider fixture on the GUEST and drive it through the real packaged router with authenticated SSE and `/v1/responses` WebSockets. Arrange barriers so A's sessions are positively open before switching. Release barriers only after B has served new requests. Exercise A→B and rollback B→A; count at least two swaps, 100 completed new HTTP requests per transition, one old SSE terminal completion, and multiple old WebSocket turns across both transitions. Zero errors and zero truncated sessions are required. Do not rely on a short live-provider answer happening to overlap the switch.

Qualify three separate classes and report them separately: deterministic continuity, live-provider auth/routing, and crash recovery. A worker/process crash test deliberately causes failures and must not be included in the healthy release zero-error population. Resolve worker PIDs through private status/control rather than first matching `next-server` PID.

Receipt gates all named checks; a missing check is failure. Bind the exact tarball AND every external runtime file to their hashes. Run the same controller/worker/template bytes used in qualification on production. Do not replace a proxy/controller independently while sessions drain. Reject stale receipts after any runtime bundle change. Print subject counts with populations.

The persistent devbox layout remains on `/Volumes/devbox/9router`; regenerate short private socket paths after VM activation. Restore plists with concrete slot configs before starting the proxy. Do not delete a release directory while a worker references it. The harness stops/awaits isolated fixture sessions before guest baseline reset.

At the host qualification boundary, put `devbox stop <exact-name> --force` in `finally` for success, failure, timeout, and interrupt. Independently read stopped state without SSH, session listing, exec, or any activating API. A stop failure is an infrastructure failure, not qualification success. Do not send support requests or revoke credentials automatically. This plan does not add audit attribution tooling.

Watchdog continues probing stable `20128` and Tailscale/helper health. Enrolled mode targets the PROXY label only for a failed proxy, and the ACTIVE worker label for confirmed active-worker failure. It does not kickstart healthy workers merely because they are draining. Unknown control state alarms and does not grant drain-complete permission. Retain existing cooldown behavior. Do not restart external Tailscale for a candidate failure.

- [ ] **Step 4: Run local deterministic tests, then qualification only after authorization.**

```bash
python3 -m pytest tests/mac/test_9router_deploy.py tests/mac/test_9router_hotswap.py tests/mac/test_9router_vm_qualify.py tests/mac/test_9router_path_watchdog.py tests/mac/test_9router_devbox_restore.py -q
node tests/mac/9router_hotswap.check.cjs
node tests/unit/token-refresh-cross-process.check.mjs
```

Expected: all targeted tests pass. Existing unrelated failures must be recorded separately. Then build the release with `npm run cli:pack`. Select the actual produced tarball path from build output; do not hardcode a speculative version.

After current-turn authorization to activate the qualification environment, run `python3 scripts/mac/9router_vm_qualify.py run <actual-tarball-path>`. Expect a complete hash-bound passing receipt AND independently verified stopped devbox. No result is a pass if the continuity test or cleanup verification did not run.

- [ ] **Step 5: Commit and land through existing release gates.**

```bash
git add scripts/mac/9router_path_watchdog.py scripts/mac/9router_vm_qualify.py scripts/mac/9router_devbox_baseline.sh scripts/mac/9router_devbox_restore.sh tests/mac/test_9router_path_watchdog.py tests/mac/test_9router_vm_qualify.py tests/mac/test_9router_devbox_restore.py
git commit -m "test(mac): qualify hot swaps and preserve draining workers"
```

Before any push, run both Ruff legs on changed Python files, targeted Node checks, Sonar secrets on changed files, dependency risks only if manifests change, and `sonar_pr_issues.py --self-check` in this repository. Verify its nonzero file population. Follow the user gates; a missing scan is a stop for the push. Do not call a secrets scan a code-analysis pass. Open the PR ready for review and own it through the applicable live CI/review gates; do not infer 9router protection from the workflow repo's documented contexts.

### Task 6: Execute separately approved production enrollment and first live swap

**Files:** Runtime-generated configuration only after current-turn approval: `~/.9router/hotswap/`, `~/Library/LaunchAgents/com.lfenergy.9router-proxy.plist`, slot plists, and legacy startup ownership. No edits occur during plan creation.

**Interfaces:** `python3 scripts/mac/9router_hotswap.py enroll --ack-maintenance`, then existing `9router_deploy.py deploy <qualified-tarball>` and `rollback` after enrollment.

- [ ] **Step 1: Preflight without mutating production.**

Read current version, route topology, Caddy service ownership, free slot ports, private directory permissions, disk/memory headroom for both workers, enabled singleton settings, qualified receipt hashes, native SQLite availability, and persistence fingerprint. Inspect the real external ingress forwarding-header contract. Read `~/.9router/apply-*.sh` by explicit discovered paths and enumerate their actual effects; do not run them against both slots. Required live patches must be in the qualified candidate source or the exact qualification-bound runtime bundle. Refuse enrollment if production behavior depends on unknown/missing patch effects.

Ensure `JWT_SECRET`, `API_KEY_SECRET`, and machine identity salt remain stable and are loaded privately by both slots. Confirm no secret is printed. Confirm dashboard static assets are compatible with the candidate; retain old immutable Next assets needed by existing dashboard pages. Implement an allowlisted static-asset union behind the stable proxy, keyed by exact build asset paths, before claiming dashboard continuity. Reject path traversal and conflicting bytes at the same immutable URL. Test a baseline dashboard page requesting an old chunk after cutover. Do not silently exclude dashboard traffic from the user's no-downtime request.

- [ ] **Step 2: Obtain explicit current-turn approval for the one-time maintenance enrollment.**

State that replacing the existing listener on `20128` can interrupt existing connections once. Do not describe this bootstrap as zero-downtime. Do not execute enrollment under the earlier planning selection alone.

- [ ] **Step 3: Enroll and verify.**

Before the maintenance window, validate the protocol-1 baseline against an isolated SQLite backup, never the live database. During the approved maintenance window, positively quiesce and stop the legacy worker BEFORE attaching any managed worker to the live database: legacy workers lack the cross-process refresh and migration protections. Start A from the newly built qualified protocol-1 release with its concrete manifest and paths, then start the separate Caddy label on `20128`. Verify version, models, auth, complete streams, WebSockets, and client IP sanitization through existing ingress. On enrollment failure, stop only newly introduced jobs and restore the previous launchd configuration without restoring an old provider database. Preserve diagnostics and report whether legacy service is actually healthy. Rolling back after enrollment may only use another compatible protocol-1 release; never overlap the unprotected legacy package with a managed worker.

- [ ] **Step 4: Prove a real release swap.**

Hold an authenticated SSE request and a WebSocket session across a deployment to B. Confirm new requests return B's version while the old sessions remain on A and complete naturally. Confirm proxy PID does not change. Report live request/session counts and drain state. Exercise rollback with the same preservation checks. Never forcibly end the held real sessions to make a drain verdict pass; use isolated test sessions that naturally finish at controlled barriers.

- [ ] **Step 5: Close out with evidence.**

Record exact serving version, tarball/runtime hashes, proxy PID before/after, completed request count, error/truncation count, old slot state, and receipt path. State any retained draining worker and that another release is refused until a slot becomes free. Verify qualification devbox remains stopped without connecting. Reap only the implementing agent's worktree after landing; retain authored plan evidence.

## Acceptance criteria

- Candidate install, startup, or direct verification failure leaves public routing and old sessions unchanged.
- New HTTP requests, including over an existing client keepalive connection, use the newly selected release.
- Old SSE responses reach their terminal event and old WebSockets remain usable across deploy AND rollback.
- No healthy deployment reloads/restarts Caddy or kills a live session.
- A busy draining slot blocks a third release without killing it.
- A missing or unreadable work counter never grants permission to stop.
- Two processes cannot reuse a single-use OAuth token through any live dispatch covered by qualification.
- Shared usage/credential writes survive concurrent operation; unsupported persistence and changed schema refuse hot swap.
- Crash reconciliation never guesses a route from stale state alone.
- Existing API authentication, dashboard secrets, and client-IP trust boundaries remain intact.
- Qualification receipt covers exact tarball and runtime bundle, with nonzero named subjects.
- Qualification success, failure, and interruption all leave compute verifiably stopped or report an explicit cleanup failure.

## Plan self-review

Coverage maps to Tasks 1–6, including Task 4b: native switching, preservation, shared-state safety, transactional rollback/recovery, destructive dashboard-path refusal, gates/watchdog/qualification, and authorized enrollment. Review corrected three activation hazards: managed initialization must retain active-slot background work; enrollment must not overlap legacy and managed workers against the live database; dashboard updater endpoints must not bypass managed deployment. Two-slot saturation and schema refusal are explicit ceilings, not a universal promise that arbitrary future releases can swap safely. Task 3 must define complete durable refresh and compare-and-swap code before its GREEN run; the SQL and test snippets here are not that implementation.

Implementation confidence: moderate. The installed Caddy socket-switch primitive has direct experimental proof. Full packaged-worker continuity, token-flight durability, native-driver enforcement, and existing hotpatch compatibility remain acceptance tests, not established facts.

## Appendix A: Complete native switching check

This is proposed test code. It uses fake applications and a separate Caddy instance, not production. Run with `node tests/mac/9router_hotswap.check.cjs`; Caddy must be on PATH. Remove the `renameSync` route switch to prove RED.

```javascript
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn, execFileSync } = require('node:child_process');
const { once } = require('node:events');

(async () => {
  const root = fs.mkdtempSync('/tmp/9r-swap-');
  const servers = [];
  const sockets = new Set();
  const agent = new http.Agent({ keepAlive: true });
  let proxy;
  let endStream;
  const deadline = setTimeout(() => {
    for (const socket of sockets) socket.destroy();
    proxy?.kill('SIGTERM');
    console.error('FAIL: isolated routing check exceeded 20 seconds');
    process.exitCode = 1;
  }, 20000);
  const listen = async (server, address) => {
    servers.push(server);
    server.listen(address);
    await once(server, 'listening');
    return server.address();
  };
  const track = (server) => server.on('connection', (socket) => {
    sockets.add(socket);
    socket.once('close', () => sockets.delete(socket));
  });
  try {
    for (const name of ['a', 'b']) {
      const app = track(http.createServer((req, res) => {
        if (req.url !== '/stream') return res.end(name);
        res.writeHead(200, { 'Content-Type': 'text/event-stream' });
        res.write(`data: ${name}-start\n\n`);
        endStream = () => res.end(`data: ${name}-done\n\n`);
      }));
      app.on('upgrade', (req, socket) => {
        const accept = crypto.createHash('sha1')
          .update(req.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11')
          .digest('base64');
        socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n' +
          `Connection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
        socket.on('data', () => socket.write(Buffer.from([0x81, 1, name.charCodeAt(0)])));
      });
      const address = await listen(app, { host: '127.0.0.1', port: 0 });
      const bridge = track(net.createServer((incoming) => {
        const outgoing = net.connect(address.port, '127.0.0.1');
        sockets.add(outgoing);
        outgoing.once('close', () => sockets.delete(outgoing));
        incoming.on('error', () => outgoing.destroy());
        outgoing.on('error', () => incoming.destroy());
        incoming.once('close', () => outgoing.destroy());
        outgoing.once('close', () => incoming.destroy());
        incoming.pipe(outgoing);
        outgoing.pipe(incoming);
      }));
      await listen(bridge, path.join(root, `${name}.sock`));
    }
    const reservation = net.createServer();
    reservation.listen(0, '127.0.0.1');
    await once(reservation, 'listening');
    const port = reservation.address().port;
    await new Promise((resolve) => reservation.close(resolve));
    const active = path.join(root, 'active.sock');
    fs.symlinkSync(path.join(root, 'a.sock'), active);
    const config = path.join(root, 'Caddyfile');
    fs.writeFileSync(config, `{
 admin off
 auto_https off
}
http://:${port} {
 bind 127.0.0.1
 reverse_proxy unix/${active} {
  flush_interval -1
  transport http {
   keepalive off
   versions 1.1
  }
 }
}
`);
    execFileSync('caddy', ['validate', '--config', config, '--adapter', 'caddyfile'], { stdio: 'pipe' });
    proxy = spawn('caddy', ['run', '--config', config, '--adapter', 'caddyfile'], { stdio: 'ignore' });
    const originalPid = proxy.pid;
    const get = () => new Promise((resolve, reject) => {
      const req = http.get({ host: '127.0.0.1', port, path: '/', agent }, (res) => {
        let body = '';
        res.on('data', (chunk) => { body += chunk; });
        res.on('end', () => resolve(body));
        res.on('error', reject);
      });
      req.on('error', reject);
      req.setTimeout(2000, () => req.destroy(new Error('request timeout')));
    });
    const readyDeadline = Date.now() + 10000;
    while (true) {
      try { assert.equal(await get(), 'a'); break; }
      catch (error) {
        if (Date.now() > readyDeadline) throw error;
        await new Promise((resolve) => setTimeout(resolve, 30));
      }
    }
    let streamBody = '';
    const streamDone = new Promise((resolve, reject) => {
      const req = http.get({ host: '127.0.0.1', port, path: '/stream' }, (res) => {
        res.on('data', (chunk) => { streamBody += chunk; });
        res.on('end', resolve);
        res.on('error', reject);
      });
      req.on('error', reject);
      req.setTimeout(10000, () => req.destroy(new Error('stream timeout')));
    });
    streamDone.catch(() => {});
    const startedDeadline = Date.now() + 5000;
    while (!streamBody.includes('a-start')) {
      if (Date.now() > startedDeadline) throw new Error('stream did not start');
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    const ws = net.connect(port, '127.0.0.1');
    sockets.add(ws);
    ws.setTimeout(5000, () => ws.destroy(new Error('WebSocket test timeout')));
    await once(ws, 'connect');
    ws.write('GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n' +
      'Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n' +
      'Sec-WebSocket-Version: 13\r\n\r\n');
    const [headers] = await once(ws, 'data');
    assert.match(headers.toString(), /101 Switching Protocols/);
    fs.symlinkSync(path.join(root, 'b.sock'), path.join(root, 'next.sock'));
    fs.renameSync(path.join(root, 'next.sock'), active);
    for (let n = 0; n < 100; n++) assert.equal(await get(), 'b');
    const echoed = once(ws, 'data');
    ws.write(Buffer.from([0x81, 0x81, 1, 2, 3, 4, 121]));
    assert.deepEqual((await echoed)[0], Buffer.from([0x81, 1, 97]));
    endStream();
    await streamDone;
    assert.equal(streamBody, 'data: a-start\n\ndata: a-done\n\n');
    assert.equal(proxy.pid, originalPid);
    assert.equal(proxy.exitCode, null);
    console.log('PASS: 100 new requests on B; one old SSE and one old WebSocket on A; no proxy reload');
  } finally {
    clearTimeout(deadline);
    agent.destroy();
    proxy?.kill('SIGTERM');
    for (const socket of sockets) socket.destroy();
    for (const server of servers) await new Promise((resolve) => server.close(resolve));
    if (proxy && proxy.exitCode === null && proxy.signalCode === null) await once(proxy, 'exit');
    fs.rmSync(root, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
```

## Execution handoff

This document is a concrete, evidence-backed task plan, not a full prewritten patch. Appendix A is runnable and the interface/state contracts are fixed. Worker IPC accounting, OAuth generation persistence, control parsing, and journal recovery still require implementation code and their RED/GREEN tests before any production action. Do not present the planning experiment as qualification or the plan as built software.
