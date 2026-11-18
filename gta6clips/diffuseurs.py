"""Envoi des posts vers TikTok et Instagram.

Diffuseurs (pilote.publier_avec dans config.json) :
- « aucun » : rien ne part, le pilote note ce qu'il aurait publié (tant que les comptes ne sont pas branchés) ;
- « zernio » : compte Zernio propre à la page (jamais celui du propriétaire). Choisi le 12/11/2026 après vérification sur
  les pages officielles (zernio.com/pricing, docs.zernio.com) : 2 comptes gratuits sans carte bancaire, posts
  illimités, API complète ; le fichier part directement du Mac (POST /v1/media/presign, PUT, puis POST /v1/posts) ;
  TikTok publié en public par la connexion « TikTok for Business », 15 vidéos par 24 h glissantes au plus.
  Clés dans config.local.json : zernio_api_key, zernio_comptes {"tiktok": "<accountId>", "instagram": "<accountId>"}
  (./gta6.py diffusion les relève et les écrit).
Écartés le 12/11/2026 : Buffer (pas d'envoi de fichier, il faut héberger chaque vidéo à une adresse publique),
l'API TikTok en direct (non audité = posts privés ; l'audit refuse un outil pour ses propres comptes), un tunnel
temporaire depuis le Mac (refusé par le garde-fou de sécurité de Claude Code).
TikTok supprime les lignes vides d'un texte envoyé par API (constaté sur le compte du propriétaire le 25/09/2026) : aerer().
"""
import json
import mimetypes
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import youtube
from .commun import RACINE, config, log

API = "https://zernio.com/api/v1"


def aerer(t):
    return "\n".join("⠀" if l.strip() == "" else l for l in t.split("\n"))


def _appel(cle, methode, chemin, corps=None, entetes=None, timeout=120):
    h = {"Authorization": f"Bearer {cle}", "Content-Type": "application/json", **(entetes or {})}
    req = urllib.request.Request(API + chemin, method=methode, headers=h,
                                 data=json.dumps(corps).encode() if corps is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        texte = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"Zernio {methode} {chemin} : HTTP {e.code} {texte}") from None


def envoyer_fichier(cle, fichier):
    """Envoi direct du fichier local vers le stockage de Zernio ; renvoie son adresse publique."""
    f = Path(fichier)
    type_ = mimetypes.guess_type(f.name)[0] or ("video/mp4" if f.suffix.lower() == ".mp4" else "image/jpeg")
    _, p = _appel(cle, "POST", "/media/presign", {"filename": f.name, "contentType": type_, "size": f.stat().st_size})
    with open(f, "rb") as flux:
        req = urllib.request.Request(p["uploadUrl"], data=flux, method="PUT",
                                     headers={"Content-Type": type_, "Content-Length": str(f.stat().st_size)})
        with urllib.request.urlopen(req, timeout=900) as r:
            if r.status not in (200, 201):
                raise RuntimeError(f"envoi du fichier refusé : HTTP {r.status}")
    return p["publicUrl"], type_


def _etat(post):
    """(statut, détail) à partir d'un post Zernio : publie, en_cours (Zernio continue seul) ou echec."""
    lignes = post.get("platforms") or [{}]
    etat = lignes[0].get("status") or post.get("status") or "?"
    lien = lignes[0].get("platformPostUrl") or ""
    pid = post.get("_id") or post.get("id") or ""
    if etat == "published":
        return "publie", f"{lien}".strip() or "publié"
    if etat in ("failed", "error"):
        return "echec", f"{etat} : {lignes[0].get('errorMessage') or ''}".strip()
    return "en_cours", f"{etat} (post {pid})"


