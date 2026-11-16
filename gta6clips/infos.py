"""Format « Infos » de 6 Étoiles : centraliser les infos du jeu et des streamers (décision du propriétaire le 11/11/2026).

Cartes 1080x1350 (fil Instagram, 4:5) et 1080x1920 (story, TikTok photo), rendues par Chrome dans la charte A :
- compte à rebours jusqu'à la sortie (fait vérifié : jeudi 19/11/2026, PS5 et Xbox Series X|S, site officiel) ;
- info du jeu : titre + texte + SOURCE obligatoire (site officiel, Newswire, annonce publique d'un streamer) ;
- en live : les streamers FR en live sur le jeu (API Twitch si l'appli existe, sinon liste blanche lue par yt-dlp).
Jamais de fuite, de rumeur présentée comme un fait, ni de spoiler : Rockstar fait retirer les fuites.
"""
import datetime as dt
import html
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .commun import police, RACINE, config, log, nettoyer_titre, streamers

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
KIT = RACINE / "marque" / "kit"
SORTIE_JEU = dt.date(2026, 11, 19)
NUIT, LAVANDE, CREME, MAGENTA = "#0F0C1A", "#CCC2F5", "#FFF9CB", "#D263E9"
# Directions choisies par le propriétaire le 11/11/2026 (refus du « look IA » : pas de halo flou, pas d'étiquette en capitales
# espacées, pas de légende en petit dans un coin) : « lavande, grand chiffre » pour le compte à rebours,
# « nuit à plat » pour les infos du jeu et les streamers en live. Texte calé à gauche, ancré en bas.
# Story 1920 : rien au-dessus de y 250 ni sous y 1500 (interface Instagram et TikTok).


def _grain(alpha):
    return ("url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='320' height='320'%3E%3Cfilter id='n'%3E"
            "%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E"
            f"%3CfeColorMatrix values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 {alpha} 0'/%3E%3C/filter%3E"
            "%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E\")")


MOIS = "janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre"


def _insecable(t, titre=False):
    """Pas de « 6 » ni de « novembre » seul en début de ligne. Dans un titre (grandes lettres), seuls « GTA 6 » et
    « 19 novembre » sont soudés, pour qu'aucun bloc ne dépasse la largeur de la carte ; dans le texte, aussi
    « novembre 2026 » et « Xbox Series X|S »."""
    t = t.replace("GTA 6", "GTA&nbsp;6")
    t = re.sub(rf"\b(\d{{1,2}}) ({MOIS})\b", r"\1&nbsp;\2", t)
    if not titre:
        t = t.replace("Xbox Series X|S", "Xbox&nbsp;Series&nbsp;X|S")
        t = re.sub(rf"\b({MOIS}) (\d{{4}})", r"\1&nbsp;\2", t)
    return t


_JOST = None


def _encre(texte, taille, espacement_em=-0.06):
    """Bord gauche et largeur de l'encre d'un nombre en Jost Black (fontTools), pour placer le grand chiffre."""
    global _JOST
    from fontTools.pens.boundsPen import BoundsPen
    if _JOST is None:
        from fontTools.ttLib import TTFont
        from fontTools.varLib.instancer import instantiateVariableFont
        _JOST = instantiateVariableFont(TTFont(str(police('Jost[wght].ttf'))), {"wght": 900})
    cmap, gs = _JOST.getBestCmap(), _JOST.getGlyphSet()
    k = taille / _JOST["head"].unitsPerEm
    x, gauche, droite = 0.0, None, None
    for c in texte:
        g = gs[cmap[ord(c)]]
        bp = BoundsPen(gs)
        g.draw(bp)
        if bp.bounds:
            gauche = x + bp.bounds[0] * k if gauche is None else gauche
            droite = x + bp.bounds[2] * k
        x += g.width * k + espacement_em * taille
    return gauche, droite - gauche


def _marges(h):
    """(haut du logo, bas du bloc de texte) selon le format."""
    return (84, 150) if h == 1350 else (260, 420)


def _page(contenu, h, fond=NUIT, couleur="#fff", logo="logo-couleur.svg", grain=0.0):
    haut, _ = _marges(h)
    trame = f'<div style="position:absolute;inset:0;background-image:{_grain(grain)};mix-blend-mode:multiply"></div>' if grain else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0}} .logo svg{{width:100%;height:auto;display:block}}
