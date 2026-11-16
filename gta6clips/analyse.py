"""Accroche, légende, intérêt et contrôle anti-spoiler par Claude.

Trois voies, dans cet ordre :
- l'API Claude si une clé est posée (config.local.json → anthropic_api_key, ou ANTHROPIC_API_KEY) ;
- sinon la commande « claude -p » de Claude Code (abonnement du propriétaire, aucune dépense en plus), lancée avec un
  contexte réduit au minimum (sans MCP, sans réglages, sans crochets) : environ 5 000 jetons et 5 s par clip ;
- sinon des règles simples : le clip n'est alors jamais publié seul (pilote.py le laisse en attente).
"""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .commun import nettoyer_titre, config, lancer, log, sonde

SCHEMA = {
    "type": "object",
    "properties": {
        "image": {"type": "string", "enum": ["jeu_libre", "cinematique", "menu_ou_carte", "autre"]},
        "spoiler": {"type": "boolean"},
        "musique_sous_licence": {"type": "boolean"},
        "accroche": {"type": "string"},
        "mot_fort": {"type": "string"},
        "legende": {"type": "string"},
        "interet": {"type": "integer"},
        "inedit": {"type": "boolean"},
        "deja_vu": {"type": "boolean"},
        "raison": {"type": "string"},
    },
    "required": ["image", "spoiler", "musique_sous_licence", "accroche", "mot_fort", "legende", "interet", "inedit",
                 "deja_vu", "raison"],
    "additionalProperties": False,
}

CONSIGNE = """Tu prépares un clip vertical court tiré du live Twitch d'un streamer français qui joue à {jeu}.
Streamer : {streamer}. Titre du clip donné par un spectateur : « {titre} ».
Ce qu'il dit (transcription automatique, peut contenir des erreurs) :
{texte}

Les images {images} sont prises à 10, 30, 50, 70 et 90 % du clip.

Sujets des clips déjà publiés par la page ces deux derniers jours (pour juger si c'est neuf) :
{recents}

Réponds en JSON :
- image : « cinematique » si on voit une scène cinématique (bandes noires, pas d'interface de jeu, caméra de film), « menu_ou_carte » pour un menu ou la carte, sinon « jeu_libre ».
- spoiler : true si le clip montre ou raconte la fin du jeu, une mission finale, une grosse révélation de l'histoire, ou une séquence de cinématiques.
- musique_sous_licence : true seulement si la transcription contient les paroles d'une chanson enregistrée (morceau d'artiste, radio du jeu) qui couvre une bonne partie du clip. Ne compte pas : des joueurs qui chantent ou scandent eux-mêmes, une comptine, un lecteur de musique ou un titre de morceau affiché à l'écran (tu n'entends pas le son, tu ne vois que les images et la transcription). Dans le doute, false.
- accroche : 2 à 7 mots en français, en majuscules, qui donnent envie de regarder sans mentir ni rien inventer, tirés de ce qui se passe vraiment (pas de « INCROYABLE », pas d'emoji, pas de point d'exclamation multiple, jamais de tiret long, de flèche ni de point médian).
- mot_fort : le mot de l'accroche (ou deux mots liés, ex. « GTA 5 ») qui porte l'info importante, recopié tel quel ; il sera en rose. Un seul.
- legende : une phrase courte en français qui dit ce qui se passe, avec au plus un smiley, sans hashtag (ils sont ajoutés ensuite).
- interet : de 0 à 10, la chance que ce moment fasse rire ou surprenne quelqu'un qui ne connaît pas le streamer. Sois exigeant : 7 et plus seulement si le moment se comprend sans contexte et tient en quelques secondes.
- inedit : true si le clip montre une découverte du jeu (un lieu, un véhicule, un animal, une mécanique, une réaction à une nouveauté) ou une situation rare.
- deja_vu : true si le sujet est le même qu'un des clips déjà publiés listés plus haut.
- raison : une ligne qui justifie la note.
"""


def _cle(c):
    return c["secret"].get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")


