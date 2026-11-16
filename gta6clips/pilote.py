"""Pilote automatique de la page 6 Étoiles (pleins pouvoirs donnés par le propriétaire le 11/11/2026).

À chaque passage (launchd, toutes les 15 min) :
1. le radar relève les clips FR du jeu et leur vitesse (radar.py) ;
2. preparer() prend les plus rapides, les fait analyser par Claude et ne monte que ceux qui passent la barrière
   (jeu libre, pas de spoiler, pas de musique sous licence, sujet pas déjà publié, intérêt au-dessus du seuil) ;
3. cartes_du_jour() fabrique la carte du compte à rebours (jusqu'au 18/11) ;
4. publier_du() publie, plateforme par plateforme, le meilleur élément prêt si l'écart minimum depuis le dernier
   post est passé, dans les heures actives et sous le plafond du jour ;
5. point() écrit un point court pour le propriétaire dans ~/Movies/GTA6 CLIPS/POINT.txt.
Sans comptes branchés (pilote.publier_avec = « aucun »), l'étape 4 simule : elle note ce qui serait parti.
Un élément qui échoue deux fois sur une plateforme n'y est plus retenté.
"""
import datetime as dt
import json
import math
import shutil
import subprocess
import time
from pathlib import Path

from . import analyse, infos, monter
from .commun import corbeille, en_jpeg, DONNEES, JOURNAL, base, config, log, maj, maintenant, paris, sonde, streamers
from .diffuseurs import Diffusion

DEFAUT = {
    "publier_avec": "aucun",
    "seuil_interet": 6,
    "preparer_par_tour": 3,
    "analyses_max_par_jour": 80,
    "fraicheur_max_heures": 30,
    "heures_actives": ["08:00", "01:30"],
    "carte_heure": "18:30",
    "jeux": ["Grand Theft Auto VI"],
    "jeux_avant_sortie": [],
    "avant_sortie_clips_max_jour": 3,
    "plateformes": {"tiktok": {"max_jour": 8, "ecart_min_minutes": 75},
                    "instagram": {"max_jour": 4, "ecart_min_minutes": 150}},
}


def reglages(c=None):
    c = c or config()
    r = dict(DEFAUT, **c.get("pilote", {}))
    return r


def _iso(t):
    return t.astimezone(dt.timezone.utc).isoformat()


