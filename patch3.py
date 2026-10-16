p = 'tests/test_09_patient_details.py'
s = open(p, encoding='utf-8').read()
a = '            st.actual += f" | server={v_status} ({v_where})"\n'
b = a + """            try:
                page.screenshot(path=str(ART / "vitals_after_add.png"), full_page=True)
                st.actual += f" | BUTTONS={button_names(dlg)} | dialogs={page.locator('[role=dialog]').count()} | DLGTEXT={re.sub(chr(92)+'s+', ' ', dlg.inner_text())[:300]}"
            except Exception as e:
                st.actual += f" | (vitals debug failed: {str(e)[:60]})"
"""
if 'vitals_after_add.png' in s:
    print('already patched')
elif a in s:
    open(p, 'w', encoding='utf-8').write(s.replace(a, b, 1)); print('patched: True')
else:
    print('patched: False (line not found)')