.carte{{position:relative;width:1080px;height:{h}px;overflow:hidden;background:{fond};color:{couleur};font-family:Jost}}
.titre{{font:900 120px/.92 Montserrat;letter-spacing:-.045em}}
.texte{{font:400 38px/1.35 Jost}}
</style></head><body><div class="carte">
<div class="logo" style="position:absolute;top:{haut}px;left:84px;width:210px">{(KIT / logo).read_text(encoding="utf-8")}</div>
{contenu}{trame}</div></body></html>"""


def _rendre(contenu_html, sortie, h):
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.unlink(missing_ok=True)  # une ancienne carte du même nom ne passe jamais pour la nouvelle
    page = sortie.with_suffix(".html")
    page.write_text(contenu_html, encoding="utf-8")
    try:
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                        f"--window-size=1080,{h}", f"--screenshot={sortie}", page.as_uri()], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"carte non rendue : {sortie.name} ({e})") from e
    finally:
        page.unlink(missing_ok=True)
    if not sortie.exists():
        raise RuntimeError(f"carte non rendue : {sortie.name}")
    return sortie


def compte_a_rebours(jour=None, h=1350):
    jour = jour or dt.date.today()
    n = (SORTIE_JEU - jour).days
    if n < 1:
        raise ValueError("le jeu est sorti : plus de compte à rebours")
    mot = "jour" if n == 1 else "jours"
    plus_que = "Plus qu'un jour" if n == 1 else f"Plus que {n} jours"
    _, bas = _marges(h)
    # le chiffre commence à x 600 (le texte s'arrête avant x 480) et au plus 25 % de son encre sort à droite
    debut_encre, place = 600, 1080 - 600
    base = 1250 if h == 1350 else 1400
    _, largeur_em = _encre(str(n), 1000)
    taille = min(base, place / 0.75 / (largeur_em / 1000))
    gauche, _ = _encre(str(n), taille)
    haut_chiffre = 110 if h == 1350 else 330
    contenu = f"""<div style="position:absolute;left:{debut_encre - gauche:.0f}px;top:{haut_chiffre}px;font:900 {taille:.0f}px/.8 Jost;letter-spacing:-.06em;color:{NUIT};white-space:nowrap">{n}</div>
<div style="position:absolute;left:84px;bottom:{bas}px;width:480px">
  <div class="titre" style="font-size:118px">{mot}<br>avant<br>GTA&nbsp;6.</div>
  <div class="texte" style="margin-top:44px;font-size:34px;max-width:420px">{_insecable("Sortie le jeudi 19 novembre 2026 sur PS5 et Xbox Series X|S, d'après le site de Rockstar Games.")}</div>
</div>"""
    legende = (f"{plus_que} avant GTA 6 ⭐ Sortie le jeudi 19 novembre 2026 sur PS5 et Xbox Series X|S.\n\n"
               "#gta6 #gta6fr #leonida #vicecity #gaming")
    return _page(contenu, h, fond=LAVANDE, couleur=NUIT, logo="logo-couleur-sur-clair.svg", grain=0.14), legende, f"j-{n}"


def info(titre, texte, source, etiquette=None, h=1350, fort=None):
    """Info du jeu : titre (fort = morceau du titre en lavande), texte, et la source dans le texte. Sans source, rien."""
    if not (source or "").strip():
        raise ValueError("une info sans source ne part pas")
    if len(titre) > 90 or len(texte) > 320:
        raise ValueError("titre (90 signes) ou texte (320 signes) trop long pour une carte")
    _, bas = _marges(h)
    taille_titre = 120 if len(titre) <= 40 else (100 if len(titre) <= 60 else 84)
    if max(len(m) for m in titre.split()) > 12:  # un mot long déborde de la marge en 120 px
        taille_titre = min(taille_titre, 100)
    t = _insecable(html.escape(titre), titre=True)
    f = _insecable(html.escape(fort), titre=True) if fort else ""
    if f and f in t:
        t = t.replace(f, f'<span style="color:{LAVANDE}">{f}</span>', 1)
    contenu = f"""<div style="position:absolute;left:84px;right:84px;bottom:{bas}px">
  <div class="titre" style="font-size:{taille_titre}px">{t}</div>
  <div class="texte" style="margin-top:50px;color:{LAVANDE}">{_insecable(html.escape(texte))}</div>
  <div class="texte" style="margin-top:22px;color:{LAVANDE};opacity:.7">Source : {html.escape(source)}.</div>
</div>"""
    return _page(contenu, h)


def _live_ytdlp(login):
    try:
        r = subprocess.run(["yt-dlp", "-J", "--skip-download", "--socket-timeout", "20", f"https://www.twitch.tv/{login}"],
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:  # une chaîne lente ne bloque pas les autres
        log(f"infos : {login} trop lent, ignoré")
        return None
    except OSError as e:
        log(f"infos : yt-dlp introuvable ({e})")
        return None
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except ValueError:
        log(f"infos : réponse illisible pour {login}")
        return None
    return {"login": login, "titre": d.get("description") or d.get("title") or "",
            "spectateurs": d.get("concurrent_view_count") or 0}


GTA6 = re.compile(r"\bgta\s?(6|vi)\b", re.I)


def _refuses():
    return {l for l, s in streamers().items() if s.get("noir") or s.get("accord") == "non"}


def en_live(filtre_jeu=True):
    """Streamers FR en live : lives.json du radar (API Twitch) s'il est frais, sinon liste blanche via yt-dlp.
    Jamais un streamer qui a demandé le retrait ; « sur GTA 6 » seulement si c'est sûr."""
    f = RACINE / "donnees" / "lives.json"
    helix = f.exists() and (dt.datetime.now().timestamp() - f.stat().st_mtime) < 1800
    jeu_releve = ""
    if helix:
        d = json.loads(f.read_text(encoding="utf-8"))
        jeu_releve = d.get("jeu", "") if isinstance(d, dict) else ""  # ancien format (liste) : jeu inconnu
        lives = d.get("lives", []) if isinstance(d, dict) else d
    else:
        with ThreadPoolExecutor(max_workers=8) as ex:
            lives = [x for x in ex.map(_live_ytdlp, list(streamers())) if x]
    refuses = _refuses()
    lives = [x for x in lives if x["login"].lower() not in refuses]
    # avec l'API, la catégorie fait foi seulement si le radar tourne sur GTA VI ; sinon seul le titre dit le jeu
    categorie_sure = helix and re.search(r"\bVI\b", jeu_releve)
    if filtre_jeu and not categorie_sure:
        lives = [x for x in lives if GTA6.search(x.get("titre", ""))]
    return sorted(lives, key=lambda x: x.get("spectateurs") or 0, reverse=True)


