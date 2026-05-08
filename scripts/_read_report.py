import sys, pathlib
sys.stdout.reconfigure(encoding='utf-8')
parent = pathlib.Path('D:/Obsidian/SecondBrain/03-Areas')
for child in parent.iterdir():
    name = child.name
    if len(name) == 4:  # 4 Chinese chars = 量化系统
        for f in sorted(child.glob('*.md'), reverse=True)[:1]:
            text = f.read_text(encoding='utf-8')
            print(text)
