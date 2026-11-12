p = 'tests/test_09_patient_details.py'
s = open(p, encoding='utf-8').read()
a = '        times = dlg.locator("input[type=time]")\n'
if 'vt = f"' in s:
    print('already patched')
elif a in s and 'times.nth(i).fill("08:00")' in s:
    s = s.replace(a, '        vt = f"{random.randint(0, 23):02d}:{random.randint(0, 59):02d}"\n' + a, 1)
    s = s.replace('times.nth(i).fill("08:00")', 'times.nth(i).fill(vt)')
    s = s.replace('times 08:00"', 'times {vt}"')
    open(p, 'w', encoding='utf-8').write(s); print('patched: True')
else:
    print('patched: False (lines not found)')