def titre_propre(t):
    """Titre de live lisible sur une carte : sans balises, commandes (!discord), liens ni tirets ou flèches."""
    return html.escape(nettoyer_titre(t)[:70])


def carte_live(lives, h=1350, titre="En live sur GTA 6"):
    avec_chiffres = any(x.get("spectateurs") for x in lives)
    _, bas = _marges(h)
    nb = 5 if h == 1350 else 6

    def ligne(x):
        vus = f'{x["spectateurs"]:,}'.replace(",", "\u202f") if avec_chiffres else ""
        return f"""<div style="display:flex;align-items:baseline;gap:24px;margin-top:26px">
  <div style="flex:1;min-width:0"><div style="font:900 52px/1 Montserrat;letter-spacing:-.03em">{html.escape(x['login'])}</div>
    <div style="margin-top:6px;font:400 28px/1.2 Jost;color:{LAVANDE};white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{titre_propre(x['titre'])}</div></div>
  <div style="font:500 32px Jost;color:{LAVANDE}">{vus}</div></div>"""

    heure = dt.datetime.now().strftime("%H h %M")
    note = f"Spectateurs relevés à {heure}. Source : Twitch." if avec_chiffres else f"Relevé à {heure}. Source : Twitch."
    contenu = f"""<div style="position:absolute;left:84px;right:84px;bottom:{bas}px">
  <div class="titre">{_insecable(html.escape(titre), titre=True)}</div>
  <div style="margin-top:24px">{''.join(ligne(x) for x in lives[:nb])}</div>
  <div class="texte" style="margin-top:44px;font-size:30px;color:{LAVANDE};opacity:.7">{note}</div>
</div>"""
    return _page(contenu, h)


def fabriquer(genre, dossier, **kw):
    c = config()
    jour = dt.date.today().isoformat()
    dossier = Path(dossier).expanduser() if dossier else c["dossier_sortie"] / "A VALIDER" / "INFOS"
    if not dossier.is_absolute():  # sous launchd le dossier courant est « / »
        dossier = c["dossier_sortie"] / dossier
    sorties = []
    if genre == "compte-a-rebours":
        for h, nom in ((1350, "fil"), (1920, "story")):
            html, legende, cle = compte_a_rebours(h=h)
            png = _rendre(html, dossier / f"{jour}-{cle}-{nom}.png", h)
            png.with_suffix(".txt").write_text(legende, encoding="utf-8")
            sorties.append(png)
    elif genre == "info":
        for h, nom in ((1350, "fil"), (1920, "story")):
            png = _rendre(info(kw["titre"], kw["texte"], kw["source"], h=h, fort=kw.get("fort")),
                          dossier / f"{jour}-info-{kw['cle']}-{nom}.png", h)
            if kw.get("legende"):
                png.with_suffix(".txt").write_text(kw["legende"], encoding="utf-8")
            sorties.append(png)
    elif genre == "live":
        lives = en_live(filtre_jeu=kw.get("filtre_jeu", True))
        if not lives:
            log("infos : personne en live sur le jeu, pas de carte")
            return []
        titre = "En live sur GTA 6" if kw.get("filtre_jeu", True) else "En live"
        tags = "#gta6 #gta6fr #twitchfr #streamerfr" if kw.get("filtre_jeu", True) else "#twitchfr #streamerfr #live"
        for h, nom in ((1350, "fil"), (1920, "story")):
            noms = ", ".join(x["login"] for x in lives[:5 if h == 1350 else 6])  # les mêmes que sur la carte
            png = _rendre(carte_live(lives, h=h, titre=titre), dossier / f"{jour}-live-{nom}.png", h)
            png.with_suffix(".txt").write_text(f"{titre} en ce moment : {noms} 🎮\n\n{tags}", encoding="utf-8")
            sorties.append(png)
    log(f"infos : {len(sorties)} carte(s) {genre} dans {dossier}")
    return sorties