def _date(txt):
    try:
        d = dt.datetime.fromisoformat((txt or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def refuses():
    """Streamers en liste noire ou qui ont dit non : relu à chaque usage (un retrait peut tomber en plein passage)."""
    return {l for l, s in streamers().items() if s.get("noir") or s.get("accord") == "non"}


def avant_la_sortie(c, clip):
    """Deuxième barrière après celle du radar : GTA 6 filmé avant la sortie = fuite, embargo ou bande-annonce."""
    from .radar import est_gta6, sortie_du_jeu
    cree = _date(clip.get("cree_le"))
    return est_gta6(clip.get("jeu")) and (cree is None or cree < sortie_du_jeu(c))


def debut_du_jour():
    """Une journée de la page va de 5 h à 5 h (heure de Paris) : les soirées de jeu finissent tard."""
    p = paris()
    d = p.replace(hour=5, minute=0, second=0, microsecond=0)
    return d if p >= d else d - dt.timedelta(days=1)


def _creneaux(R):
    """Créneaux de publication en minutes depuis 5 h (une journée va de 5 h à 5 h)."""
    paires = R.get("creneaux") or [R["heures_actives"]]
    out = []
    for deb, fin in paires:
        d, f = (((int(h.split(":")[0]) * 60 + int(h.split(":")[1])) - 300) % 1440 for h in (deb, fin))
        out.append((d, f if f > d else 1440))
    return out


def _minute_du_jour(p):
    return ((p.hour * 60 + p.minute) - 300) % 1440


def dans_les_heures(R, p=None):
    m = _minute_du_jour(p or paris())
    return any(d <= m < f for d, f in _creneaux(R))


def minutes_restantes(R, p=None):
    """Minutes de publication qui restent aujourd'hui, tous créneaux confondus."""
    m = _minute_du_jour(p or paris())
    return sum(max(0, f - max(m, d)) for d, f in _creneaux(R))


# ---------- préparation ----------

def _compteur_analyses(ajouter=0):
    f = DONNEES / "analyses.json"
    d = json.loads(f.read_text()) if f.exists() else {}
    jour = debut_du_jour().date().isoformat()
    d = {jour: d.get(jour, 0) + ajouter}
    f.write_text(json.dumps(d))
    return d[jour]


def recents(db):
    limite = _iso(maintenant() - dt.timedelta(hours=48))
    return [r["accroche"] for r in db.execute(
        "select accroche from file where genre = 'clip' and statut in ('pret', 'publie') and ajoute_le >= ? "
        "and accroche is not null order by ajoute_le desc limit 20", (limite,))]


def barriere(a, R):
    """Raison de ne pas publier, ou None."""
    if a.get("voie") == "regles":
        return "pas d'analyse par Claude : jamais publié seul"
    if a.get("image") != "jeu_libre":
        return f"image « {a.get('image')} » (cinématique, menu ou autre)"
    if a.get("spoiler"):
        return "spoiler"
    if a.get("musique_sous_licence"):
        return "musique sous licence audible"
    if a.get("deja_vu"):
        return "même sujet qu'un post récent"
    if not (a.get("accroche") or "").strip():
        return "accroche vide"
    if (a.get("interet") or 0) < R["seuil_interet"]:
        return f"intérêt {a.get('interet')}/10 sous le seuil de {R['seuil_interet']}"
    return None


def controler_rendu(sortie):
    """Contrôle du fichier rendu avant la file : format, durée, son."""
    try:
        i = sonde(sortie)
    except Exception as e:
        return f"fichier illisible ({e})"
    if (i["w"], i["h"]) != (1080, 1920):
        return f"format {i['w']}x{i['h']}"
    if i["duree"] < 5:
        return f"durée {i['duree']:.1f} s"
    if not i["audio"]:
        return "pas de son"
    return None


def a_la_corbeille(*fichiers):
    for f in fichiers:
        f = Path(f)
        if f.exists():
            corbeille(f)


def preparer_un(db, clip, c, R):
    cid = clip["id"]
    if avant_la_sortie(c, clip):
        maj(db, cid, statut="rejete", raison="avant la sortie officielle (fuite, embargo ou bande-annonce)")
        return None
    try:
        source = monter.telecharger(clip["url"], cid)
        maj(db, cid, statut="telecharge", source=str(source))
        mots = monter.transcrire(source, cid)
        a = analyse.analyser(source, clip["titre"], clip["streamer"], monter.texte_dit(mots), clip.get("jeu") or None,
                             recents=recents(db))
        if a.get("voie") == "regles" and a.get("indisponible"):  # limite d'abonnement, jeton : le clip attend, on retentera
            maj(db, cid, statut="attente", raison="Claude indisponible (limite ou jeton) : retenté plus tard")
            log("pilote : Claude indisponible, préparation suspendue jusqu'au passage suivant")
            return "pause"
        _compteur_analyses(1)
        from .radar import est_gta6
        seuil = R["seuil_interet"] if est_gta6(clip.get("jeu")) else R.get("seuil_interet_avant_sortie", R["seuil_interet"])
        from .radar import suiveurs
        if suiveurs([clip["streamer"]]).get((clip["streamer"] or "").lower(), 0) >= R.get("abonnes_star", 10**12):
            seuil -= 1  # gros streamer (abonnés Twitch) : un point d'exigence en moins
            seuil -= 1
        raison = barriere(a, {**R, "seuil_interet": seuil})
        if raison:
            maj(db, cid, statut="attente" if a.get("voie") == "regles" else "rejete", raison=raison,
                accroche=a.get("accroche"))
            log(f"pilote : {cid} écarté ({raison})")
            return None
        legende = analyse.legende_complete(c, a["legende"], clip["streamer"], clip.get("jeu"))
        d = c["dossier_sortie"] / "A PUBLIER" / paris().strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        sortie = d / f"{clip['streamer']}-{cid[:24]}.mp4"
        monter.rendre(source, sortie, mots, a["accroche"], clip["streamer"], fiche_avec_cam(clip["streamer"], a),
                      mot_fort=a.get("mot_fort"))
        sortie.with_suffix(".txt").write_text(legende, encoding="utf-8")
        defaut = controler_rendu(sortie)
        if defaut:
            a_la_corbeille(sortie, sortie.with_suffix(".txt"))
            maj(db, cid, statut="erreur", raison=f"rendu refusé : {defaut}")
            log(f"pilote : rendu de {cid} refusé ({defaut})")
            return None
        if clip["streamer"] in refuses():  # retrait demandé pendant le montage
            a_la_corbeille(sortie, sortie.with_suffix(".txt"))
            maj(db, cid, statut="rejete", raison="retrait demandé par le streamer")
            return None
        cree = _date(clip.get("cree_le")) or maintenant()
        expire = cree + dt.timedelta(hours=R["fraicheur_max_heures"])
        from .radar import est_gta6, sortie_du_jeu
        if not est_gta6(clip.get("jeu")):  # un clip « en attendant » ne part plus une fois le jeu sorti
            if maintenant() >= sortie_du_jeu(c):
                a_la_corbeille(sortie, sortie.with_suffix(".txt"))
                maj(db, cid, statut="rejete", raison="clip d'attente fini après la sortie du jeu")
                return None
            expire = cree + dt.timedelta(hours=R.get("fraicheur_avant_sortie_heures", R["fraicheur_max_heures"]))
            expire = min(expire, sortie_du_jeu(c))
        db.execute("""insert or replace into file (id, genre, ref, fichier, legende, streamer, accroche, interet, inedit,
                      score, pas_avant, expire_le, statut, ajoute_le) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (f"clip:{cid}", "clip", cid, str(sortie), legende, clip["streamer"], a["accroche"], int(a["interet"]),
                    int(bool(a.get("inedit"))), clip.get("score") or 0, None,
                    _iso(expire), "pret", _iso(maintenant())))
        maj(db, cid, statut="pret", sortie=str(sortie), accroche=a["accroche"], legende=legende,
            raison=f"intérêt {a['interet']}/10{', inédit' if a.get('inedit') else ''} : {a.get('raison', '')}"[:300])
        log(f"pilote : prêt {sortie.name} (intérêt {a['interet']}/10, {a['accroche']})")
        return sortie
    except Exception as e:
        maj(db, cid, statut="erreur", raison=str(e)[:300])
        log(f"pilote : {cid} en erreur ({e})")
        return None


def preparer(maxi=None, jeux=None):
    c = config()
    R = reglages(c)
    maxi = max(maxi or 0, R["preparer_par_tour"])  # le réglage est un plancher (launchd lance avec --max 3)
    from .radar import jeux_en_attendant
    db = base()
    attente = jeux_en_attendant(c)
    jeux = jeux or (R["jeux"] + attente)
    if attente:  # avant la sortie : quelques clips « en attendant », pas plus
        n = db.execute("select count(*) from file where genre = 'clip' and ajoute_le >= ?",
                       (_iso(debut_du_jour()),)).fetchone()[0]
        maxi = min(maxi, max(0, R["avant_sortie_clips_max_jour"] - n))
        if maxi <= 0:
            log("pilote : quota de clips d'avant-sortie atteint aujourd'hui")
            return 0
    reste = R["analyses_max_par_jour"] - _compteur_analyses()
    if reste <= 0:
        log(f"pilote : plafond de {R['analyses_max_par_jour']} analyses atteint aujourd'hui")
        return 0
    fraicheur = R.get("fraicheur_avant_sortie_heures", R["fraicheur_max_heures"]) if attente else R["fraicheur_max_heures"]
    limite = _iso(maintenant() - dt.timedelta(hours=fraicheur - 2))
    noirs = refuses()
    # diversité : pas plus de deux clips prêts du même streamer dans la file
    charges = {r["streamer"]: r["n"] for r in db.execute(
        "select streamer, count(*) n from file where statut = 'pret' and genre = 'clip' group by streamer")}
    q = (f"select * from clips where statut in ('nouveau', 'telecharge', 'attente') and cree_le >= ? "
         f"and jeu in ({','.join('?' * len(jeux))}) order by score desc limit 60")
    faits, vus, essais = 0, set(), 0
    plafond_passage = min(reste, 2 * maxi)  # chaque essai = téléchargement, whisper et une analyse Claude
    for r in db.execute(q, [limite, *jeux]).fetchall():
        if faits >= maxi or essais >= plafond_passage:
            break
        s = r["streamer"]
        if s in noirs or s in vus or charges.get(s, 0) >= 2:
            continue
        vus.add(s)
        essais += 1
        res = preparer_un(db, dict(r), c, R)
        if res == "pause":
            break
        if res:
            faits += 1
    log(f"pilote : {faits} clip(s) ajouté(s) à la file ({essais} essayé(s))")
    return faits


# ---------- cartes Infos ----------

def cartes_du_jour():
    c = config()
    R = reglages(c)
    db = base()
    p = paris()
    jour = p.date()
    if jour >= infos.SORTIE_JEU:
        return 0
    n = (infos.SORTIE_JEU - jour).days
    fid = f"carte:compte-a-rebours:{jour.isoformat()}"
    if db.execute("select 1 from file where id = ?", (fid,)).fetchone():
        return 0
    d = c["dossier_sortie"] / "A PUBLIER" / jour.isoformat() / "INFOS"
    pngs = infos.fabriquer("compte-a-rebours", str(d))
    fil = next(x for x in pngs if x.stem.endswith("-fil"))
    # TikTok refuse le PNG pour les photos : JPEG qualité haute
    jpg = fil.with_suffix(".jpg")
    en_jpeg(fil, jpg)
    hh, mm = (int(x) for x in R["carte_heure"].split(":"))
    pas_avant = p.replace(hour=hh, minute=mm, second=0, microsecond=0)
    fin = p.replace(hour=23, minute=59, second=0, microsecond=0)
    db.execute("""insert into file (id, genre, ref, fichier, legende, streamer, accroche, interet, inedit, score,
                  pas_avant, expire_le, statut, ajoute_le) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (fid, "carte", f"j-{n}", str(jpg), fil.with_suffix(".txt").read_text(encoding="utf-8"), None,
                f"J-{n} avant GTA 6", 10, 0, 0, _iso(pas_avant), _iso(fin), "pret", _iso(maintenant())))
    db.commit()
    log(f"pilote : carte J-{n} prête pour {R['carte_heure']}")
    return 1


def cartes_programmees():
    """Cartes Infos écrites à l'avance dans cartes.json (titre, texte, source obligatoire, jour, heure, légende)."""
    from .commun import RACINE
    f = RACINE / "cartes.json"
    if not f.exists():
        return 0
    c = config()
    db = base()
    p = paris()
    jour = p.date().isoformat()
    n = 0
    try:
        liste = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        log(f"pilote : cartes.json illisible ({e})")
        return 0
    if not isinstance(liste, list):
        log("pilote : cartes.json n'est pas une liste")
        return 0
    for k in liste:
        if not isinstance(k, dict) or k.get("jour") != jour:
            continue
        if not k.get("cle"):
            log("pilote : carte sans clé dans cartes.json, ignorée")
            continue
        fid = f"carte:info:{k['cle']}"
        if db.execute("select 1 from file where id = ?", (fid,)).fetchone():
            continue
        try:
            hh, mm = (int(x) for x in k.get("heure", "12:30").split(":"))
            pas_avant = p.replace(hour=hh, minute=mm, second=0, microsecond=0)
            d = c["dossier_sortie"] / "A PUBLIER" / jour / "INFOS"
            pngs = infos.fabriquer("info", str(d), titre=k["titre"], texte=k["texte"], source=k["source"],
                                   fort=k.get("fort"), cle=k["cle"], legende=k["legende"])
            fil = next(x for x in pngs if x.stem.endswith("-fil"))
            jpg = fil.with_suffix(".jpg")
            en_jpeg(fil, jpg)
        except Exception as e:
            log(f"pilote : carte {k.get('cle')} en échec ({type(e).__name__}: {e})")
            continue
        fin = p.replace(hour=23, minute=59, second=0, microsecond=0)
        db.execute("""insert into file (id, genre, ref, fichier, legende, streamer, accroche, interet, inedit, score,
                      pas_avant, expire_le, statut, ajoute_le) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (fid, "carte", k["cle"], str(jpg), k["legende"], None, k["titre"], 10, 0, 0, _iso(pas_avant),
                    _iso(fin), "pret", _iso(maintenant())))
        db.commit()
        log(f"pilote : carte info « {k['titre']} » prête pour {k.get('heure', '12:30')}")
        n += 1
    return n


# ---------- publication ----------

def fiche_avec_cam(streamer, a):
    """Cam repérée par l'analyse du clip (consigne du 16/11 : regarder à chaque clip s'il y a une cam).
    Zones rendues sur une image de 960 px de large, notées ici dans le repère 1920x1080 du montage.
    Sans cam repérée ou zone incohérente : la fiche du streamer (streamers.json), sinon le fond flou."""
    fiche = dict(monter.fiche_streamer(streamer))
    z = [2 * int(v) for v in (a.get("cam_zone") or [])]
    if a.get("voie") in ("api", "cli") and a.get("cam") is False:
        fiche["cam"] = None  # l'analyse ne voit pas de cam sur ce clip : fond flou, sans risque
        return fiche
    if not a.get("cam") or len(z) != 4:
        return fiche
    x, y, w, h = z
    if not (240 <= w <= 1100 and 150 <= h <= 720 and 0.8 <= w / h <= 2.6 and x >= -4 and y >= -4
            and x + w <= 1924 and y + h <= 1084):
        log(f"pilote : zone de cam incohérente pour {streamer} ({z}), fiche gardée")
        return fiche
    x, y = max(0, x), max(0, y)
    w, h = min(w, 1920 - x), min(h, 1080 - y)
    m = fiche.get("cam")
    if m:  # cam notée à la main au même endroit : plus précise (pas d'étiquette ni de cadre), on la garde
        iw = max(0, min(x + w, m[0] + m[2]) - max(x, m[0]))
        ih = max(0, min(y + h, m[1] + m[3]) - max(y, m[1]))
        if iw * ih / float(w * h + m[2] * m[3] - iw * ih) >= 0.5:
            return fiche
    b = [2 * int(v) for v in (a.get("cam_bandeau") or [])]
    if len(b) == 4 and b[0] <= x and b[1] <= y and b[0] + b[2] >= x + w and b[1] + b[3] >= y + h and b[2] <= 1300:
        exclure = [max(0, b[0] - 8), max(0, b[1] - 8), b[2] + 16, b[3] + 16]
    else:
        exclure = [max(0, x - 16), max(0, y - 16), w + 32, h + 32]
    # 4 px rognés sur chaque bord : jamais de liseré du cadre ni du jeu autour de la cam
    fiche.update(cam=[x + 4, y + 4, w - 8, h - 8], exclure=exclure)
    return fiche


def priorite(r, t=None):
    t = t or maintenant()
    if r["genre"] == "carte":
        # les clips d'abord (ce sont eux qui font les vues) ; une carte passe devant 90 min après son heure prévue
        prevu = _date(r["pas_avant"]) if r["pas_avant"] else None
        return 1000.0 if (prevu and (t - prevu).total_seconds() > 90 * 60) else 15.0
    cree = _date(r["cree_le"]) if r["cree_le"] else None
    age_h = (t - cree).total_seconds() / 3600 if cree else 12
    # après la sortie, les découvertes du jeu (lieu, véhicule, mécanique) passent devant : consigne du 16/11
    from .radar import sortie_du_jeu
    poids_inedit = 30 if t >= sortie_du_jeu(config()) else 15
    return (r["interet"] or 0) * 10 + poids_inedit * (r["inedit"] or 0) + 8 * math.log10(1 + max(0, r["score"] or 0)) - min(2 * age_h, 30)


def expirer(db):
    t = _iso(maintenant())
    for r in db.execute("select * from file where statut = 'pret' and expire_le < ?", (t,)).fetchall():
        deja = db.execute("select 1 from publications where clip_id = ? and statut in ('publie', 'en_cours')",
                          (r["id"],)).fetchone()
        if not deja:  # publié nulle part : vidéo à la corbeille ; publié quelque part : gardée (retrait éventuel)
            a_la_corbeille(r["fichier"], Path(r["fichier"]).with_suffix(".txt"))
        db.execute("update file set statut = ? where id = ?", ("publie" if deja else "expire", r["id"]))
        if r["genre"] == "clip":
            maj(db, r["ref"], statut="publie" if deja else "expire",
                **({} if deja else {"raison": "trop vieux avant d'avoir trouvé sa place"}))
    db.commit()


def suivre_en_cours(db, diff):
    """Posts que Zernio publiait encore au passage précédent : on relit leur état."""
    if diff.simulation:
        return
    for r in db.execute("select rowid, * from publications where statut = 'en_cours' and post_id is not null "
                        "and post_id != ''").fetchall():
        try:
            statut, detail = diff.relire(r["post_id"])
        except Exception as e:
            log(f"pilote : état du post {r['post_id']} illisible ({e})")
            continue
        if statut != "en_cours":
            db.execute("update publications set statut = ?, reponse = ? where rowid = ?", (statut, detail, r["rowid"]))
            log(f"pilote : {r['plateforme']} {r['clip_id']} passe en {statut} ({detail})")
            if statut == "echec":  # finalement refusé : l'élément redevient disponible s'il n'a pas expiré
                db.execute("update file set statut = 'pret' where id = ? and statut = 'publie' and expire_le > ?",
                           (r["clip_id"], _iso(maintenant())))
    db.commit()


def _fin_de_fenetre(R, p):
    """Heure de fin des heures actives qui suit p (01:30 le lendemain si on est le soir)."""
    hh, mm = (int(x) for x in R["heures_actives"][1].split(":"))
    fin = p.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return fin if fin > p else fin + dt.timedelta(days=1)


def publier_du():
    c = config()
    R = reglages(c)
    db = base()
    expirer(db)
    n = 0
    with Diffusion() as diff:
        suivre_en_cours(db, diff)
        if not dans_les_heures(R):
            return 0
        comptes = ("publie", "en_cours", "simule") if diff.simulation else ("publie", "en_cours")
        marques = ",".join("?" * len(comptes))
        actives = diff.actives()
        noirs = refuses()
        t = maintenant()
        p = paris()
        jour = _iso(debut_du_jour())
        for plat in actives:
            regle = R["plateformes"][plat]
            faits = db.execute(f"select count(*) from publications where plateforme = ? and statut in ({marques}) "
                               "and le >= ?", (plat, *comptes, jour)).fetchone()[0]
            glissant = db.execute(f"select count(*) from publications where plateforme = ? and statut in ({marques}) "
                                  "and le >= ?", (plat, *comptes, _iso(t - dt.timedelta(hours=24)))).fetchone()[0]
            if max(faits, glissant) >= regle["max_jour"]:  # journée de 5 h à 5 h et 24 h glissantes (limite TikTok)
                continue
            # écart : au moins l'écart minimum, et assez pour étaler les posts restants jusqu'à la fin de la soirée
            reste_min = minutes_restantes(R, p)
            ecart = max(regle["ecart_min_minutes"], 0.8 * reste_min / (regle["max_jour"] - faits))
            dernier = db.execute(f"select le, clip_id from publications where plateforme = ? and statut in ({marques}) "
                                 "order by le desc limit 1", (plat, *comptes)).fetchone()
            if dernier and (t - _date(dernier["le"])).total_seconds() < ecart * 60:
                continue
            cands = db.execute(f"""select f.*, c.cree_le from file f left join clips c on c.id = f.ref
                                  where f.statut = 'pret' and (f.pas_avant is null or f.pas_avant <= ?)
                                  and (f.expire_le is null or f.expire_le > ?)
                                  and not exists (select 1 from publications p where p.clip_id = f.id
                                                  and p.plateforme = ? and p.statut in ({marques}))
                                  and (select count(*) from publications p where p.clip_id = f.id
                                       and p.plateforme = ? and p.statut = 'echec') < 2""",
                               (_iso(t), _iso(t), plat, *comptes, plat)).fetchall()
            cands = [r for r in cands if r["streamer"] not in noirs]  # retrait demandé depuis la mise en file
            recents_echecs = {x["clip_id"] for x in db.execute(
                "select clip_id from publications where plateforme = ? and statut = 'echec' and le >= ?",
                (plat, _iso(t - dt.timedelta(minutes=30))))}
            cands = [r for r in cands if r["id"] not in recents_echecs]  # une panne ne brûle pas un clip en un passage
            if not cands:
                continue
            # diversité : pas deux fois de suite le même streamer sur une plateforme, s'il y a un autre choix
            dernier_streamer = None
            if dernier:
                x = db.execute("select streamer from file where id = ?", (dernier["clip_id"],)).fetchone()
                dernier_streamer = x["streamer"] if x else None
            autres = [r for r in cands if r["streamer"] is None or r["streamer"] != dernier_streamer] or cands
            choix = max(autres, key=lambda r: priorite(r, t))
            statut, detail, pid = diff.publier(plat, choix["fichier"], choix["legende"], ident=choix["id"])
            db.execute("insert into publications (clip_id, plateforme, statut, reponse, le, post_id) values (?,?,?,?,?,?)",
                       (choix["id"], plat, statut, detail, _iso(maintenant()), pid))
            db.commit()
            log(f"pilote : {plat} {statut} {Path(choix['fichier']).name} ({detail})")
            n += statut in comptes
        # un élément sort de la file quand chaque plateforme active l'a pris, ou l'a refusé deux fois
        for r in db.execute("select * from file where statut = 'pret'").fetchall():
            restantes = [pl for pl in actives
                         if not db.execute(f"select 1 from publications where clip_id = ? and plateforme = ? "
                                           f"and statut in ({marques})", (r["id"], pl, *comptes)).fetchone()
                         and db.execute("select count(*) from publications where clip_id = ? and plateforme = ? "
                                        "and statut = 'echec'", (r["id"], pl)).fetchone()[0] < 2]
            if not restantes:
                pris = db.execute(f"select 1 from publications where clip_id = ? and statut in ({marques})",
                                  (r["id"], *comptes)).fetchone()
                db.execute("update file set statut = ? where id = ?", ("publie" if pris else "echec", r["id"]))
                if r["genre"] == "clip":
                    maj(db, r["ref"], statut="publie" if pris else "erreur",
                        **({} if pris else {"raison": "refusé deux fois par chaque plateforme"}))
        db.commit()
    return n


def menage():
    """Fichiers de travail de l'outil (sources Twitch téléchargées, transcriptions, sous-titres) de plus de 2 jours :
    supprimés, ils se retéléchargent. Dossiers A PUBLIER de plus de 7 jours : à la corbeille."""
    limite = time.time() - 2 * 86400
    from .commun import SOURCES, TRAVAIL
    for d in (SOURCES, TRAVAIL):
        if d.exists():
            for f in d.iterdir():
                if (f.is_file() and f.suffix in (".mp4", ".json", ".ass", ".html") and f.name != "telegram_offset.json"
                        and f.stat().st_mtime < limite):
                    f.unlink()
    racine = config()["dossier_sortie"] / "A PUBLIER"
    if racine.exists():
        borne = (paris().date() - dt.timedelta(days=7)).isoformat()
        for d in racine.iterdir():
            if d.is_dir() and len(d.name) == 10 and d.name < borne:
                a_la_corbeille(d)


# ---------- point pour le propriétaire ----------

def point():
    c = config()
    R = reglages(c)
    db = base()
    depuis = _iso(maintenant() - dt.timedelta(hours=24))
    p = paris()
    vus = db.execute("select count(*) from clips where vu_le >= ?", (depuis,)).fetchone()[0]
    raisons = db.execute("select raison, count(*) n from clips where statut in ('rejete', 'attente') and maj >= ? "
                         "group by raison order by n desc limit 4", (depuis,)).fetchall()
    prets = db.execute("select count(*) from file where statut = 'pret'").fetchone()[0]
    pubs = db.execute("select plateforme, statut, count(*) n from publications where le >= ? group by plateforme, statut",
                      (depuis,)).fetchall()
    erreurs = db.execute("select count(*) from clips where statut = 'erreur' and maj >= ?", (depuis,)).fetchone()[0]
    mode = "SIMULATION (comptes pas encore branchés)" if R["publier_avec"] == "aucun" else f"réelle par {R['publier_avec']}"
    lignes = [f"6 Étoiles, point du {p:%d/%m/%Y à %H h %M}", f"Publication : {mode}", "",
              f"Dernières 24 h : {vus} clip(s) repéré(s), {prets} prêt(s) dans la file, {erreurs} erreur(s)."]
    noms = {"publie": "publié(s)", "simule": "simulé(s)", "en_cours": "en cours chez Zernio", "echec": "échec(s)"}
    for r in pubs:
        lignes.append(f"{r['plateforme']} : {r['n']} {noms.get(r['statut'], r['statut'])}")
    if raisons:
        lignes.append("Écartés, raisons principales : " + " ; ".join(f"{r['raison']} ({r['n']})" for r in raisons))
    prochains = db.execute("""select f.*, c.cree_le from file f left join clips c on c.id = f.ref
                              where f.statut = 'pret'""").fetchall()
    for r in sorted(prochains, key=priorite, reverse=True)[:3]:
        lignes.append(f"Prochain : {r['accroche']} ({r['streamer'] or 'carte'})")
    (c["dossier_sortie"]).mkdir(parents=True, exist_ok=True)
    (c["dossier_sortie"] / "POINT.txt").write_text("\n".join(lignes) + "\n", encoding="utf-8")


def sante():
    """État du pilote, pour un contrôle rapide."""
    db = base()
    lignes = []
    try:
        dernier = next((l for l in reversed(JOURNAL.read_text(encoding="utf-8").splitlines()) if "passage terminé" in l), None)
    except FileNotFoundError:
        dernier = None
    lignes.append(f"dernier passage complet : {dernier[:16] if dernier else 'aucun'}")
    try:
        r = subprocess.run(["launchctl", "print", f"gui/{subprocess.run(['id', '-u'], capture_output=True, text=True).stdout.strip()}/pilote.gta6clips"],
                           capture_output=True, text=True)
        lignes.append("launchd : " + ("chargé" if r.returncode == 0 else "PAS chargé"))
    except OSError:
        lignes.append("launchd : absent (machine sans launchd)")
    for x in db.execute("select statut, count(*) n from file group by statut"):
        lignes.append(f"file {x['statut']} : {x['n']}")
    jour = _iso(debut_du_jour())
    for x in db.execute("select plateforme, statut, count(*) n from publications where le >= ? group by 1, 2", (jour,)):
        lignes.append(f"aujourd'hui {x['plateforme']} {x['statut']} : {x['n']}")
    lignes.append(f"analyses aujourd'hui : {_compteur_analyses()}")
    return "\n".join(lignes)
