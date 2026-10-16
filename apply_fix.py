p = 'tests/test_09_patient_details.py'
s = open(p, encoding='utf-8').read()
n = 0
a = 'click_and_capture(page, add_btn, timeout=4000)'
if a in s:
    s = s.replace(a, 'click_and_capture(page, add_btn, timeout=15000)'); n += 1
a = 're.compile(r"^\\s*add\\b", re.I)'
if a in s:
    s = s.replace(a, 're.compile(r"^\\s*add\\s*$", re.I)'); n += 1
open(p, 'w', encoding='utf-8').write(s)
print('changes applied:', n, '(expect 2 the first time, 0 if already patched)')
print('vitals timeout 15000:', 'add_btn, timeout=15000)' in s)
print('exact ADD:', 'add\\s*$", re.I)' in s)
