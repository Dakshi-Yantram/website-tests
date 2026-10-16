p = 'tests/test_08_opd_registration.py'
s = open(p, encoding='utf-8').read()
old = '    i = (SLOT_BASE + n) % 48\n    return f"{8 + i // 4:02d}:{(i % 4) * 15:02d}"'
new = """    import datetime as _dt
    now = _dt.datetime.now()
    i = max(32, (now.hour * 60 + now.minute) // 15 - 1 - (SLOT_BASE % 4) - n)     # 32 = 08:00; slot in the past, not future
    return f"{i // 4:02d}:{(i % 4) * 15:02d}\""""
if '_dt.datetime.now()' in s:
    print('already patched')
elif old in s:
    open(p, 'w', encoding='utf-8').write(s.replace(old, new, 1)); print('patched: True')
else:
    print('patched: False (lines not found)')
