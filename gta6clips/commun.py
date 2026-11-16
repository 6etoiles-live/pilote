"""Outils partagés : réglages, base de données des clips, journal."""
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent


def nettoyer_titre(t):
    """Titre Twitch lisible : sans balises, liens, commandes (!discord), tirets longs, flèches ni points médians."""
    t = re.sub(r"<[^>]*>", " ", t or "")
    t = re.sub(r"https?://\S+|\S+\.(com|tv|gg|fr)\S*", " ", t)
    t = re.sub(r"(^|\s)![\w-]+", " ", t)
    t = re.sub(r"\s*[—–→←⇒·|]+\s*", ", ", t)
    t = re.sub(r"\s*(,\s*)+", ", ", t)  # « GTA 6 | !drop | Leonida » ne laisse pas « , , »
    return re.sub(r"\s+", " ", t).strip(" ,")

DONNEES = RACINE / "donnees"
SOURCES = DONNEES / "sources"
TRAVAIL = DONNEES / "travail"
BASE = DONNEES / "clips.db"
JOURNAL = DONNEES / "journal.log"

# launchd ne connaît pas le PATH du propriétaire : on ajoute ses binaires
os.environ["PATH"] = ":".join([str(Path.home() / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin", os.environ.get("PATH", "")])


def lire_json(chemin, defaut=None):
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return defaut


def config():
    c = lire_json(RACINE / "config.json", {})
    secret = lire_json(RACINE / "config.local.json", {}) or {}
    # hors du Mac (GitHub Actions) : clés, dossier de sortie et mode de publication viennent de l'environnement
    if os.environ.get("GTA6_SECRETS"):
        try:
            secret = dict(secret, **json.loads(os.environ["GTA6_SECRETS"]))
        except (ValueError, TypeError):
            secret = {}  # clés illisibles : la publication retombe en simulation
    c["secret"] = secret
    c["dossier_sortie"] = Path(os.path.expanduser(os.environ.get("GTA6_SORTIE") or c.get("dossier_sortie", "~/Movies/GTA6 CLIPS")))
    if os.environ.get("GTA6_PUBLIER"):
        c.setdefault("pilote", {})["publier_avec"] = os.environ["GTA6_PUBLIER"]
    return c


def police(nom):
    """Fichier d'une police : celle du Mac, sinon celle livrée avec l'outil (polices/)."""
    f = Path.home() / "Library" / "Fonts" / nom
    return f if f.exists() else RACINE / "polices" / nom


def chrome():
    """Commande de Chrome sans fenêtre : celui du Mac, sinon celui de la machine (Linux)."""
    import shutil, sys
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(mac).exists():
        return [mac]
    for nom in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(nom):
            return [shutil.which(nom), "--no-sandbox"]
    return [mac]


def encodeur_aac():
    """AAC d'Apple sur le Mac (meilleur), AAC de ffmpeg ailleurs."""
    import sys
    return "aac_at" if sys.platform == "darwin" else "aac"


def en_jpeg(png, jpg):
    """PNG vers JPEG qualité haute (TikTok refuse le PNG pour les photos)."""
    import shutil, subprocess
    if shutil.which("sips"):
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "95", str(png), "--out", str(jpg)]
    else:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(png), "-q:v", "2", str(jpg)]
    subprocess.run(cmd, capture_output=True, check=True)


def corbeille(f):
    """Jamais de suppression directe : le fichier va à la corbeille."""
    import shutil, time
    f = Path(f)
    if f.exists():
        d = Path.home() / ".Trash"
        d.mkdir(exist_ok=True)
        shutil.move(str(f), str(d / f"{f.stem}-{int(time.time())}{f.suffix}"))


def streamers():
    d = lire_json(RACINE / "streamers.json", {"streamers": []})
    return {s["login"].lower(): s for s in d["streamers"]}


def maintenant():
    return dt.datetime.now(dt.timezone.utc)


def paris():
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("Europe/Paris"))


class Verrou:
    """Un seul passage à la fois (launchd, ou un lancement à la main pendant un passage)."""

    def __init__(self, nom="tourner"):
        self.chemin = DONNEES / f"{nom}.verrou"
        self.f = None

    def __enter__(self):
        import fcntl
        DONNEES.mkdir(parents=True, exist_ok=True)
        self.f = open(self.chemin, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.close()
            self.f = None
            return False
        self.f.write(str(os.getpid()))
        self.f.flush()
        return True

    def __exit__(self, *a):
        if self.f:
            self.f.close()


def log(msg):
    ligne = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(ligne, flush=True)
    DONNEES.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(ligne + "\n")


def base():
    DONNEES.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(BASE)
    db.row_factory = sqlite3.Row
    db.execute("""create table if not exists clips (
        id text primary key,
        url text not null,
        streamer text,
        titre text,
        jeu text,
        langue text,
        vues integer,
        duree real,
        cree_le text,
        video_id text,
        vod_offset integer,
        score real,
        statut text default 'nouveau',
        raison text,
        source text,
        sortie text,
        accroche text,
        legende text,
        vu_le text,
        maj text
    )""")
    db.execute("""create table if not exists publications (
        clip_id text,
        plateforme text,
        statut text,
        reponse text,
        le text
    )""")
    if "post_id" not in [r[1] for r in db.execute("pragma table_info(publications)")]:
        db.execute("alter table publications add column post_id text")  # id du post chez Zernio, relu plus tard
    # relevés de vues successifs : la vitesse d'un clip compte plus que son total
    db.execute("create table if not exists releves (clip_id text, le text, vues integer)")
    db.execute("create index if not exists releves_clip on releves (clip_id, le)")
    # file de publication du pilote automatique (clips montés et cartes Infos)
    db.execute("""create table if not exists file (
        id text primary key,
        genre text,
        ref text,
        fichier text,
        legende text,
        streamer text,
        accroche text,
        interet integer,
        inedit integer,
        score real,
        pas_avant text,
        expire_le text,
        statut text default 'pret',
        ajoute_le text
    )""")
    return db


def maj(db, clip_id, **champs):
    champs["maj"] = maintenant().isoformat()
    cols = ", ".join(f"{k} = ?" for k in champs)
    db.execute(f"update clips set {cols} where id = ?", [*champs.values(), clip_id])
    db.commit()


def lancer(cmd, **kw):
    """Lance une commande, renvoie la sortie texte ; lève une erreur lisible sinon."""
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} a échoué ({r.returncode}) : {r.stderr.strip()[-800:]}")
    return r.stdout


def sonde(fichier):
    out = lancer(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(fichier)])
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    num, den = (v.get("avg_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return {"w": int(v["width"]), "h": int(v["height"]), "fps": fps, "duree": float(d["format"]["duration"]),
            "audio": any(s["codec_type"] == "audio" for s in d["streams"])}
