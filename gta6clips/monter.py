"""Montage vertical 1080x1920 d'un clip Twitch.

Deux mises en page :
- « flou » (par défaut) : le jeu recadré en 4:3 (bande de y 600 à 1410), l'image floutée derrière, l'accroche
  au-dessus de la bande pendant tout le clip, les sous-titres sur le haut du jeu (y 795) ;
- « empile » : si la position de la webcam du streamer est connue (streamers.json), sa cam en haut, le jeu en bas,
  l'accroche les 3 premières secondes.
Dans les deux cas, le crédit (avatar + TWITCH.TV/STREAMER) est en haut à gauche tout le clip (calque d'habillage.py).
Sous-titres mot à mot (mlx_whisper), mot prononcé en magenta, pas de pastille.
"""
import json
import re
import subprocess
from pathlib import Path

from .commun import police as fichier_police, encodeur_aac, TRAVAIL, SOURCES, config, lancer, log, paris, sonde, streamers
from .habillage import BANDE_FLOU_Y, calque

W, H = 1080, 1920
MODELE_WHISPER = "mlx-community/whisper-large-v3-turbo"
HALLUCINATIONS = ["amara.org", "sous-titres réalisés", "sous-titrage", "merci d'avoir regardé", "abonnez-vous",
                  "société radio-canada", "st' 501", "sous titres"]


# ---------- téléchargement et transcription ----------

def telecharger(url, clip_id):
    SOURCES.mkdir(parents=True, exist_ok=True)
    dest = SOURCES / f"{clip_id}.mp4"
    if dest.exists() and dest.stat().st_size > 10_000:
        return dest
    # formats paysage seulement (Twitch propose aussi des « portrait-* » recadrés au hasard)
    lancer(["yt-dlp", "-q", "--no-warnings", "-f", "1080/720/b[format_id!*=portrait]", "-o", str(dest), url], timeout=300)
    return dest


def _transcrire_faster(source, sortie):
    """Transcription sans carte graphique (machines de GitHub) : faster-whisper, même forme de sortie que mlx_whisper."""
    import os
    from faster_whisper import WhisperModel
    modele = WhisperModel(os.environ.get("GTA6_WHISPER_MODELE", "large-v3-turbo"), device="cpu", compute_type="int8")
    # son décodé par ffmpeg (16 kHz mono) : pas de dépendance à la version de PyAV
    import numpy as np
    brut = subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                          capture_output=True, check=True).stdout
    segs, _ = modele.transcribe(np.frombuffer(brut, dtype=np.float32), language="fr", word_timestamps=True, condition_on_previous_text=False,
                                hallucination_silence_threshold=1.5)
    d = {"segments": [{"text": s.text, "no_speech_prob": s.no_speech_prob, "avg_logprob": s.avg_logprob,
                       "words": [{"word": w.word, "start": w.start, "end": w.end} for w in (s.words or [])]}
                      for s in segs]}
    sortie.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def transcrire(source, clip_id):
    TRAVAIL.mkdir(parents=True, exist_ok=True)
    sortie = TRAVAIL / f"{clip_id}.json"
    if not sortie.exists():
        import os
        if os.environ.get("GTA6_WHISPER") == "faster":
            _transcrire_faster(source, sortie)
        else:
            lancer(["mlx_whisper", str(source), "--model", MODELE_WHISPER, "--language", "fr",
                    "--word-timestamps", "True", "--condition-on-previous-text", "False",
                    "--hallucination-silence-threshold", "1.5",
                    "--output-format", "json", "--output-dir", str(TRAVAIL), "--output-name", clip_id], timeout=900)
    d = json.loads(sortie.read_text(encoding="utf-8"))
    mots = []
    for seg in d.get("segments", []):
        texte = seg.get("text", "").lower()
        if seg.get("no_speech_prob", 0) > 0.6 or seg.get("avg_logprob", 0) < -1.2:
            continue
        if any(h in texte for h in HALLUCINATIONS):
            continue
        for w in seg.get("words", []):
            m = w["word"].strip()
            if not m:
                continue
            # whisper coupe les mots composés (« contre » + « -cœur ») et les élisions (« c » + « 'est ») : on recolle
            seul_ponctuation = not any(ch.isalnum() for ch in m)
            colle = (seul_ponctuation or m[0] in "-'’" or mots[-1]["mot"][-1:] in ("-", "'", "’")) if mots else False
            if colle and float(w["start"]) - mots[-1]["fin"] < 0.3:
                mots[-1]["mot"] += m
                mots[-1]["fin"] = float(w["end"])
                continue
            mots.append({"mot": m, "debut": float(w["start"]), "fin": float(w["end"])})
    return mots


