#!/usr/bin/env python3
"""前端 lint：抓單檔時代最容易出的三種錯。
1. CSS 同一個選擇器在頂層被定義兩次（例：硬體 .hero 撞上儀表 .gauge .hero 的那類問題，先從「同名重定義」抓起）。
2. index.html 靜態標記裡重複的 id。
3. 中英字串表 key 不對齊或佔位符不一致。
只回報事實，不自動改。"""
import json, re, subprocess, sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
bad = 0

# 1. CSS：把 @media 區塊剝掉後，逐條規則取選擇器
css = open(os.path.join(ROOT, 'static/css/app.css'), encoding='utf-8').read()
css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
top = re.sub(r'@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}', '', css)   # 去掉 @media（內含一層規則）
seen = {}
for m in re.finditer(r'([^{}]+)\{[^{}]*\}', top):
    for sel in m.group(1).split(','):
        sel = ' '.join(sel.split())
        if not sel:
            continue
        seen.setdefault(sel, 0); seen[sel] += 1
dups = {k: v for k, v in seen.items() if v > 1}
if dups:
    # 只警告不擋：既有的「後面再補一條」寫法有 8 處，合併會動到層疊順序；新加的請避免，舊的改到時順手併掉
    print(f'CSS 警告：選擇器重複定義 {len(dups)} 個（不擋，改到時併掉）：'); [print(f'  {k}  ×{v}') for k, v in sorted(dups.items())]
else:
    print(f'CSS OK（{len(seen)} 個頂層選擇器，無重複）')

# 2. HTML 重複 id
html = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
ids = re.findall(r'\sid="([^"]+)"', html)
d = {i for i in ids if ids.count(i) > 1}
if d:
    bad += 1; print('HTML 重複 id：', sorted(d))
else:
    print(f'HTML OK（{len(ids)} 個 id，無重複）')

# 3. 字串表
js = open(os.path.join(ROOT, 'static/js/i18n.js'), encoding='utf-8').read()
m = re.search(r'const STR = (\{.*?\n\});\n', js, re.S)
STR = json.loads(subprocess.check_output(['node', '-e', 'const STR=' + m.group(1) + ';console.log(JSON.stringify(STR))']))
py = open(os.path.join(ROOT, 'server.py'), encoding='utf-8').read()
MSG = eval(re.search(r'^MSG = (\{.*?\n\})\n', py, re.S | re.M).group(1))
for name, tbl in (('STR', STR), ('MSG', MSG)):
    zh, en = tbl['zh-TW'], tbl['en']
    miss = [k for k in zh if k not in en] + [k for k in en if k not in zh]
    ph = [k for k in zh if k in en and set(re.findall(r'\{\w+\}', zh[k])) != set(re.findall(r'\{\w+\}', en[k]))]
    zh_in_en = [k for k in en if re.search('[一-鿿]', en[k])]
    if miss or ph or zh_in_en:
        bad += 1; print(f'{name} 字串表問題：缺 key {miss[:5]} 佔位符不一致 {ph[:5]} 英文表含中文 {zh_in_en[:5]}')
    else:
        print(f'{name} OK（{len(zh)} 個 key 中英對齊）')
sys.exit(1 if bad else 0)
