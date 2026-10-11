# Spark Center code boundaries

- Keep new hardware/network queries in `backend/hardware.py` and disk inventory in `backend/disk.py`.
- `backend/system.py` owns shared process/HTTP helpers and project paths; `backend/messages.py` owns `MSG`.
- Backend modules must not import `server.py`. Keep dependencies one-way; do not inject server globals.
- Cache state and locks belong to their feature module. Use its functions to invalidate or inspect state.
- `server.py` retains HTTP handling, package transactions, alert/control orchestration and existing model API integration. Do not add unrelated feature logic there.
- Model inventory/switching/guard remain in their existing `tools/` modules.
- Split only when responsibilities warrant it; do not create speculative abstractions or rewrite unrelated code.
- Run `tools/check.sh`. Before restarting the user service, verify `/api/job` is idle or finished and `/api/llm/switch` is not running.
- After deployment verify affected live APIs and every UI tab. Keep refactors separate from behavior changes.
- Keep private data out of commits; see `CLAUDE.md` and `HANDOFF.md` for deployment details.
