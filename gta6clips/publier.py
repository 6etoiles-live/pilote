"""Validation sur Telegram (un bot dédié, jamais celui de LinkedIn) et file de publication.

Chaque clip prêt part sur Telegram en aperçu léger avec deux boutons : ✅ Publier / ❌ Jeter.
Un clip validé est rangé dans « A PUBLIER/<date>/ » (vidéo pleine qualité + légende .txt), d'où l'outil de
publication du compte (Buffer ou Zernio, compte séparé de celui du propriétaire) le prend aux heures du config.json.
"""
import json
import shutil
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .commun import corbeille, TRAVAIL, base, config, lancer, log, maj, maintenant


def _tg(c, methode, champs=None, fichier=None):
    jeton = c["secret"].get("telegram_bot_token")
    if not jeton:
        raise RuntimeError("telegram_bot_token absent de config.local.json")
    url = f"https://api.telegram.org/bot{jeton}/{methode}"
    if fichier is None:
        data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                       for k, v in (champs or {}).items()}).encode()
        req = urllib.request.Request(url, data=data)
    else:  # envoi multipart d'une vidéo
        limite = uuid.uuid4().hex
        corps = b""
        for k, v in (champs or {}).items():
            v = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            corps += f"--{limite}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        nom_champ, chemin = fichier
        corps += (f"--{limite}\r\nContent-Disposition: form-data; name=\"{nom_champ}\"; filename=\"{Path(chemin).name}\"\r\n"
                  "Content-Type: video/mp4\r\n\r\n").encode() + Path(chemin).read_bytes() + f"\r\n--{limite}--\r\n".encode()
        req = urllib.request.Request(url, data=corps, headers={"Content-Type": f"multipart/form-data; boundary={limite}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    if not d.get("ok"):
        raise RuntimeError(f"Telegram {methode} : {d}")
    return d["result"]


def apercu(sortie):
    """Version légère (< 50 Mo, limite des bots Telegram) pour juger sur le téléphone."""
    p = TRAVAIL / (Path(sortie).stem + "-apercu.mp4")
    lancer(["ffmpeg", "-y", "-v", "error", "-i", str(sortie), "-vf", "scale=720:-2", "-c:v", "libx264",
            "-preset", "veryfast", "-b:v", "3M", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(p)])
    return p


def envoyer_pour_validation(clip):
    c = config()
    if not c["secret"].get("telegram_bot_token"):
        log(f"validation : pas de bot Telegram, {clip['id']} attend dans A VALIDER")
        return False
    texte = (f"🎮 {clip['streamer']} · {clip['vues']} vues · {clip['duree']:.0f} s\n"
             f"Accroche : {clip['accroche']}\n\n{clip['legende']}\n\n{clip['url']}")
    clavier = {"inline_keyboard": [[{"text": "✅ Publier", "callback_data": f"ok:{clip['id']}"[:64]},
                                    {"text": "❌ Jeter", "callback_data": f"non:{clip['id']}"[:64]}]]}
    _tg(c, "sendVideo", {"chat_id": c["secret"]["telegram_chat_id"], "caption": texte[:1000],
                         "reply_markup": clavier, "supports_streaming": "true"}, ("video", apercu(clip["sortie"])))
    return True


def relever_reponses():
    """Lit les boutons touchés sur Telegram depuis le dernier passage."""
    c = config()
    if not c["secret"].get("telegram_bot_token"):
        return 0
    db = base()
    etat_f = TRAVAIL / "telegram_offset.json"
    offset = json.loads(etat_f.read_text()).get("offset", 0) if etat_f.exists() else 0
    n = 0
    def repondre(cq_id, texte):
        # un bouton expire vite côté Telegram : une réponse refusée ne doit jamais bloquer la boucle
        try:
            _tg(c, "answerCallbackQuery", {"callback_query_id": cq_id, "text": texte})
        except Exception as e:
            log(f"telegram : réponse au bouton impossible ({e})")

    for u in _tg(c, "getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]}):
        offset = u["update_id"] + 1
        etat_f.write_text(json.dumps({"offset": offset}))  # enregistré à chaque fois : un plantage ne rejoue rien
        cq = u.get("callback_query")
        if not cq or str(cq.get("message", {}).get("chat", {}).get("id")) != str(c["secret"]["telegram_chat_id"]):
            continue
        action, _, cid = (cq.get("data") or "").partition(":")
        r = db.execute("select * from clips where id like ?", (cid + "%",)).fetchone()
        if not r or r["statut"] != "a_valider":
            repondre(cq["id"], "Déjà traité.")
            continue
        try:
            if action == "ok":
                dest = ranger_a_publier(dict(r))
                maj(db, r["id"], statut="valide", sortie=str(dest))
                repondre(cq["id"], "✅ Dans la file de publication")
            else:
                jeter(dict(r))
                maj(db, r["id"], statut="rejete", raison="jeté par le propriétaire")
                repondre(cq["id"], "❌ Jeté")
            n += 1
        except Exception as e:
            log(f"telegram : {r['id']} non traité ({e})")
    return n


def jeter(clip):
    """Clip refusé : vidéo et légende vont à la corbeille (jamais de suppression définitive)."""
    for f in (Path(clip["sortie"]), Path(clip["sortie"]).with_suffix(".txt")):
        if f.exists():
            corbeille(f)


def ranger_a_publier(clip):
    c = config()
    jour = maintenant().astimezone().strftime("%Y-%m-%d")
    d = c["dossier_sortie"] / "A PUBLIER" / jour
    d.mkdir(parents=True, exist_ok=True)
    dest = d / Path(clip["sortie"]).name
    shutil.move(clip["sortie"], dest)
    dest.with_suffix(".txt").write_text(clip["legende"], encoding="utf-8")
    ancien_txt = Path(clip["sortie"]).with_suffix(".txt")
    if ancien_txt.exists():
        ancien_txt.unlink()  # la légende vit maintenant à côté de la vidéo, dans A PUBLIER
    log(f"file de publication : {dest.name}")
    return dest
