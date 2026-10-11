# GB10 stability controls

## Scope and evidence

This protects managed request admission, exposes memory pressure and records bounded local events. It does not guarantee CUDA allocations or prevent all kernel/driver hangs. Direct Ollama 11434 bypasses the proxy; Strata 8080 is protected only when its drain patch and STRATA_ADMISSION_URL setting are deployed.

`tools/calibrate_memory.py` samples /proc for 20 seconds without loading a model. The reserve is 1.25 times the maximum observed sum of AnonPages, Shmem, SUnreclaim, PageTables and KernelStack. This includes the current workload; it is a conservative measured baseline with an explicit policy margin, not a model peak prediction. PSI full avg10 >= 10% also refuses admission. No missing/invalid policy silently permits a request. Recalibrate deliberately after workload changes, not during an OOM incident.

## Emergency OOM daemon (requires root)

Review `tools/install_earlyoom.sh`, then run it with sudo. It installs the pinned Ubuntu noble arm64 earlyoom 1.7-2 package, verified against the APT index SHA-256. No repository update or unrelated package upgrade is performed. Configuration is backed up if present.

The daemon sends TERM at half the admission reserve and KILL at one quarter. These fractions are policy, not measured failure thresholds. Both swap thresholds are 100%, so it does not wait for swap exhaustion. Model engines are preferred; gnome-shell, Xorg, Xwayland, spark-center and spark-guard are avoided (not absolutely immune). It can still terminate other processes if memory cannot be recovered. Model recovery is manual; do not configure restart loops for large models.

Verify with `systemctl status earlyoom` and `journalctl -u earlyoom`. Installing requires your terminal's sudo authentication; never put the password into chat. Without this step the emergency daemon is NOT active, even if the UI and admission checks work. To roll back: `sudo systemctl disable --now earlyoom`; restore the saved /etc/default/earlyoom if applicable. No fstab changes are included.

## Drain and interrupted switches

Strata accepts authenticated local, no-Origin JSON POST /admin/drain with action=drain, returns an owner token, and counts the entire request including body processing and tokenization. Active requests continue; new generation/load/PDF operations return 503. Resume requires the same owner token. GET /admin/drain reports counts without exposing the token.

Spark Center checks idle status then atomically drains. If an in-flight request entered the gap, switching fails and resumes its own drain; it does not kill the request. A process lock serializes switch operations. Phase state is atomically written and fsynced. Restart displays interrupted rather than claiming success; explicit retry checks real services and pending markers, and resumes only the stored drain owner. No automatic model reload or package replay is added.

## Observability

Memory sample: available bytes, swap used, swap-in/out bytes per second, PSI some/full. Kernel journal events are keyed by journal cursor, grouped with a >5-second gap; a group is not proof of a separate incident. The counter reflects readable records observed this boot, not a hardware allocation counter. Initial history reads at most 1,000 matching lines. Persistent event history is capped at 5,000 records / seven days. No prompt or authorization headers are logged. An unavailable journal is shown as unavailable, not proof of zero errors.

## NAS

The existing Offline Builder downloader was inspected: one local flock serializes all groups/destinations, default bulk writes are limited to 150 MiB/s, and files are hashed after NAS readback. These are existing safeguards, not new claims of a safe sustained NAS speed. CIFS soft is the default; omission in fstab is not evidence of hard behavior. Existing NFS hard semantics are preserved. Do not change fstab blindly or copy at unlimited speed from SSD; do not remove source material before destination verification. No large NAS write test is part of this release.

## Local deployment and validation (2026-10-11)

The measured policy on this machine reserves 19,559,229,440 bytes (18.22 GiB); the observed 20-sample baseline peak was 15,647,383,552 bytes. This is not a promise that any particular PDF/context fits.

User service drop-ins named `stability.conf` set OOMScoreAdjust=100 for spark-center and spark-ollama-guard, and STRATA_ADMISSION_URL=http://127.0.0.1:11436/guard/admission for strata-server. Strata retains OOMScoreAdjust=200. These local files are outside the repository; reproduce them explicitly after migration, then daemon-reload the user manager. Restart only when jobs and switches are idle. The policy, event database and switch state are deliberately ignored private runtime data.

Real API validation: Strata returned HTTP 503/server_draining during drain and HTTP 200 after owner resume. A complete Strata → local qwen3.5:4b → Strata cycle succeeded; the Ollama leg took 8.85 s and restoration took 46.83 s (switch timestamps, one trial). Ollama answered 「切換測試成功」. No model download was performed.

If a drain request loses its response before the owner token is saved, do not invent a token or force model loading. Inspect GET /admin/drain and /status. After active_requests=0, busy=false and queued=0, a deliberate Strata service restart resets the gate. A failed/unknown request leaves admission closed. The persisted switch recovery button handles known owners; it does not override another owner.

No actual memory exhaustion, GPU driver failure or NAS outage was injected on the desktop. Isolated tests cover memory refusal, journal failure/empty windows, concurrent switch refusal, interrupted state, drain races and state-storage failure.

During the earlyoom package installation, the system manager logged `Failed to fork off sandboxing environment for executing generators: Protocol error`; each daemon-reload waited approximately 90 seconds. This is a separate observed system-manager fault, not evidence of a model OOM. Do not interrupt dpkg or start a second package transaction while installation is still running.

The user manager inherits oom_score_adj=100. Requesting 0 in an unprivileged user unit did not lower the actual process value. The deployed drop-ins therefore explicitly use 100, verified in /proc; model wrappers retain 200. Prefer/avoid daemon rules provide the additional selection policy.

Final installation check at 08:47:58: earlyoom active/running/enabled, dpkg install ok installed, installer exited. Actual startup log: TERM <= 7.61% available RAM, KILL <= 3.81%, swap thresholds 100%. The generator fault remains unresolved despite eventual installation success.
