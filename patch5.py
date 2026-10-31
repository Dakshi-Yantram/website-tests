p = 'utils/ui_helpers.py'
s = open(p, encoding='utf-8').read()
a = '    why = ""\n    try:\n        for n, (name, act) in enumerate(strategies):'
b = """    try:
        page.evaluate(\"\"\"() => { window.__toasts = []; if (window.__obs) window.__obs.disconnect();
          window.__obs = new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes) {
            if (n.nodeType !== 1) continue; const t = (n.innerText || '').trim();
            if (t && (n.matches('[role=alert],[class*=Alert],[class*=nackbar],[class*=oast],[class*=otistack]') ||
                      n.querySelector('[role=alert],[class*=Alert],[class*=nackbar],[class*=oast]'))) window.__toasts.push(t.slice(0, 120)); } });
          window.__obs.observe(document.body, {childList: true, subtree: true}); }\"\"\")
    except Exception:
        pass

    def toasts():
        try:
            return page.evaluate("() => window.__toasts || []")
        except Exception:
            return []

""" + a
if 'window.__toasts' in s:
    print('ui_helpers: already patched')
elif a in s and 'console errors={errs[-3:]}")' in s:
    s = s.replace(a, b, 1).replace('console errors={errs[-3:]}")', 'console errors={errs[-3:]}; TOASTS={toasts()}")', 1)
    open(p, 'w', encoding='utf-8').write(s); print('ui_helpers patched: True')
else:
    print('ui_helpers patched: False (lines not found)')

p = 'tests/test_09_patient_details.py'
s = open(p, encoding='utf-8').read()
a = [l for l in s.splitlines() if l.strip().startswith('vt = f"')]
if a:
    new = '        vt = page.evaluate("() => { const d = new Date(Date.now() - 15*60000); return d.getDate() === new Date().getDate() ? d.toTimeString().slice(0,5) : \'00:00\'; }")      # always in the past (the app rejects future times)'
    s = s.replace(a[0], new, 1)
    open(p, 'w', encoding='utf-8').write(s); print('test file patched: True')
else:
    print('test file: vt line not found (already patched?)')