def publier_zernio(cle, compte, plateforme, fichier, texte, cle_unique):
    """Renvoie (statut, détail, id du post Zernio)."""
    url, type_ = envoyer_fichier(cle, fichier)
    video = type_.startswith("video")
    entree = {"platform": plateforme, "accountId": compte}
    corps = {"content": texte, "mediaItems": [{"type": "video" if video else "image", "url": url}],
             "platforms": [entree], "publishNow": True}
    if plateforme == "tiktok":
        reglages = {"privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": True,
                    "content_preview_confirmed": True, "express_consent_given": True}
        if video:
            corps["content"] = aerer(texte)
            reglages.update(allow_duet=True, allow_stitch=True)
        else:
            # photo : content devient un titre de 90 signes sans hashtags, la légende complète va dans description
            from .analyse import couper_aux_mots
            corps["content"] = couper_aux_mots(texte.split("\n")[0].split(" ⭐")[0], 88)
            reglages.update(media_type="photo", photo_cover_index=0, description=aerer(texte))
        corps["tiktokSettings"] = reglages
    elif plateforme == "instagram" and video:
        entree["platformSpecificData"] = {"shareToFeed": True}
    try:
        # même clé pour un nouvel essai : Zernio rend le post d'origine au lieu d'en créer un second (24 h)
        statut_http, r = _appel(cle, "POST", "/posts", corps, {"Idempotency-Key": cle_unique[:255]}, timeout=900)
    except RuntimeError as e:
        if "HTTP 409" in str(e):  # premier envoi encore en traitement, ou même contenu déjà publié
            return "en_cours", f"409 : {str(e)[-200:]}", ""
        raise
    post = r.get("post") or {}
    pid = post.get("_id") or post.get("id") or r.get("postId") or ""
    if not post:  # 202 : enregistré, pas encore publié
        return "en_cours", f"accepté (HTTP {statut_http})", pid
    statut, detail = _etat(post)
    return statut, f"{detail} (HTTP {statut_http})", pid


def relire(cle, post_id):
    """État actuel d'un post envoyé plus tôt (GET /v1/posts/{id})."""
    _, r = _appel(cle, "GET", f"/posts/{post_id}")
    return _etat(r.get("post") or r)


def comptes(cle):
    """Comptes branchés sur Zernio : {plateforme: [(id, pseudo)]}."""
    _, r = _appel(cle, "GET", "/accounts")
    out = {}
    for a in r.get("accounts") or r.get("data") or []:
        out.setdefault(a.get("platform"), []).append((a.get("_id") or a.get("id"), a.get("username") or a.get("displayName")))
    return out


def ecrire_comptes(cle):
    """Relève les comptes TikTok et Instagram de la page et les écrit dans config.local.json."""
    trouves = comptes(cle)
    p = RACINE / "config.local.json"
    loc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    loc.setdefault("zernio_comptes", {})
    for plat in ("tiktok", "instagram"):
        if len(trouves.get(plat, [])) == 1:
            loc["zernio_comptes"][plat] = trouves[plat][0][0]
    p.write_text(json.dumps(loc, ensure_ascii=False, indent=1), encoding="utf-8")
    p.chmod(0o600)
    return trouves, loc["zernio_comptes"]


# ---------- point d'entrée ----------

class Diffusion:
    """Un passage de publication."""

    def __init__(self):
        c = config()
        self.mode = c.get("pilote", {}).get("publier_avec", "aucun")
        self.secret = c["secret"]
        self.plateformes = list(c.get("pilote", {}).get("plateformes", {"tiktok": 1, "instagram": 1}))
        if self.mode == "zernio" and not (self.secret.get("zernio_api_key") and self.secret.get("zernio_comptes")):
            log("diffusion : zernio demandé mais zernio_api_key ou zernio_comptes absents de config.local.json, rien ne part")
            self.mode = "aucun"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    @property
    def simulation(self):
        return self.mode != "zernio"

    def actives(self):
        """Plateformes servies : toutes en simulation, seulement celles branchées en réel."""
        if self.simulation:
            return self.plateformes
        return [p for p in self.plateformes if (youtube.branche(self.secret) if p == "youtube"
                                                 else self.secret["zernio_comptes"].get(p))]

    @staticmethod
    def accepte(plateforme, fichier):
        """YouTube ne prend que les vidéos (les cartes photo restent pour TikTok et Instagram)."""
        return plateforme != "youtube" or str(fichier).lower().endswith(".mp4")

    def publier(self, plateforme, fichier, texte, ident=None):
        """Renvoie (statut, détail, id du post). statut : publie | en_cours | simule | echec."""
        if self.simulation:
            return "simule", f"aurait publié sur {plateforme} : {Path(fichier).name}", ""
        try:
            if plateforme == "youtube":
                if not getattr(self, "_jeton_yt", None):
                    self._jeton_yt = youtube.jeton_acces(self.secret)
                return youtube.publier(self.secret, fichier, texte, self._jeton_yt)
            cle_unique = f"6etoiles-{plateforme}-{ident or uuid.uuid4().hex}"
            return publier_zernio(self.secret["zernio_api_key"], self.secret["zernio_comptes"][plateforme], plateforme,
                                  fichier, texte, cle_unique)
        except Exception as e:
            return "echec", f"{type(e).__name__}: {e}"[:400], ""

    def relire(self, post_id):
        return relire(self.secret["zernio_api_key"], post_id)