# « putain » n'est pas masqué : à trancher par le propriétaire
GROS_MOTS_DEBUT = ("encul", "salop", "connard", "connass", "niqu", "batard", "bâtard", "tafiol", "negro", "nègre",
                   "negre", "pédé")
GROS_MOTS_EXACTS = {"fdp", "ntm", "pute", "putes", "pd", "pede", "tg"}


def censurer(mot):
    """Gros mots masqués à l'écran (le texte affiché pèse dans la diffusion TikTok). Début de mot seulement."""
    bas = mot.lower()
    elision = re.match(r"^[a-zà-ÿ]{1,2}['’]", bas)
    debut = elision.end() if elision else 0
    coeur = bas[debut:].strip(".,!?…;:\"«»")
    if coeur in GROS_MOTS_EXACTS or coeur.startswith(GROS_MOTS_DEBUT):
        lettres = list(mot)
        idx = [i for i, ch in enumerate(lettres) if ch.isalpha() and i >= debut]
        for i in idx[1:-1]:
            lettres[i] = "*"
        return "".join(lettres)
    return mot


def censurer_texte(texte):
    return " ".join(censurer(m) for m in texte.split())


def texte_dit(mots):
    return " ".join(m["mot"] for m in mots)


# ---------- sous-titres ASS ----------

def _t(s):
    s = max(0.0, s)
    h, r = divmod(s, 3600)
    m, r = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{r:05.2f}"