def images_du_clip(source, n=5):
    info = sonde(source)
    d = tempfile.mkdtemp(prefix="gta6img")
    imgs = []
    for i, t in enumerate([info["duree"] * f for f in (0.1, 0.3, 0.5, 0.7, 0.9)][:n]):
        p = Path(d) / f"{i}.jpg"
        lancer(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(source), "-frames:v", "1",
                "-vf", "scale=960:-2", "-q:v", "4", str(p)])
        imgs.append(p)
    return imgs


def nom_court(jeu):
    return {"Grand Theft Auto VI": "GTA 6", "Grand Theft Auto V": "GTA 5"}.get(jeu, jeu)


PETITS_MOTS = {"de", "la", "le", "les", "du", "des", "à", "a", "au", "aux", "et", "en", "un", "une", "sur", "avec",
               "pour", "dans", "par", "son", "sa", "ses", "mon", "ma", "ton", "ta", "qui", "que", "d'", "l'"}


def couper_aux_mots(t, n):
    """Coupe à n caractères sans couper un mot ni finir sur « DE », « LA »…"""
    if len(t) <= n:
        return t
    mots = t[:n + 1].rsplit(" ", 1)[0].split()
    while len(mots) > 1 and mots[-1].lower() in PETITS_MOTS:
        mots.pop()
    return " ".join(mots).rstrip(" ,")


def heuristique(titre, streamer, texte, jeu="Grand Theft Auto VI", pourquoi="pas de clé API"):
    t = nettoyer_titre(titre)
    lisible = len(re.findall(r"[A-Za-zÀ-ÿ]", t)) >= 8 and len(t.split()) >= 2 and not t.lower().startswith("wzbot")
    accroche = couper_aux_mots(t.upper(), 60) if lisible else f"{streamer.upper()} SUR {nom_court(jeu)}"
    if lisible:  # à défaut de mieux, le mot le plus long de l'accroche
        mot_fort = max(re.findall(r"[\wÀ-ÿ]{4,}", accroche) or [""], key=len)
    else:
        mot_fort = nom_court(jeu)
    return {"image": "inconnu", "spoiler": False, "musique_sous_licence": False, "accroche": accroche, "mot_fort": mot_fort,
            "legende": (t if lisible else f"{streamer} sur {nom_court(jeu)}"), "interet": -1,
            "inedit": False, "deja_vu": False, "voie": "regles",
            "raison": f"{pourquoi} : accroche tirée du titre, à relire"}


CLAUDE_CLI = Path.home() / ".local/bin/claude"
if not CLAUDE_CLI.exists() and shutil.which("claude"):
    CLAUDE_CLI = Path(shutil.which("claude"))


def _nettoyer_sortie(a):
    for champ in ("accroche", "mot_fort", "legende"):  # la consigne ne suffit pas : on nettoie aussi la sortie
        a[champ] = nettoyer_titre(a.get(champ, ""))
    a.setdefault("inedit", False)
    a.setdefault("deja_vu", False)
    return a


def _via_cli(c, imgs, consigne):
    """Analyse par « claude -p » (abonnement), contexte minimal : pas de MCP, pas de réglages, pas de crochets."""
    noms = ", ".join(str(p) for p in imgs)
    invite = (f"Lis avec l'outil Read les images {noms}, puis réponds à la consigne suivante.\n\n" + consigne)
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "USER": os.environ.get("USER", ""),
           "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):  # hors du Mac : jeton de l'abonnement, pas de trousseau
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    r = subprocess.run(
        [str(CLAUDE_CLI), "-p", invite, "--model", c.get("modele_cli", "sonnet"), "--output-format", "json",
         "--json-schema", json.dumps(SCHEMA), "--tools", "Read", "--allowedTools", "Read",
         "--no-session-persistence", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
         "--exclude-dynamic-system-prompt-sections", "--settings", '{"disableAllHooks": true}',
         "--system-prompt", "Tu prépares des clips de jeux vidéo pour une page de clips. Tu réponds seulement par "
                            "l'objet JSON demandé, en français, sans tiret long, sans flèche, sans point médian."],
        capture_output=True, text=True, timeout=240, cwd=str(imgs[0].parent), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"claude -p a échoué ({r.returncode}) : {(r.stderr or r.stdout).strip()[-300:]}")
    d = json.loads(r.stdout)
    if d.get("is_error") or not isinstance(d.get("structured_output"), dict):
        raise RuntimeError(f"claude -p sans réponse exploitable : {str(d.get('result'))[:200]}")
    return d["structured_output"]


