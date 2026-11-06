"""Package only User assets; no private Pi data or Developer assets."""
from pathlib import Path
import shutil
root = Path(__file__).resolve().parents[2]
out = Path(__file__).resolve().parent / 'public'
out.mkdir(exist_ok=True)
for name in ('user.html', 'user.js', 'user.css', 'favicon.png'):
    shutil.copyfile(root / 'web' / name, out / name)
shutil.copyfile(root / 'web/favicon.png', out / 'glow-favicon.png')
p = out / 'user.html'
p.write_text(p.read_text().replace('href="/">Switch interface', 'href="http://glow.ultraviolet.nirtech.org/dev">Developer interface'))
