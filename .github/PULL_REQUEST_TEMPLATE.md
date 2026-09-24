## What this changes and why

<!-- One paragraph. If it fixes a wrong value, say what the real value is and where it comes from. -->

## Checklist

- [ ] `tools/check.sh` passes (syntax, duplicate CSS selectors, duplicate ids, zh/en string tables aligned)
- [ ] Every new user-visible string is in **both** `STR['zh-TW']` and `STR['en']` (`static/js/i18n.js`), and in both `MSG` tables if it comes from `server.py`
- [ ] Anything the tool cannot read shows `—` with a reason; nothing is guessed or defaulted
- [ ] Qualifiers are kept: "estimated", "based on modinfo", "cannot be ruled out" and similar are not softened or dropped
- [ ] Tested on a real machine: <!-- model, DGX OS version -->
- [ ] No serial numbers, MAC addresses, IPs or Wi-Fi names in code, screenshots or this description