def analyser(source, titre, streamer, texte, jeu=None, recents=None):
    c = config()
    jeu = jeu or c["jeu"]
    cle = _cle(c)
    voie = "api" if cle else ("cli" if c.get("analyse_par", "auto") in ("auto", "cli") and CLAUDE_CLI.exists() else None)
    if not voie:
        return heuristique(titre, streamer, texte, jeu)
    imgs = images_du_clip(source)
    consigne = CONSIGNE.format(jeu=jeu, streamer=streamer, titre=titre, texte=texte or "(rien d'audible)",
                               images="jointes" if voie == "api" else "lues",
                               recents="\n".join(f"- {x}" for x in (recents or [])) or "(aucun)")
    if voie == "cli":
        try:
            a = _nettoyer_sortie(_via_cli(c, imgs, consigne))
            a["voie"] = "cli"
            return a
        except Exception as e:
            log(f"analyse : claude -p en échec ({type(e).__name__}: {e}), règles simples")
            return heuristique(titre, streamer, texte, jeu, "analyse Claude en échec")
        finally:
            shutil.rmtree(imgs[0].parent, ignore_errors=True)
    try:
        import anthropic
    except ImportError:
        log("analyse : paquet anthropic absent, règles simples")
        shutil.rmtree(imgs[0].parent, ignore_errors=True)
        return heuristique(titre, streamer, texte, jeu)

    contenu = []
    for p in imgs:
        contenu.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(p.read_bytes()).decode()}})
    shutil.rmtree(imgs[0].parent, ignore_errors=True)
    contenu.append({"type": "text", "text": consigne})
    client = anthropic.Anthropic(api_key=cle)
    try:
        r = client.beta.messages.create(
            model=c.get("modele_claude", "claude-opus-5-5"),
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": contenu}],
        )
        if r.stop_reason == "refusal":
            log("analyse : refus du modèle, règles simples")
            return heuristique(titre, streamer, texte, jeu, "refus du modèle")
        texte_json = next(b.text for b in r.content if b.type == "text")
        a = _nettoyer_sortie(json.loads(texte_json))
        a["voie"] = "api"
        return a
    except Exception as e:
        log(f"analyse : erreur API ({type(e).__name__}: {e}), règles simples")
        return heuristique(titre, streamer, texte, jeu, "erreur de l'API Claude")


def legende_complete(c, base_legende, streamer, jeu=None):
    if not jeu:  # jeu inconnu (clip sans catégorie) : hashtags neutres
        tags = ["#gtafr", "#streamerfr", "#twitchfr", "#gaming", f"#{streamer}"]
    elif not re.search(r"\b(VI|6)\b", jeu):  # banc d'essai sur un autre jeu : pas de #gta6 trompeur
        from .radar import jeux_en_attendant
        if jeu in jeux_en_attendant(c):  # avant la sortie : clip d'attente, dit comme tel
            base_legende = f"{base_legende}\n\nEn attendant GTA 6 ⭐"
            tags = ["#gta6", "#gtav", "#gta5", "#gtafr", f"#{streamer}"]
        else:
            tags = ["#gta5", "#gtav", "#gtafr", "#streamerfr", f"#{streamer}"]
    else:
        # deux fixes, deux qui changent d'un post à l'autre (choix stable pour une même légende), puis le streamer
        import zlib
        reste = [t for t in c["hashtags_base"] if t not in ("gta6", "gta6fr")]
        k = zlib.crc32(f"{base_legende}{streamer}".encode())
        n = len(reste)
        choisis = [reste[k % n], reste[(k % n + 1 + (k // n) % (n - 1)) % n]] if n > 1 else reste  # deux différents
        tags = ["#gta6", "#gta6fr"] + [f"#{t}" for t in dict.fromkeys(choisis)] + [f"#{streamer}"]
    return f"{base_legende}\n\n🎮 {streamer} en live sur twitch.tv/{streamer}\n\n{' '.join(tags)}"
