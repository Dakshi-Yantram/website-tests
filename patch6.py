p = 'tests/test_09_patient_details.py'
s = open(p, encoding='utf-8').read()
old = '        fill_checked(dlg, r"^Medicine Name", "AUTOTEST Paracetamol 500")\n'
new = '        mname = type_and_pick(page, dlg, r"^Medicine Name", texts=("par", "ami", "cet", "ibu", "pan", "azi", "amo", "met"))      # a search dropdown: must pick a listed medicine\n'
if 'mname = type_and_pick' in s:
    print('already patched')
elif old in s:
    s = s.replace(old, new, 1).replace('log = [f"type={mtype}", ', 'log = [f"medicine={mname}", f"type={mtype}", ', 1)
    open(p, 'w', encoding='utf-8').write(s); print('patched: True')
else:
    print('patched: False (line not found)')
