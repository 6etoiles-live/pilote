"""Calque fixe de la marque « 6 Étoiles » posé sur chaque clip (PNG transparent 1080x1920, rendu par Chrome).

Contenu : avatar (le 6 en dégradé) + crédit « twitch.tv/<streamer> » en minuscules en haut à gauche, dans la zone commune
Instagram/TikTok (x 120 à 960, y 285 à 1248) ; filet en dégradé magenta, violet, bleu ciel à la couture cam/jeu
(mise en page « empile ») ou au-dessus et au-dessous de la bande de jeu (« flou »).
Couleurs relevées sur rockstargames.com/VI le 11/11/2026 : #0F0C1A, #D263E9, #9B89F5, #99CCFF.
"""
import hashlib
import subprocess
from pathlib import Path

from .commun import RACINE, TRAVAIL

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
AVATAR = RACINE / "marque" / "kit" / "avatar.svg"
DEGRADE = "linear-gradient(90deg,#D263E9,#9B89F5 52%,#99CCFF)"
BANDE_FLOU_Y = 600  # haut de la bande de jeu 4:3 (1080 x 810) en mise en page « flou »


def _html(streamer, mise_en_page):
    if not AVATAR.exists():  # un clip sans le logo de la page ne part pas
        raise FileNotFoundError(f"avatar de la marque introuvable : {AVATAR} (lancer marque/logo_a.py)")
    avatar = AVATAR.read_text(encoding="utf-8")
    if mise_en_page == "empile":
        filets = f'<div style="position:absolute;left:0;right:0;top:757px;height:7px;background:{DEGRADE}"></div>'
    else:
        filets = (f'<div style="position:absolute;left:0;right:0;top:{BANDE_FLOU_Y - 6}px;height:6px;background:{DEGRADE}"></div>'
                  f'<div style="position:absolute;left:0;right:0;top:{BANDE_FLOU_Y + 810}px;height:6px;background:{DEGRADE}"></div>')
    # pseudos longs (Twitch : 25 signes au plus) : le crédit rapetisse pour rester sous x 940
    taille = 32 if len(streamer) <= 16 else max(22, round(32 * 16 / len(streamer)))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:transparent}} .av svg{{width:100%;height:100%;display:block}}</style></head>
<body><div style="position:relative;width:1080px;height:1920px">
{filets}
<div style="position:absolute;left:124px;top:300px;display:flex;align-items:center;gap:14px">
  <div class="av" style="width:86px;height:86px;border-radius:50%;overflow:hidden;box-shadow:0 0 0 4px #0F0C1A">{avatar}</div>
  <div style="padding:8px 22px 11px;border-radius:40px;background:rgba(15,12,26,.78);font:600 {taille}px Jost;color:#fff;white-space:nowrap;max-width:700px;overflow:hidden">
    twitch.tv/{streamer.lower()}</div>
</div></div></body></html>"""


def calque(streamer, mise_en_page):
    """Chemin du PNG du calque (mis en cache tant que l'avatar et le texte ne changent pas)."""
    html = _html(streamer, mise_en_page)
    cle = hashlib.sha1(html.encode()).hexdigest()[:10]
    png = TRAVAIL / f"calque-{streamer}-{mise_en_page}-{cle}.png"
    if png.exists():
        return png
    TRAVAIL.mkdir(parents=True, exist_ok=True)
    page = TRAVAIL / f"calque-{streamer}-{mise_en_page}.html"
    page.write_text(html, encoding="utf-8")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--default-background-color=00000000", "--window-size=1080,1920", f"--screenshot={png}",
                    page.as_uri()], capture_output=True, timeout=120)
    if not png.exists():
        raise RuntimeError("calque de marque non rendu (Chrome)")
    return png
