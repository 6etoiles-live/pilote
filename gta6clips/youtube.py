"""Envoi des clips en YouTube Shorts par l'API officielle YouTube Data v3, gratuite.

Choisi le 18/11/2026 : Google a levé le 02/11/2026 la règle qui bloquait en privé les vidéos envoyées par un projet
non audité (developers.google.com/youtube/v3/revision_history, vérifié le 18/11) ; videos.insert = 100 envois par
jour au plus (« Video Uploads quota bucket »). Zernio demandait 6 $ par mois pour un 3e compte.
Une vidéo verticale de 3 min au plus devient un Short toute seule.

Clés (jamais dans le dépôt) : youtube_client_id, youtube_client_secret, youtube_refresh_token, soit dans
config.local.json, soit dans le secret GitHub YOUTUBE_SECRETS (JSON), fabriqué par deploiement/youtube_jeton.py.
Le projet Google doit être « En production » : en « Test », le jeton meurt au bout de 7 jours.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

JETON = "https://oauth2.googleapis.com/token"
ENVOI = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
CLES = ("youtube_client_id", "youtube_client_secret", "youtube_refresh_token")


def branche(secret):
    return all(secret.get(k) for k in CLES)


def jeton_acces(secret):
    corps = urllib.parse.urlencode({"client_id": secret["youtube_client_id"],
                                    "client_secret": secret["youtube_client_secret"],
                                    "refresh_token": secret["youtube_refresh_token"],
                                    "grant_type": "refresh_token"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(JETON, data=corps, method="POST"), timeout=60) as r:
            return json.loads(r.read().decode())["access_token"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"YouTube jeton : HTTP {e.code} {e.read().decode(errors='replace')[:300]}") from None


def _propre(t):
    return t.replace("<", "").replace(">", "")  # YouTube refuse < et > dans le titre et la description


def titre_et_texte(legende):
    """Titre : première ligne de la légende (100 signes au plus, avec #Shorts) ; description : la légende entière."""
    lignes = [l for l in legende.split("\n") if l.strip()]
    premiere = _propre(lignes[0].strip()) if lignes else "GTA 6"
    premiere = re.sub(r"\s+#\S+", "", premiere).strip() or "GTA 6"
    # recherche YouTube (18/11) : le titre commence par « GTA 6 » pour un clip GTA 6 qui ne le nomme pas
    if "#gta6" in legende.lower() and not re.search(r"\bGTA\b", premiere, re.I):
        premiere = "GTA 6 : " + premiere
    suffixe = " #Shorts"
    if len(premiere) + len(suffixe) > 100:
        premiere = premiere[:100 - len(suffixe) - 1].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
    return premiere + suffixe, _propre(legende).encode()[:4900].decode(errors="ignore")  # limite YouTube : 5 000 octets


def publier(secret, fichier, legende, jeton=None):
    """Envoie la vidéo en public. Renvoie (statut, détail, id YouTube)."""
    f = Path(fichier)
    if f.suffix.lower() != ".mp4":
        return "echec", "YouTube : seulement les vidéos", ""
    jeton = jeton or jeton_acces(secret)
    titre, texte = titre_et_texte(legende)
    meta = {"snippet": {"title": titre, "description": texte, "categoryId": "20",
                        "defaultLanguage": "fr", "defaultAudioLanguage": "fr",
                        "tags": ["GTA 6", "GTA VI", "GTA 6 FR", "streamer", "Twitch"]},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": False, "embeddable": True}}
    taille = f.stat().st_size
    req = urllib.request.Request(ENVOI, data=json.dumps(meta).encode(), method="POST", headers={
        "Authorization": f"Bearer {jeton}", "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(taille)})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            adresse = r.headers["Location"]
        with open(f, "rb") as flux:
            req = urllib.request.Request(adresse, data=flux, method="PUT", headers={
                "Authorization": f"Bearer {jeton}", "Content-Type": "video/mp4", "Content-Length": str(taille)})
            with urllib.request.urlopen(req, timeout=900) as r:
                video = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return "echec", f"YouTube HTTP {e.code} {e.read().decode(errors='replace')[:300]}", ""
    vid = video.get("id") or ""
    etat = (video.get("status") or {}).get("uploadStatus", "?")
    if not vid or etat in ("failed", "rejected"):
        return "echec", f"YouTube : envoi {etat} {json.dumps(video.get('status') or {})[:200]}", vid
    return "publie", f"https://youtube.com/shorts/{vid}", vid
