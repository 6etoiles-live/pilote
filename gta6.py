#!/usr/bin/env python3
"""Clips GTA 6 des streamers FR : radar, montage vertical, validation, file de publication.

  gta6.py radar [--jeu "Grand Theft Auto V"]     repère les nouveaux clips
  gta6.py preparer [--max 6]                      monte les meilleurs clips et les envoie à valider
  gta6.py clip <url> [--accroche "..."] [--mot-fort "..."]   monte un clip précis (essai, commande à la main)
  gta6.py valider                                 relève les ✅ / ❌ touchés sur Telegram
  gta6.py tourner [--jeu ...]                     un passage complet (lancé par launchd toutes les 15 min) :
                                                  en mode « auto », radar, préparation, cartes, publication, point
  gta6.py etat                                    résumé de la base
  gta6.py sante                                   état du pilote (dernier passage, launchd, file, publications)
  gta6.py file                                    éléments prêts à publier, dans l'ordre de passage
  gta6.py diffusion                               relève les comptes branchés sur Zernio (TikTok, Instagram)
  gta6.py retirer <login>                         un streamer demande le retrait : liste noire
  gta6.py infos compte-a-rebours|sortie|live      cartes Infos (jeu et streamers) dans A VALIDER/INFOS
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gta6clips import analyse, infos, monter, pilote, publier, radar  # noqa: E402
from gta6clips.commun import Verrou, base, config, log, maj, maintenant, streamers  # noqa: E402


def preparer_un(db, clip, accroche_forcee=None, dossier="A VALIDER", mot_fort_force=None):
    c = config()
    cid = clip["id"]
    try:
        source = monter.telecharger(clip["url"], cid)
        maj(db, cid, statut="telecharge", source=str(source))
        mots = monter.transcrire(source, cid)
        texte = monter.texte_dit(mots)
        a = analyse.analyser(source, clip["titre"], clip["streamer"], texte, clip.get("jeu") or None)
        if a.get("spoiler") or a.get("image") in ("cinematique", "menu_ou_carte"):
            maj(db, cid, statut="rejete", raison=f"spoiler ou cinématique : {a.get('raison', '')}")
            log(f"{cid} écarté : {a.get('raison')}")
            return None
        accroche = (accroche_forcee or "").strip() or a["accroche"]
        legende = analyse.legende_complete(c, a["legende"], clip["streamer"], clip.get("jeu"))
        sortie_dir = c["dossier_sortie"] / dossier
        sortie_dir.mkdir(parents=True, exist_ok=True)
        sortie = sortie_dir / f"{clip['streamer']}-{cid[:24]}.mp4"
        mot_fort = mot_fort_force if mot_fort_force is not None else (None if accroche_forcee else a.get("mot_fort"))
        monter.rendre(source, sortie, mots, accroche, clip["streamer"], __import__("gta6clips.pilote", fromlist=["x"]).fiche_avec_cam(clip["streamer"], a),
                      mot_fort=mot_fort)
        sortie.with_suffix(".txt").write_text(legende, encoding="utf-8")
        note = "" if a.get("interet", -1) < 0 else f" (intérêt {a['interet']}/10)"
        if a.get("musique_sous_licence"):
            note += " ⚠️ musique sous licence"
        maj(db, cid, statut="essai" if dossier == "ESSAIS" else "a_valider", sortie=str(sortie), accroche=accroche, legende=legende,
            raison=(a.get("raison", "") + note).strip())
        return dict(db.execute("select * from clips where id = ?", (cid,)).fetchone())
    except Exception as e:
        maj(db, cid, statut="erreur", raison=str(e)[:300])
        log(f"{cid} en erreur : {e}")
        return None


def envoyer(db, clip):
    try:
        if publier.envoyer_pour_validation(clip):
            return True
    except Exception as e:
        log(f"{clip['id']} : envoi Telegram raté, retenté au prochain passage ({e})")
        maj(db, clip["id"], statut="a_envoyer")
    return False


def cmd_preparer(maxi):
    db = base()
    c = config()
    # d'abord les clips montés dont l'envoi sur Telegram avait raté
    for r in db.execute("select * from clips where statut = 'a_envoyer'").fetchall():
        maj(db, r["id"], statut="a_valider")
        envoyer(db, dict(r, statut="a_valider"))
    age_max = c.get("age_max_heures", 36)
    limite = (maintenant() - dt.timedelta(hours=age_max)).isoformat()
    # « telecharge » : un passage interrompu en plein montage, repris au suivant
    requete = "select * from clips where statut in ('nouveau', 'telecharge') and cree_le >= ?"
    params = [limite]
    # liste noire toujours exclue, même si la fiche a été changée à la main sans passer par « retirer »
    noirs = [l for l, s in streamers().items() if s.get("noir") or s.get("accord") == "non"]
    if noirs:
        requete += f" and streamer not in ({','.join('?' * len(noirs))})"
        params += noirs
    if c.get("publication", {}).get("seulement_avec_accord", True):
        # on ne monte que les streamers qui ont dit oui (filtré avant le tri, pour n'en oublier aucun)
        ok = [l for l, s in streamers().items() if s.get("accord") in ("oui", "programme")]
        marques = ",".join("?" * len(ok)) or "''"
        requete += f" and streamer in ({marques})"
        params += ok
    rangs = db.execute(requete + " order by score desc limit ?", params + [maxi * 3]).fetchall()
    faits = 0
    for r in rangs:
        if faits >= maxi:
            break
        clip = preparer_un(db, dict(r))
        if clip:
            envoyer(db, clip)
            faits += 1
    log(f"préparer : {faits} clips prêts à valider")


def cmd_clip(url, accroche, mot_fort=None):
    import json
    import subprocess
    r = subprocess.run(["yt-dlp", "-J", "--skip-download", url], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Clip introuvable ou adresse invalide : {url}\n{r.stderr.strip()[-300:]}")
    infos = json.loads(r.stdout)
    cree = dt.datetime.fromtimestamp(infos.get("timestamp") or 0, dt.timezone.utc).isoformat()
    slug = url.rstrip("/").split("/")[-1].split("?")[0]
    db = base()
    if not db.execute("select 1 from clips where id = ?", (slug,)).fetchone():
        db.execute("insert into clips (id, url, streamer, titre, jeu, vues, duree, cree_le, statut) values (?,?,?,?,?,?,?,?,?)",
                   (slug, url, (infos.get("channel") or infos.get("creator") or "streamer").lower(), infos.get("title"),
                    (infos.get("categories") or [""])[0], infos.get("view_count") or 0, infos.get("duration") or 0,
                    cree, "essai"))
        db.commit()
    clip = dict(db.execute("select * from clips where id = ?", (slug,)).fetchone())
    r = preparer_un(db, clip, accroche_forcee=accroche, dossier="ESSAIS", mot_fort_force=mot_fort)
    if r:
        print(r["sortie"])
        print(r["legende"])


def cmd_retirer(login):
    """Un streamer demande le retrait : il passe en liste noire, ses clips en attente sont jetés,
    et on liste ceux déjà publiés, plateforme par plateforme, à supprimer à la main sous 24 h."""
    import re
    import time
    v = Verrou()  # attend la fin d'un passage en cours : un clip du streamer pourrait être en plein montage
    for _ in range(120):
        if v.__enter__():
            break
        time.sleep(10)
    else:
        sys.exit("retrait : un passage du pilote ne se termine pas, réessaie dans quelques minutes")
    try:
        _retirer(login, re)
    finally:
        v.__exit__()


def _retirer(login, re):
    login = login.strip().lstrip("@").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,25}", login):
        sys.exit(f"Pseudo Twitch invalide : « {login} » (lettres, chiffres et _ seulement, comme dans twitch.tv/pseudo)")
    p = Path(__file__).resolve().parent / "streamers.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    fiche = next((s for s in d["streamers"] if s["login"].lower() == login), None)
    if not fiche:
        # inconnu de la liste : ajouté quand même, pour que le radar ne prenne jamais ses clips
        connu = base().execute("select 1 from clips where streamer = ?", (login,)).fetchone()
        print(f"{login} n'était pas dans streamers.json{'' if connu else ' ni dans la base'} : ajouté en liste noire par précaution")
        fiche = {"login": login, "cam": None}
        d["streamers"].append(fiche)
    fiche["noir"], fiche["accord"] = True, "non"
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    db = base()
    # montés mais pas encore en ligne (A VALIDER, A PUBLIER) : vidéo et légende à la corbeille
    for r in db.execute("select * from clips where streamer = ? and statut in ('a_valider', 'a_envoyer', 'valide', 'pret') "
                        "and sortie is not null", (login,)).fetchall():
        publier.jeter(dict(r))
    db.execute("update file set statut = 'jete' where streamer = ? and statut = 'pret'", (login,))
    db.execute("update clips set statut = 'rejete', raison = 'retrait demandé par le streamer' "
               "where streamer = ? and statut in ('nouveau', 'telecharge', 'a_valider', 'a_envoyer', 'valide', 'pret', "
               "'attente')", (login,))
    db.commit()
    publies = db.execute("""select p.plateforme, p.statut, p.reponse, f.fichier from publications p
                            join file f on f.id = p.clip_id where f.streamer = ? and p.statut in ('publie', 'en_cours')""",
                         (login,)).fetchall()
    log(f"retrait : {login} en liste noire, clips en attente jetés, {len(publies)} post(s) en ligne à supprimer à la main")
    for r in publies:
        print(f"  à supprimer sur {r['plateforme']} ({r['statut']}) : {r['reponse']}  [{Path(r['fichier']).name}]")


def cmd_etat():
    db = base()
    for r in db.execute("select statut, count(*) n from clips group by statut order by n desc"):
        print(f"{r['statut']:<12} {r['n']}")


def main():
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("radar"); s.add_argument("--jeu")
    s = sp.add_parser("preparer"); s.add_argument("--max", type=int, default=6)
    s = sp.add_parser("clip"); s.add_argument("url"); s.add_argument("--accroche"); s.add_argument("--mot-fort")
    sp.add_parser("valider")
    s = sp.add_parser("tourner"); s.add_argument("--jeu"); s.add_argument("--max", type=int, default=None)
    sp.add_parser("etat")
    sp.add_parser("sante")
    sp.add_parser("file")
    sp.add_parser("diffusion")
    s = sp.add_parser("retirer"); s.add_argument("login")
    s = sp.add_parser("infos"); s.add_argument("genre", choices=["compte-a-rebours", "sortie", "live"])
    s.add_argument("--dossier"); s.add_argument("--tous", action="store_true", help="live : tous les jeux (essai)")
    a = p.parse_args()
    if a.cmd == "radar":
        radar.tourner(a.jeu)
    elif a.cmd == "preparer":
        cmd_preparer(a.max)
    elif a.cmd == "clip":
        cmd_clip(a.url, a.accroche, a.mot_fort)
    elif a.cmd == "valider":
        print(publier.relever_reponses(), "réponses relevées")
    elif a.cmd == "tourner":
        cmd_tourner(a)
    elif a.cmd == "etat":
        cmd_etat()
    elif a.cmd == "sante":
        print(pilote.sante())
    elif a.cmd == "file":
        cmd_file()
    elif a.cmd == "diffusion":
        cmd_diffusion()
    elif a.cmd == "retirer":
        cmd_retirer(a.login)
    elif a.cmd == "infos":
        try:
            cmd_infos(a)
        except ValueError as e:  # ex. : le jeu est sorti, plus de compte à rebours
            sys.exit(f"infos : {e}")


def cmd_tourner(a):
    with Verrou() as libre:
        if not libre:
            log("tourner : un passage est déjà en cours, celui-ci s'arrête")
            return
        if config().get("publication", {}).get("mode") == "auto":
            etapes = (("publier d'abord", pilote.publier_du),  # ce qui est prêt part sans attendre les analyses
                      ("radar", lambda: radar.tourner(a.jeu)), ("préparer", lambda: pilote.preparer(a.max)),
                      ("cartes", pilote.cartes_du_jour), ("cartes infos", pilote.cartes_programmees), ("publier", pilote.publier_du), ("ménage", pilote.menage),
                      ("point", pilote.point))
        else:
            etapes = (("validation", publier.relever_reponses), ("radar", lambda: radar.tourner(a.jeu)),
                      ("préparer", lambda: cmd_preparer(a.max or 6)))
        # chaque étape est isolée : une panne de Twitch ou de l'analyse n'empêche pas les autres
        for nom, etape in etapes:
            try:
                etape()
            except Exception as e:
                log(f"tourner : étape {nom} en échec ({type(e).__name__}: {e})")
        log("tourner : passage terminé")


def cmd_file():
    db = base()
    rangs = db.execute("""select f.*, c.cree_le from file f left join clips c on c.id = f.ref
                          where f.statut = 'pret'""").fetchall()
    for r in sorted(rangs, key=pilote.priorite, reverse=True):
        print(f"{pilote.priorite(r):6.1f}  {r['genre']:<5} {(r['streamer'] or '-'):<16} {r['accroche']}  "
              f"(après {(r['pas_avant'] or 'tout de suite')[:16]}, expire {r['expire_le'][:16]})")
    if not rangs:
        print("file vide")


def cmd_diffusion():
    """Relève les comptes branchés sur Zernio et écrit leurs identifiants dans config.local.json."""
    from gta6clips import diffuseurs
    c = config()
    cle = c["secret"].get("zernio_api_key")
    if not cle:
        sys.exit("zernio_api_key absent de config.local.json")
    trouves, retenus = diffuseurs.ecrire_comptes(cle)
    for plat, liste in trouves.items():
        print(plat, ", ".join(f"{pseudo} ({i})" for i, pseudo in liste))
    print("retenus pour la page :", json.dumps(retenus, ensure_ascii=False))


def cmd_infos(a):
    if a.genre == "sortie":
        infos.fabriquer("info", a.dossier, cle="sortie", titre="GTA 6 sort le 19 novembre.", fort="19 novembre",
                        texte="Le jeu arrive le jeudi 19 novembre 2026 sur PS5 et Xbox Series X|S. "
                              "Le préchargement est déjà ouvert. Aucune version PC n'est annoncée au 11/11/2026.",
                        source="rockstargames.com/VI, relevé le 11/11/2026",
                        legende="GTA 6 sort le jeudi 19 novembre 2026 sur PS5 et Xbox Series X|S, le préchargement est ouvert ⭐\n\n"
                                "#gta6 #gta6fr #leonida #vicecity #gaming")
    elif a.genre == "live":
        infos.fabriquer("live", a.dossier, filtre_jeu=not a.tous)
    else:
        infos.fabriquer(a.genre, a.dossier)


if __name__ == "__main__":
    main()