def _esc(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


_METRIQUES = {}


def largeur_texte(texte, taille, espacement=-3, police="Montserrat-Black.ttf"):
    """Largeur en pixels d'un texte rendu par libass (taille ASS = hauteur Windows de la police, comme VSFilter)."""
    if police not in _METRIQUES:
        from fontTools.ttLib import TTFont
        f = TTFont(str(fichier_police(police)))
        _METRIQUES[police] = (f.getBestCmap(), f["hmtx"].metrics, f["OS/2"].usWinAscent + f["OS/2"].usWinDescent)
    cmap, hmtx, hauteur = _METRIQUES[police]
    k = taille / hauteur
    defaut = cmap.get(ord("o"))
    return sum(hmtx[cmap.get(ord(c), defaut)][0] * k for c in texte) + espacement * max(0, len(texte) - 1)


def _couper_largeur(mots, taille, limite):
    lignes, cur = [], ""
    for m in mots:
        essai = f"{cur} {m}".strip()
        if cur and largeur_texte(essai, taille) > limite:
            lignes.append(cur)
            cur = m
        else:
            cur = essai
    return lignes + ([cur] if cur else [])


def ajuster_accroche(texte, hauteur_max, largeur_max=820, taille_max=84, taille_min=44):
    """Plus grande taille où l'accroche tient dans la boîte (zone commune : 840 px de large, moins le contour),
    lignes équilibrées."""
    mots = texte.split()
    if not mots:
        return [], taille_max
    for taille in range(taille_max, taille_min - 1, -2):
        lignes = _couper_largeur(mots, taille, largeur_max)
        if len(lignes) * taille <= hauteur_max and all(largeur_texte(l, taille) <= largeur_max for l in lignes):
            break
    # équilibrage : on resserre la largeur tant que le nombre de lignes ne change pas (pas de mot seul en bas)
    n, limite = len(lignes), max(largeur_texte(l, taille) for l in lignes)
    while limite > 200:
        essai = _couper_largeur(mots, taille, limite - 10)
        nouvelle = max(largeur_texte(l, taille) for l in essai)
        if len(essai) != n or nouvelle >= limite:  # un seul long mot ne se resserre plus : on s'arrête
            break
        lignes, limite = essai, nouvelle
    return lignes, taille


def couper_lignes(texte, max_car):
    mots, lignes, cur = texte.split(), [], ""
    for m in mots:
        if cur and len(cur) + 1 + len(m) > max_car:
            lignes.append(cur)
            cur = m
        else:
            cur = f"{cur} {m}".strip()
    if cur:
        lignes.append(cur)
    return lignes


def grouper(mots, max_mots=3, max_car=15, pause=0.35):
    """Groupes de 1 à 3 mots, coupés aux pauses et à la ponctuation forte."""
    groupes, cur = [], []
    for i, m in enumerate(mots):
        if cur:
            trop_long = len(" ".join(x["mot"] for x in cur + [m])) > max_car
            coupe = m["debut"] - cur[-1]["fin"] > pause or re.search(r"[.!?…]$", cur[-1]["mot"])
            if len(cur) >= max_mots or trop_long or coupe:
                groupes.append(cur)
                cur = []
        cur.append(m)
    if cur:
        groupes.append(cur)
    return groupes


# styles de l'accroche : couleurs (texte, secondaire, contour ou bloc, ombre) puis bord (BorderStyle, Outline, Shadow)
# « contour » : blanc cerné de nuit (version du 11/11, jugée trop lourde par le propriétaire le soir même) ;
# « ombre » : blanc sans contour, ombre nette ; « bloc » : nuit sur bloc crème ; « bloc_magenta » : blanc sur magenta
STYLES_ACCROCHE = {
    "contour": "&H00FFFFFF,&H00FFFFFF,{nuit},{nuit}",
    "ombre": "&H00FFFFFF,&H00FFFFFF,{nuit},&H55" + "1A0C0F",
    "bloc": "{nuit},{nuit},&H00CBF9FF,&H00CBF9FF",
    "bloc_magenta": "&H00FFFFFF,&H00FFFFFF,&H00E963D2,&H00E963D2",
}
BORDS_ACCROCHE = {"contour": "1,10,5", "ombre": "1,0,5", "bloc": "3,18,0", "bloc_magenta": "3,18,0"}


def colorer(texte, mot_fort, couleur):
    """Met en couleur la première occurrence du mot fort (un mot ou deux liés), même coupée par un retour à la ligne."""
    if not mot_fort or not mot_fort.split():
        return texte
    motif = r"(?<!\w)" + r"\s+".join(re.escape(m) for m in mot_fort.split()) + r"(?!\w)"
    m = re.search(motif, texte, re.I)
    if not m:
        return texte
    morceau = re.sub(r"\S+", lambda x: f"{{\\1c{couleur}}}{x.group(0)}{{\\1c&H00FFFFFF}}", m.group(0))
    return texte[:m.start()] + morceau + texte[m.end():]


def contexte_jeu(jeu, quand=None):
    """Petite ligne de contexte au-dessus de l'accroche (18/11, le propriétaire : « on comprend pas trop sur le reel »).
    Jour compté depuis la sortie en France (19/11/2026) ; le 18/11 (Nouvelle-Zélande), c'est aussi le premier jour."""
    import datetime as dt
    from .radar import est_gta6
    if not est_gta6(jeu):
        return ""
    jour = ((quand or paris()) - dt.timedelta(hours=5)).date()  # journée du pilote : de 5 h à 5 h
    n = max(1, (jour - dt.date(2026, 11, 19)).days + 1)
    if n > 30:
        return "SUR GTA 6"
    return f"{'1ER' if n == 1 else str(n) + 'E'} JOUR SUR GTA 6"


def ecrire_ass(chemin, mots, accroche, credit, mise_en_page, duree, decalage, h, mot_fort=None, contexte=""):
    pol = h["police"]
    # zone commune Instagram/TikTok (règle du 06/11) : texte entre x 120 et 960, y 285 à 1248, et sous y 840
    # rien à droite de x 780. Sous-titres centrés à y 795 (au-dessus de 840), crédit court à y 1235.
    y_sous = 803 if mise_en_page == "empile" else 795  # en empilé, à 13 px sous le filet de la couture (757-763)
    acc = censurer_texte(accroche)
    acc = acc.lower() if h.get("accroche_casse", "minuscules") == "minuscules" else acc.upper()
    if acc:  # un titre en bas de casse commence par une majuscule (règle du propriétaire du 06/11)
        acc = acc[0].upper() + acc[1:]
    # flou : entre la puce du crédit (fin à y 390) et la bande de jeu (y 600), moins le contour et l'ombre ;
    # empile : sur la cam, au-dessus de la couture (y 757)
    hauteur_acc = BANDE_FLOU_Y - 390 - 60 if mise_en_page == "flou" else 180
    if contexte and mise_en_page == "flou":
        hauteur_acc -= 46  # place pour la ligne de contexte entre le crédit et l'accroche
    lignes_accroche, taille_acc = ajuster_accroche(acc, hauteur_acc)
    # l'interlettrage du style est ignoré par le libass de ffmpeg 7.1 (mesuré le 11/11/2026 : 942 px au lieu
    # de 825) ; seule la balise \fsp dans chaque ligne serre vraiment les lettres
    nuit = h.get("couleur_contour", "&H001A0C0F")
    style = h.get("style_texte", "contour")
    flou_sous = "" if style == "contour" else "\\blur1.5"
    entete = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Accroche,{pol},{taille_acc},{STYLES_ACCROCHE[style].format(nuit=nuit)},0,0,0,0,100,100,-3,0,{BORDS_ACCROCHE[style]},5,0,0,0,1
Style: Sous,{h['police_sous_titres']},76,&H00FFFFFF,&H00FFFFFF,{nuit},{nuit[:2]}{'40' if style != 'contour' else '00'}{nuit[4:]},0,0,0,0,100,100,-2,0,1,{9 if style == 'contour' else 3},{0 if style == 'contour' else 2},5,0,0,0,1
Style: Contexte,Montserrat ExtraBold,42,&H00FFFFFF,&H00FFFFFF,{nuit},{nuit[:2]}40{nuit[4:]},0,0,0,0,100,100,-1,0,1,2,2,5,0,0,0,1
Style: Credit,{pol},34,&H1AFFFFFF,&H1AFFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,0,5,0,0,0,1
Style: Appel,Montserrat ExtraBold,58,&H00FFFFFF,&H00FFFFFF,{nuit},{nuit[:2]}00{nuit[4:]},0,0,0,0,100,100,-1,0,1,4,5,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    # accroche : arrive entière à la première image ; tout le clip (« flou », ou accroche_tout_le_clip), sinon 3 s
    fin_acc = duree if (mise_en_page == "flou" or h.get("accroche_tout_le_clip")) else h["accroche_duree_s"]
    y_acc = (390 + BANDE_FLOU_Y) // 2 if mise_en_page == "flou" else 630
    if contexte:
        haut_bloc = round(len(lignes_accroche) * taille_acc * 0.92)
        if mise_en_page == "flou":
            y_acc += 23
        y_ctx = y_acc - haut_bloc // 2 - 30
        ctx = _esc(contexte).replace("GTA 6", f"{{\\1c{h.get('couleur_mot_fort', h['couleur_mot'])}}}GTA 6{{\\1c&H00FFFFFF}}")
        ev.append(f"Dialogue: 2,{_t(0)},{_t(fin_acc)},Contexte,,0,0,0,,{{\\pos(540,{y_ctx})\\fsp-1}}{ctx}")
    texte_acc = colorer("\n".join(_esc(l) for l in lignes_accroche), _esc(censurer_texte(mot_fort or "")),
                        h.get("couleur_mot_fort", h["couleur_mot"])).replace("\n", "\\N")
    ev.append(f"Dialogue: 2,{_t(0)},{_t(fin_acc)},Accroche,,0,0,0,,{{\\pos(540,{y_acc})\\fsp-3}}{texte_acc}")

    couleur_mot = h["couleur_mot"]
    groupes = grouper([dict(m, debut=m["debut"] - decalage, fin=m["fin"] - decalage) for m in mots if m["fin"] - decalage > 0.05])
    for gi, g in enumerate(groupes):
        fin_groupe = g[-1]["fin"] + 0.25
        if gi + 1 < len(groupes):
            fin_groupe = min(fin_groupe, groupes[gi + 1][0]["debut"] - 0.05)
        for i, m in enumerate(g):
            debut = m["debut"] if i else max(0.0, m["debut"] - 0.05)
            fin = g[i + 1]["debut"] if i + 1 < len(g) else fin_groupe
            if fin - debut < 0.04:
                continue
            morceaux = []
            for j, x in enumerate(g):
                mot = _esc(censurer(x["mot"]).upper())
                morceaux.append(f"{{\\1c{couleur_mot}}}{mot}{{\\1c&H00FFFFFF}}" if j == i else mot)
            txt = " ".join(morceaux)
            ev.append(f"Dialogue: 0,{_t(debut)},{_t(min(fin, duree))},Sous,,0,0,0,,{{\\pos(540,{y_sous})\\fsp-2{flou_sous}}}{txt}")
    # appel à s'abonner dans la vidéo (18/11 : 200 vues par reel et aucun abonné, la légende n'est presque pas lue) :
    # les dernières secondes, dans la bande de jeu, sous les sous-titres
    appel = h.get("appel_video")
    if appel and duree > 6:
        deb = duree - h.get("appel_video_duree_s", 2.5)
        txt = _esc(appel).replace("ABONNE-TOI", f"{{\\1c{h.get('couleur_mot_fort', h['couleur_mot'])}}}ABONNE-TOI{{\\1c&H00FFFFFF}}")
        ev.append(f"Dialogue: 3,{_t(deb)},{_t(duree)},Appel,,0,0,0,,"
                  f"{{\\pos(540,1090)\\fsp-1\\fad(120,0)\\fscx70\\fscy70\\t(0,180,\\fscx100\\fscy100)}}{txt}")
    Path(chemin).write_text(entete + "\n".join(ev) + "\n", encoding="utf-8")


# ---------- rendu ----------

def debut_utile(mots):
    """Coupe un long blanc au début (au plus 4 s), en gardant 0,6 s de marge avant la première phrase."""
    if not mots:
        return 0.0
    premier = mots[0]["debut"]
    return min(4.0, premier - 0.6) if premier > 3.0 else 0.0


def mesure_volume(source, debut, cible):
    """Premier passage de loudnorm : mesure, pour un second passage linéaire précis."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", f"{debut:.3f}", "-i", str(source), "-vn", "-af",
                        f"loudnorm=I={cible}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    bloc = r.stderr[r.stderr.rfind("{"): r.stderr.rfind("}") + 1]
    try:
        return json.loads(bloc)
    except ValueError:
        return None


def rendre(source, sortie, mots, accroche, streamer, fiche=None, style=None, mot_fort=None, couleur_mot_fort=None, jeu=None):
    c = config()
    h = dict(c["habillage"], **({"style_texte": style} if style else {}),
             **({"couleur_mot_fort": couleur_mot_fort} if couleur_mot_fort else {}))
    info = sonde(source)
    decalage = debut_utile(mots)
    duree = info["duree"] - decalage
    fps = min(60, round(info["fps"])) or 30
    fiche = fiche or {}
    cam = fiche.get("cam")
    mise_en_page = "empile" if cam else "flou"
    ass = TRAVAIL / (Path(sortie).stem + ".ass")
    contexte = contexte_jeu(jeu)
    ecrire_ass(ass, mots, accroche, f"twitch.tv/{streamer}", mise_en_page, duree, decalage, h, mot_fort, contexte)

    if mise_en_page == "flou":
        filtre = (
            "[0:v]split=2[a][b];"
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=38,eq=brightness=-0.13:saturation=1.1[fond];"
            "[b]crop='min(iw,ih*4/3)':ih,scale=1080:810:flags=lanczos[jeu];"
            f"[fond][jeu]overlay=0:{BANDE_FLOU_Y}[v0];"
        )
    else:
        k = info["w"] / 1920  # positions notées sur une image 1920x1080, la source peut être en 720p
        x, y, w, hh = (round(v * k) for v in cam)
        ex, ey, ew, eh = (round(v * k) for v in (fiche.get("exclure") or cam))
        marge = round(16 * k)
        # zone de jeu : recadrage centré, décalé pour ne garder aucun morceau de la cam ni de son bandeau
        cw = round(info["h"] * 1080 / 1160)
        x0 = (info["w"] - cw) // 2
        if ex < x0 + cw and ex + ew > x0:
            x0 = max(x0, ex + ew + marge) if ex + ew / 2 < info["w"] / 2 else min(x0, ex - marge - cw)
        x0 = max(0, min(info["w"] - cw, x0))
        filtre = (
            "[0:v]split=2[a][b];"
            f"[a]crop={w}:{hh}:{x}:{y},scale=1080:760:force_original_aspect_ratio=increase,crop=1080:760[cam];"
            f"[b]crop={cw}:ih:{x0}:0,scale=1080:1160:flags=lanczos[jeu];"
            "[cam][jeu]vstack[v0];"
        )
    ass_echappe = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    filtre += f"[v0][1:v]overlay=0:0:format=auto[v1];[v1]ass='{ass_echappe}',fps={fps},format=yuv420p[v]"
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{decalage:.3f}", "-i", str(source),
           "-loop", "1", "-i", str(calque(streamer, mise_en_page, "GTA 6" if contexte else None)),
           "-filter_complex", filtre, "-map", "[v]"]
    if info["audio"]:
        m = mesure_volume(source, decalage, h["loudness"])
        norme = f"loudnorm=I={h['loudness']}:TP=-1.5:LRA=11"
        if m:
            norme += (f":measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
                      f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
        cmd += ["-map", "0:a:0", "-af",
                f"{norme},afade=t=in:d=0.03,afade=t=out:st={max(0, duree - 0.15):.2f}:d=0.15",
                "-c:a", encodeur_aac(), "-b:a", h["debit_audio"], "-ar", "48000"]
    # qualité maximale (règle du propriétaire du 09/11) : débit constant autour de 28 Mbit/s, AAC 320k
    cmd += ["-c:v", "libx264", "-preset", "medium", "-profile:v", "high", "-b:v", h["debit_video"],
            "-minrate", h["debit_video"], "-maxrate", h["debit_video"], "-bufsize", "56M",
            "-x264-params", "nal-hrd=cbr", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-t", f"{duree:.3f}", str(sortie)]
    lancer(cmd, timeout=900)
    log(f"rendu {Path(sortie).name} ({duree:.1f} s, {mise_en_page})")
    return sortie


def fiche_streamer(login):
    return streamers().get(login.lower(), {})
