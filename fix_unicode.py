import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

path = r"D:\Geotab_EV_Parameters\charger_sizing_test\exact_northgate_charger_sizing_milp.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

replacements = [
    ("→", "->"),
    ("←", "<-"),
    ("≤", "<="),
    ("≥", ">="),
    ("²", "^2"),
    ("λ", "lambda_"),
    ("∑", "sum"),
    ("·", "*"),
    ("—", "--"),
    ("–", "-"),
    ("‘", "'"),
    ("’", "'"),
    ("“", '"'),
    ("”", '"'),
    ("×", "x"),
    ("∞", "inf"),
    ("≈", "~="),
    ("≠", "!="),
    ("►", ">"),
    ("•", "-"),
    ("Δ", "Delta"),
    ("η", "eta"),
    ("�", "?"),
]

count = 0
for char, rep in replacements:
    if char in content:
        n = content.count(char)
        content = content.replace(char, rep)
        count += n
        print(f"  Replaced {repr(char)} ({n}x) -> {repr(rep)}")

remaining = set(c for c in content if ord(c) > 127)
if remaining:
    print(f"Remaining non-ASCII: {[repr(c) + ' U+' + format(ord(c),'04X') for c in sorted(remaining)[:30]]}")
else:
    print("No remaining non-ASCII characters.")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Done. {count} total replacements in {path}")
