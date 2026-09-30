"""npm 安全更新交易的隔離測試：不碰真 npm／systemctl／OpenClaw，全部用假指令。
跑法：python3 tools/test_npm_safe.py"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SPARK_CENTER_NO_SWEEP", "1")
import server  # noqa: E402

REAL_UNITS_FOR = server._npm_units_for   # setUp 會把它換成假的；要測真的就用這個

UNIT = "openclaw-gateway.service"
SCHEMA_NEW = ("openclaw: cannot open your existing data: it was written by a newer schema (19); this build supports 18.\n"
              "Main process exited, code=exited, status=78/CONFIG")
SCHEMA_PENDING = "openclaw: database schema version 18 is older than required 19; run `openclaw doctor --fix`\nstatus=78"
MODULE = "Error: Cannot find module '/usr/lib/node_modules/openclaw/dist/x.js'\ncode: 'ERR_MODULE_NOT_FOUND'"


class Fake:
    """一個交易裡所有外部指令的假替身。用旗標決定每一步的成敗。"""

    def __init__(self, tmp):
        self.tmp = tmp
        self.backup_ok = True
        self.install_ok = True
        self.repair_ok = True
        self.unit_state = "active"   # 啟動後 is-active 回什麼
        self.journal = ""
        self.calls = []

    def run(self, cmd, **kw):        # subprocess.run
        self.calls.append(list(cmd))
        c = " ".join(cmd)
        class R: pass
        r = R(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        if "is-active" in c:
            r.stdout = self.unit_state + "\n"; r.returncode = 0 if self.unit_state == "active" else 3
        elif "backup create" in c:
            if not self.backup_ok:
                r.returncode = 1; r.stderr = "backup: disk full"
            else:
                p = os.path.join(cmd[-1], "openclaw-backup-test.tar.gz"); Path(p).write_bytes(b"x" * 2048)
                r.stdout = json.dumps({"archivePath": p})
        elif "doctor --fix" in c:
            r.returncode = 0 if self.repair_ok else 2
            r.stdout = "◇ schema upgraded 18 → 19" if self.repair_ok else "└ error: migration failed"
        return r

    def _run(self, cmd, timeout=20):   # server._run（回字串）
        self.calls.append(list(cmd))
        c = " ".join(cmd)
        if "journalctl" in c:
            return self.journal
        if "systemctl --user show" in c:
            return "NRestarts=0\n"
        return ""

    def run_subprocess(self, job, cmd, packages):   # Job._run_subprocess（npm install）
        self.calls.append(list(cmd))
        job._log("$ " + " ".join(cmd))
        with job.lock:
            job.state["status"] = "done" if self.install_ok else "error"
            job.state["exit"] = "rc=0" if self.install_ok else "rc=1"
            if not self.install_ok:
                job.state["error"] = "npm ERR! ETARGET"
            job.state["finished"] = "t"


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.f = Fake(self.tmp)
        self.job = server.Job()
        self.notes = []
        self.ps = [
            patch.object(server.subprocess, "run", self.f.run),
            patch.object(server, "_run", self.f._run),
            patch.object(server.Job, "_run_subprocess", lambda job, cmd, pk: self.f.run_subprocess(job, cmd, pk)),
            patch.object(server, "_npm_known_bin", lambda name: "/fake/bin/openclaw" if name == "openclaw" else None),
            patch.object(server, "_npm_units_for", lambda names: [UNIT] if "openclaw" in names else []),
            patch.object(server, "_session_env", lambda: {"DISPLAY": ":1"}),
            patch.object(server, "_notify", lambda t, b: self.notes.append((t, b))),
            patch.object(server, "NPM_BACKUP_DIR", self.tmp),
            patch.object(server.time, "sleep", lambda s: None),
        ]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()

    def order(self):
        return [s["step"] for s in self.job.state.get("steps", [])]

    def npm_cmd(self, ver):
        return [server.NPM_BIN, "install", "-g", "--engine-strict", f"openclaw@{ver}"]

    def test_success_full_sequence(self):
        self.job.state.update(kind="npm", status="running", packages=["openclaw"])
        self.job._npm_transaction(["openclaw"], self.npm_cmd("2026.9.7"), backup=True, repair=True)
        st = self.job.state
        self.assertEqual(st["status"], "done", st)
        self.assertEqual(self.order(), ["stop", "backup", "install", "repair", "start"])
        self.assertTrue(st["backup"]["path"].endswith(".tar.gz")); self.assertEqual(st["backup"]["bytes"], 2048)
        self.assertTrue(st["finished"]); self.assertEqual(self.notes, [])
        # 停在裝之前、啟動在修復之後
        flat = [" ".join(c) for c in self.f.calls]
        self.assertLess(flat.index(f"systemctl --user stop {UNIT}"), next(i for i, c in enumerate(flat) if "install -g" in c))
        self.assertLess(next(i for i, c in enumerate(flat) if "doctor --fix" in c), flat.index(f"systemctl --user start {UNIT}"))

    def test_backup_failure_aborts_before_install(self):
        self.f.backup_ok = False
        self.job.state.update(kind="npm", status="running", packages=["openclaw"])
        self.job._npm_transaction(["openclaw"], self.npm_cmd("2026.9.7"), backup=True, repair=True)
        st = self.job.state
        self.assertEqual(st["status"], "error")
        self.assertNotIn("install", self.order())          # 沒有動到程式
        self.assertIn("start", self.order())               # 服務有拉回來
        self.assertIn("openclaw", st["error"])

    def test_install_failure_restarts_old_and_stays_error(self):
        self.f.install_ok = False
        self.job.state.update(kind="npm", status="running", packages=["openclaw"])
        self.job._npm_transaction(["openclaw"], self.npm_cmd("2026.9.7"), backup=False, repair=True)
        st = self.job.state
        self.assertEqual(st["status"], "error"); self.assertIn("ETARGET", st["error"])
        self.assertEqual(self.order(), ["stop", "install", "start"])   # 沒跑 repair

    def test_rollback_schema_newer_gives_forward_button(self):
        self.f.unit_state = "failed"; self.f.journal = SCHEMA_NEW
        self.job.state.update(kind="rollback_npm", status="running", packages=["j", "openclaw"], forward={"openclaw": "2026.9.7"})
        self.job._npm_transaction(["openclaw"], [server.NPM_BIN, "install", "-g", "openclaw@2026.9.6"], backup=True, repair=False)
        st = self.job.state
        self.assertEqual(st["status"], "error")
        self.assertEqual(st["reason"], "npm_reason_schema_new"); self.assertEqual(st["reason_name"], "openclaw")
        ids = [a["id"] for a in st["actions"]]
        self.assertEqual(ids[0], "forward"); self.assertEqual(st["actions"][0]["version"], "2026.9.7")
        self.assertNotIn("repair", ids)                    # 舊版的修復指令救不了新資料，不給這顆
        self.assertIn("restart", ids); self.assertIn("backup_info", ids)
        self.assertNotIn("repair", self.order())           # 降回不跑 repair
        self.assertEqual(len(self.notes), 1)               # 桌面通知
        # 字串表兩種語言都有這個原因
        for lang in ("zh", "en"):
            self.assertTrue(server.msg("npm_unit_failed_summary", lang, units=UNIT))

    def test_update_schema_pending_gives_repair_button(self):
        self.f.unit_state = "failed"; self.f.journal = SCHEMA_PENDING
        self.job.state.update(kind="npm", status="running", packages=["openclaw"])
        self.job._npm_transaction(["openclaw"], self.npm_cmd("2026.9.7"), backup=False, repair=True)
        st = self.job.state
        self.assertEqual(st["reason"], "npm_reason_schema")
        self.assertEqual([a["id"] for a in st["actions"]][:1], ["repair"])

    def test_repair_job_kind_completes(self):
        # 「讓它自己修」是獨立工作，成功時必須真的變 done（曾經永遠停在 running）
        self.job.state.update(kind="npm_repair", status="running", packages=["openclaw"])
        self.job._run("npm_repair", ["openclaw"])
        self.assertEqual(self.job.state["status"], "done")
        self.assertEqual(self.order(), ["stop", "repair", "start"])

    def test_forward_job_kind(self):
        self.job.state.update(kind="npm_forward", status="running", packages=["openclaw", "2026.9.7"])
        self.job._run("npm_forward", ["openclaw", "2026.9.7"])
        self.assertEqual(self.job.state["status"], "done")
        self.assertEqual(self.order(), ["stop", "install", "repair", "start"])   # 不再備份（剛備過）
        self.assertTrue(any("openclaw@2026.9.7" in " ".join(c) for c in self.f.calls))

    def test_unknown_package_no_backup_no_repair(self):
        with patch.object(server, "_npm_units_for", lambda names: ["foo.service"]):
            self.job.state.update(kind="npm", status="running", packages=["foo"])
            self.job._npm_transaction(["foo"], [server.NPM_BIN, "install", "-g", "--engine-strict", "foo@2"], backup=True, repair=True)
        self.assertEqual(self.job.state["status"], "done")
        self.assertEqual(self.order(), ["stop", "install", "start"])

    def test_older_binary_refuses_new_config_is_forward_only(self):
        # 真機 2026-09-30：降回 9.6 後 gateway 說 config 是 9.7 寫的、拒絕啟動。這不是 schema 字樣，但救法一樣：回新版
        journal = ("Refusing to start the gateway service because this OpenClaw binary (2026.9.6) is older than the config "
                   "last written by OpenClaw 2026.9.7.\nMain process exited, code=exited, status=78/CONFIG")
        self.f.unit_state = "failed"; self.f.journal = journal
        self.job.state.update(kind="rollback_npm", status="running", packages=["j", "openclaw"], forward={"openclaw": "2026.9.7"})
        self.job._npm_transaction(["openclaw"], [server.NPM_BIN, "install", "-g", "openclaw@2026.9.6"], backup=False, repair=False)
        st = self.job.state
        self.assertEqual(st["reason"], "npm_reason_schema_new")
        ids = [a["id"] for a in st["actions"]]
        self.assertEqual(ids[0], "forward"); self.assertNotIn("repair", ids)
        # failed 要在第一輪就判定，不是等滿 20 輪
        self.assertLessEqual(sum(1 for c in self.f.calls if "is-active" in " ".join(c)), 2)

    def test_units_found_even_when_service_is_failed(self):
        # 服務 failed 時沒有程序在跑；靠認識的程式的清單和 ExecStart 掃描找到它
        fake_status = {"prefix": "/usr", "packages": [{"name": "openclaw", "running": []}, {"name": "foo", "running": []}]}
        def run(cmd, timeout=20):
            c = " ".join(cmd)
            if "list-units" in c:
                return "openclaw-gateway.service loaded failed failed\nfoo-daemon.service loaded inactive dead\nother.service loaded active running\n"
            if "show foo-daemon.service" in c:
                return "{ path=/usr/bin/node ; argv[]=/usr/bin/node /usr/lib/node_modules/foo/server.js }"
            if "show other.service" in c:
                return "{ path=/usr/bin/other ; argv[]=/usr/bin/other }"
            return ""
        with patch.object(server, "npm_status", lambda force=False: fake_status), patch.object(server, "_run", run):
            self.assertEqual(REAL_UNITS_FOR(["openclaw"]), ["openclaw-gateway.service"])
            self.assertEqual(REAL_UNITS_FOR(["foo"]), ["foo-daemon.service"])
            self.assertEqual(REAL_UNITS_FOR(["bar"]), [])

    def test_halfloaded_explanation(self):
        self.assertEqual(server._npm_explain(MODULE, "openclaw")[0], "npm_reason_halfloaded")
        self.assertEqual(server._npm_explain("EADDRINUSE :::18789", "openclaw")[0], "npm_reason_port")
        self.assertEqual(server._npm_explain("something else", "foo"), ("npm_reason_unknown", {}))

    def test_backup_keep_limit(self):
        d = os.path.join(self.tmp, "openclaw"); os.makedirs(d)
        for i in range(3):
            p = Path(d, f"old{i}.tar.gz"); p.write_bytes(b"o"); os.utime(p, (1000 + i, 1000 + i))
        self.job.state.update(kind="npm", status="running")
        self.assertTrue(self.job._npm_backup("openclaw"))
        self.assertEqual(len(server.npm_backups("openclaw")), server.NPM_BACKUP_KEEP)

    def test_plan_shape(self):
        fake_status = {"prefix": "/usr", "packages": [{"name": "openclaw", "current": "2026.9.6", "latest": "2026.9.7",
                       "running": [{"pid": 1, "unit": UNIT}, {"pid": 2}]}]}
        with patch.object(server, "npm_status", lambda force=False: fake_status):
            pl = server.npm_plan(["openclaw", "nope"], rollback=True)
        a, b = pl["items"]
        self.assertEqual((a["units"], a["loose_pids"], a["known"], a["can_backup"], a["migrates_data"]), ([UNIT], [2], True, True, True))
        self.assertEqual((b["known"], b["units"], b["current"]), (False, [], None))
        self.assertTrue(pl["rollback"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
