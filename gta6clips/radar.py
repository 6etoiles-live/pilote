"""Radar : repère les clips GTA 6 les plus vus des streamers français.

Deux sources :
- l'API officielle Twitch (Helix) si config.local.json contient twitch_client_id et twitch_client_secret :
  toute la catégorie du jeu, filtrée sur la langue française, plus la liste des lives FR en cours ;
- sinon la liste blanche streamers.json, lue avec yt-dlp (clips des dernières 24 h de chaque chaîne).
"""
import datetime as dt
import json
import urllib.parse
import urllib.request

from .commun import base, config, log, maintenant, streamers, DONNEES


# ---------- API Twitch officielle ----------

def _http(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _jeton(cid, secret):
    q = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
    return _http("https://id.twitch.tv/oauth2/token?" + q, data=b"")["access_token"]


def helix(c, jeu):
    cid, secret = c["secret"]["twitch_client_id"], c["secret"]["twitch_client_secret"]
    h = {"Client-Id": cid, "Authorization": "Bearer " + _jeton(cid, secret)}
    g = _http("https://api.twitch.tv/helix/games?" + urllib.parse.urlencode({"name": jeu}), h)["data"]
    if not g:
        raise RuntimeError(f"Jeu introuvable sur Twitch : {jeu}")
    game_id = g[0]["id"]

    # lives FR en cours : sert à découvrir des streamers et à savoir où ça se passe
    lives = _http("https://api.twitch.tv/helix/streams?" + urllib.parse.urlencode(
        {"game_id": game_id, "language": c["langue"], "first": 100}), h)["data"]
    (DONNEES / "lives.json").write_text(json.dumps(
        {"jeu": jeu, "lives": [{"login": s["user_login"], "spectateurs": s["viewer_count"], "titre": s["title"]} for s in lives]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    decouvrir([s["user_login"] for s in lives if s.get("viewer_count", 0) >= c.get("spectateurs_min_decouverte", 50)])

    fin = maintenant()
    debut = fin - dt.timedelta(hours=c["fenetre_heures"])
    clips, apres = [], None
    for _ in range(10):  # 10 pages de 100 clips classés par vues
        p = {"game_id": game_id, "first": 100, "started_at": debut.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "ended_at": fin.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if apres:
            p["after"] = apres
        r = _http("https://api.twitch.tv/helix/clips?" + urllib.parse.urlencode(p), h)
        for x in r["data"]:
            if x.get("language", "").split("-")[0] != c["langue"]:
                continue
            clips.append({"id": x["id"], "url": x["url"], "streamer": x["broadcaster_name"].lower(),
                          "titre": x["title"], "jeu": jeu, "langue": x["language"], "vues": x["view_count"],
                          "duree": x["duration"], "cree_le": x["created_at"], "video_id": x.get("video_id") or "",
                          "vod_offset": x.get("vod_offset")})
        apres = r.get("pagination", {}).get("cursor")
        if not apres:
            break
    log(f"radar helix : {len(lives)} lives FR, {len(clips)} clips FR sur {debut:%H:%M}-{fin:%H:%M} UTC")
    return clips


# ---------- repli : liste blanche lue avec yt-dlp ----------

def _ytdlp_lot(options, urls, timeout=420):
    """Un seul lancement de yt-dlp pour plusieurs adresses : sur ce Mac, le démarrage seul coûte ~13 s
    (mesuré le 12/11/2026), un lancement par clip rendait le radar trop lent (35 à 80 s par appel)."""
    import subprocess
    if not urls:
        return []
    try:
        r = subprocess.run(["yt-dlp", *options, "--ignore-errors", "--no-warnings", *urls],
                           capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:  # une chaîne lente ne doit pas faire tomber tout le radar
        log(f"radar : paquet de {len(urls)} adresse(s) abandonné ({type(e).__name__})")
        return []
    lignes = []
    for l in r.stdout.splitlines():
        if l.startswith("{"):
            try:
                lignes.append(json.loads(l))
            except ValueError:
                pass
    return lignes


def _paquets(liste, n):
    return [liste[i:i + n] for i in range(0, len(liste), n)]


HORS_JEU = {}  # dict pour garder l'ordre d'arrivée (les plus anciens sortent en premier)
VUES_CONNUES = {}  # vues relues des clips déjà en base, pendant le passage en cours


def decouvrir(logins):
    """Streamers FR vus en live sur le jeu : ajoutés à streamers.json (le repli sans API les suivra aussi)."""
    p = DONNEES.parent / "streamers.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    connus = {s["login"].lower() for s in d["streamers"]}
    neufs = [l.lower() for l in logins if l.lower() not in connus]
    if not neufs:
        return 0
    for l in dict.fromkeys(neufs):
        d["streamers"].append({"login": l, "accord": "inconnu", "cam": None, "auto": maintenant().date().isoformat()})
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"radar : {len(neufs)} streamer(s) découvert(s) en live : {', '.join(neufs[:10])}")
    return len(neufs)


def _slug(url):
    return (url or "").rstrip("/").split("/")[-1].split("?")[0]


def liste_blanche(c, jeu, db):
    from concurrent.futures import ThreadPoolExecutor
    en_plus = jeux_en_attendant(c)
    f = DONNEES / "hors_jeu.json"
    for s_ in (json.loads(f.read_text()) if f.exists() else []):
        HORS_JEU[s_] = 1
    deja = {r["id"] for r in db.execute("select id from clips")}
    logins = [l for l, s in streamers().items() if not s.get("noir")]

    # 1. clips des dernières 24 h de chaque chaîne, par paquets de 10 chaînes (4 lancements en parallèle)
    urls = [f"https://www.twitch.tv/{l}/clips?filter=clips&range=24hr" for l in logins]
    with ThreadPoolExecutor(max_workers=4) as ex:
        lots = list(ex.map(lambda u: _ytdlp_lot(["--flat-playlist", "-j"], u), _paquets(urls, 10)))
    par_chaine = {}
    for e in (x for lot in lots for x in lot):
        slug = _slug(e.get("url"))
        login = (e.get("playlist_id") or "").lower()
        if not slug or slug in HORS_JEU:
            continue
        if slug in deja:
            if e.get("view_count") is not None:
                VUES_CONNUES[slug] = e["view_count"]  # vitesse des clips déjà connus
            continue
        if (e.get("view_count") or 0) >= c["vues_min"]:
            par_chaine.setdefault(login, []).append(e)

    # 2. détail (catégorie, titre, date) des N plus vus de chaque chaîne, par paquets de 15 clips
    a_voir = []
    for login, es in par_chaine.items():
        es.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
        a_voir += [(login, e["url"]) for e in es[: c["clips_par_streamer_et_tour"]]]
    login_de = {_slug(u): l for l, u in a_voir}
    with ThreadPoolExecutor(max_workers=4) as ex:
        lots = list(ex.map(lambda u: _ytdlp_lot(["-j", "--skip-download"], u), _paquets([u for _, u in a_voir], 15)))
    clips = []
    for i in (x for lot in lots for x in lot):
        slug = i.get("display_id") or _slug(i.get("webpage_url"))
        cat = (i.get("categories") or [""])[0]
        if cat != jeu and cat not in en_plus:
            HORS_JEU[slug] = 1  # mémorisé pour ne pas le redemander au prochain passage
            continue
        clips.append({"id": slug, "url": i.get("webpage_url") or f"https://clips.twitch.tv/{slug}",
                      "streamer": login_de.get(slug) or (i.get("channel") or "").lower(), "titre": i.get("title") or "",
                      "jeu": cat, "langue": c["langue"], "vues": i.get("view_count") or 0, "duree": i.get("duration") or 0,
                      "cree_le": dt.datetime.fromtimestamp(i.get("timestamp") or 0, dt.timezone.utc).isoformat(),
                      "video_id": "", "vod_offset": None})
    f.write_text(json.dumps(list(HORS_JEU)[-5000:]))
    log(f"radar liste blanche : {len(clips)} nouveaux clips « {' / '.join([jeu] + en_plus)} » sur {len(logins)} chaînes "
        f"({len(a_voir)} détaillés, {len(VUES_CONNUES)} revus)")
    return clips


# ---------- tri, doublons, filtres ----------

def _date(txt):
    try:
        return dt.datetime.fromisoformat((txt or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_h(cree_le):
    t = _date(cree_le)
    if t is None:
        return 999.0
    return max(0.0, (maintenant() - t).total_seconds() / 3600)


def _meme_moment(a, b, ecart):
    if a["streamer"] != b["streamer"]:
        return False
    if a.get("video_id") and a["video_id"] == b.get("video_id") and a.get("vod_offset") is not None and b.get("vod_offset") is not None:
        return abs(a["vod_offset"] - b["vod_offset"]) < ecart
    ta, tb = _date(a.get("cree_le")), _date(b.get("cree_le"))
    if ta is None or tb is None:
        return False
    return abs((ta - tb).total_seconds()) < ecart


def jeux_en_attendant(c):
    """Avant la sortie, la page publie aussi quelques clips d'autres jeux GTA (pilote.jeux_avant_sortie)."""
    if maintenant() >= sortie_du_jeu(c):
        return []
    return list(c.get("pilote", {}).get("jeux_avant_sortie", []))


def sortie_du_jeu(c):
    return _date(c.get("sortie_jeu", "2026-11-18T23:00:00Z"))


def est_gta6(jeu):
    import re
    return bool(re.search(r"\b(VI|6)\b", jeu or ""))


def verdict(c, clip, fiches):
    t = (clip["titre"] or "").lower()
    cree = _date(clip.get("cree_le"))
    if est_gta6(clip.get("jeu")) and (cree is None or cree < sortie_du_jeu(c)):
        # avant la sortie officielle, des images de GTA 6 ne peuvent être qu'une fuite, un embargo ou une bande-annonce
        return "rejete", "avant la sortie officielle (fuite, embargo ou bande-annonce)"
    s = fiches.get(clip["streamer"], {})
    if s.get("noir") or s.get("accord") == "non":
        return "rejete", "streamer refusé"
    if any(m in t for m in c["mots_bannis"]):
        return "rejete", "triche ou fuite"
    if any(m in t for m in c["mots_spoiler"]):
        return "rejete", "spoiler probable"
    if not (c["duree_min_s"] <= (clip["duree"] or 0) <= c["duree_max_s"]):
        return "rejete", f"durée {clip['duree']:.0f} s"
    return "nouveau", None


def tourner(jeu=None):
    c = config()
    jeu = jeu or c["jeu"]
    db = base()
    if c["secret"].get("twitch_client_id") and c["secret"].get("twitch_client_secret"):
        clips = helix(c, jeu)
    else:
        clips = liste_blanche(c, jeu, db)

    fiches = streamers()
    # doublons : plusieurs spectateurs clippent souvent le même moment, on garde le plus vu
    clips.sort(key=lambda x: x["vues"], reverse=True)
    gardes = []
    connus = [dict(r) for r in db.execute("select * from clips")]
    for x in clips:
        if any(_meme_moment(x, g, c["ecart_meme_moment_s"]) for g in gardes + connus if g["id"] != x["id"]):
            continue
        gardes.append(x)

    nouveaux = 0
    for x in gardes:
        x["score"] = score(db, x["id"], x["vues"], x["cree_le"])
        if fiches.get(x["streamer"], {}).get("accord") in ("oui", "programme"):
            x["score"] *= 1.5
        statut, raison = verdict(c, x, fiches)
        r = db.execute("select statut from clips where id = ?", (x["id"],)).fetchone()
        releve(db, x["id"], x["vues"])
        if r:
            db.execute("update clips set vues = ?, score = ?, maj = ? where id = ?",
                       (x["vues"], x["score"], maintenant().isoformat(), x["id"]))
        else:
            db.execute("""insert into clips (id, url, streamer, titre, jeu, langue, vues, duree, cree_le, video_id,
                          vod_offset, score, statut, raison, vu_le, maj) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (x["id"], x["url"], x["streamer"], x["titre"], x["jeu"], x["langue"], x["vues"], x["duree"],
                        x["cree_le"], x["video_id"], x["vod_offset"], x["score"], statut, raison,
                        maintenant().isoformat(), maintenant().isoformat()))
            nouveaux += 1
    # clips déjà en base revus dans la liste d'une chaîne (repli sans API) : vues et vitesse mises à jour
    for cid, vues in VUES_CONNUES.items():
        r = db.execute("select cree_le from clips where id = ?", (cid,)).fetchone()
        if r:
            db.execute("update clips set vues = ?, score = ?, maj = ? where id = ?",
                       (vues, score(db, cid, vues, r["cree_le"]), maintenant().isoformat(), cid))
            releve(db, cid, vues)
    VUES_CONNUES.clear()
    db.commit()
    log(f"radar : {nouveaux} clips ajoutés, {len(clips) - len(gardes)} doublons écartés")
    return nouveaux


def releve(db, cid, vues):
    db.execute("insert into releves (clip_id, le, vues) values (?, ?, ?)", (cid, maintenant().isoformat(), vues))


def score(db, cid, vues, cree_le):
    """Vues par heure depuis la création, ou vitesse récente entre deux relevés si elle est plus forte
    (un clip qui décolle passe devant un clip plus ancien qui a déjà fait son plein)."""
    base_ = vues / (_age_h(cree_le) + 0.5)
    r = db.execute("select le, vues from releves where clip_id = ? order by le desc limit 1", (cid,)).fetchone()
    if r:
        dh = (maintenant() - _date(r["le"])).total_seconds() / 3600
        if dh >= 0.15:
            base_ = max(base_, (vues - r["vues"]) / dh)
    return round(base_, 1)
