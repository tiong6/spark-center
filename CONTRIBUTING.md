# Contributing

Spark Center is one person's tool for one machine that turned out to be useful to others. Contributions are welcome, and the rules below exist because the tool's whole value is that you can trust what it shows.

## The one rule that matters

**Never show a number the system did not give you.** If a value cannot be read, show `—` and say why. Do not default, estimate silently, or fall back to a plausible value. When something *is* an estimate or a heuristic, label it as such in the UI ("estimated", "based on package name", "based on `modinfo`; runtime requests cannot be ruled out") and keep that label in both languages.

## Before you open a pull request

1. Run `tools/check.sh`. It checks Python and JavaScript syntax, duplicate CSS selectors, duplicate HTML ids, and that the Chinese and English string tables have the same keys and placeholders.
2. Restart the service and click through every tab: `systemctl --user restart spark-center`, then open http://127.0.0.1:11001.
3. Say which machine and DGX OS version you tested on. The maintainer only has an ASUS Ascent GX10.

## Layout

- `server.py` — the whole back end, Python standard library plus what DGX OS ships (`python3-apt`, `python3-aptdaemon`, `python3-gi`). No pip.
- `index.html` — HTML skeleton only.
- `static/css/app.css` — all styles.
- `static/js/*.js` — one file per tab, loaded in the order listed at the bottom of `index.html`. They share one global scope; the order matters because later files reference top-level constants from earlier ones at load time.
- `static/js/i18n.js` — the `STR` string table. Every user-visible string goes in **both** `zh-TW` and `en`; use `t('key', {vars})` and `{name}` placeholders, never string concatenation (word order differs between the languages).
- `server.py` `MSG` — the same for strings that originate on the server.

## Things not to do

- No build step, no npm, no CDN, no pip packages. Everything is served straight from the repo.
- Do not bind to anything other than `127.0.0.1`. The tool can install packages and delete models.
- Do not add anything that reboots the machine, and do not add a permanent root helper. Privileged actions go through aptdaemon/polkit or `pkexec`, one action at a time.
- Do not commit anything from `data/`, and do not put serial numbers, MAC addresses, IPs or Wi-Fi names in code, screenshots or commit messages.

## Commit messages

English subject line. The body may be in Chinese or English; say **why** it was wrong, not only what changed.

## Language

Issues and pull requests in English or Traditional Chinese are both fine.
