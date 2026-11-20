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
        "cam": {"type": "boolean"},
        "cam_zone": {"type": "array", "items": {"type": "integer"}},
        "cam_bandeau": {"type": "array", "items": {"type": "integer"}},
        "debut_s": {"type": "number"},
        "fin_s": {"type": "number"},
        "note_6": {"type": "integer"},
        "type_moment": {"type": "string", "enum": ["fail", "chaos", "drole", "action", "decouverte", "mission", "discussion"]},
    },
    "required": ["image", "spoiler", "musique_sous_licence", "accroche", "mot_fort", "legende", "interet", "inedit",
                 "deja_vu", "raison", "cam", "cam_zone", "cam_bandeau", "debut_s", "fin_s", "note_6", "type_moment"],
    "additionalProperties": False,
}

CONSIGNE = """Tu prépares un clip vertical court tiré du live Twitch d'un streamer français qui joue à {jeu}.
Streamer : {streamer}. Titre du clip donné par un spectateur : « {titre} ».
Le clip dure {duree} s. Ce qu'il dit (transcription automatique, peut contenir des erreurs ; le nombre entre
crochets est la seconde du clip où la phrase commence) :
{texte}

Les images {images} sont prises à 10, 30, 50, 70 et 90 % du clip.

Ligne de la page (20/11) : les streamers qui font n'importe quoi. On cherche les fails, les morts absurdes, les
plans qui tournent au désastre, les maladresses et le chaos drôle (exemple type : il ouvre l'arrière d'un camion et
se fait dévorer par un tigre). Une mission jouée proprement, une visite ou une découverte du jeu sans rien de drôle,
une discussion : ce n'est pas pour nous.

Sujets des clips déjà publiés par la page ces deux derniers jours (pour juger si c'est neuf) :
{recents}

Réponds en JSON :
- image : « cinematique » si on voit une scène cinématique (bandes noires, pas d'interface de jeu, caméra de film) ou si le streamer regarde une bande-annonce, une vidéo officielle de l'éditeur ou la vidéo de quelqu'un d'autre, « menu_ou_carte » pour un menu ou la carte, « autre » si on voit de la nudité, un personnage dénudé ou une scène sexuelle (même dans le jeu, ou si l'accroche en parlerait : nudiste, strip-tease, etc.), sinon « jeu_libre ».
- spoiler : true si le clip montre ou raconte la fin du jeu, une mission finale, une grosse révélation de l'histoire, ou une séquence de cinématiques.
- musique_sous_licence : true seulement si la transcription contient les paroles d'une chanson enregistrée (morceau d'artiste, radio du jeu) qui couvre une bonne partie du clip. Ne compte pas : des joueurs qui chantent ou scandent eux-mêmes, une comptine, un lecteur de musique ou un titre de morceau affiché à l'écran (tu n'entends pas le son, tu ne vois que les images et la transcription). Dans le doute, false.
- accroche : 2 à 7 mots en français, en majuscules, qui donnent envie de regarder sans mentir ni rien inventer, tirés de ce qui se passe vraiment. Elle doit APPORTER DU CONTEXTE OU UN POINT DE VUE que les images ne disent pas seules (l'enjeu, ce qui s'est passé avant, pourquoi c'est drôle ou rare), pas décrire l'action visible : « SON PREMIER BRAQUAGE TOURNE MAL » ou « 20 MIN À CHERCHER LA PLAGE » plutôt que « IL PERCUTE UN CAMION » (pas de « INCROYABLE », pas d'emoji, pas de point d'exclamation multiple, jamais de tiret long, de flèche ni de point médian). Orthographe et accords exacts (« AUX TROUSSES », pas « AU TROUSSE »).
- mot_fort : le mot de l'accroche (ou deux mots liés, ex. « GTA 5 ») qui porte l'info importante, recopié tel quel ; il sera en rose. Un seul.
- legende : une phrase courte en français qui dit ce qui se passe, avec au plus un smiley, sans hashtag (ils sont ajoutés ensuite), puis à la ligne une question courte qui donne envie de répondre en commentaire (ex. « Tu aurais fait pareil ? »).
- interet : de 0 à 10, la chance que ce moment fasse rire quelqu'un qui ne connaît pas le streamer, par un fail, une maladresse, une mort absurde ou un chaos drôle. Sois exigeant : 7 et plus seulement si le moment se comprend sans contexte et tient en quelques secondes. Une mission réussie, une balade ou une découverte sans gag : 4 au plus, même si c'est joli ou nouveau.
- inedit : true si la situation est rare (un gag qu'on ne reverra pas, une mort jamais vue), pas pour une simple découverte du jeu.
- type_moment : « fail » (il rate, meurt bêtement, se fait avoir), « chaos » (il sème le désordre exprès, n'importe quoi), « drole » (blague, réaction, absurde sans fail), « action » (poursuite, fusillade réussie), « decouverte » (lieu, véhicule, mécanique), « mission » (il joue l'histoire ou une mission normalement), « discussion » (il parle surtout).
- deja_vu : true seulement si c'est le même moment qu'un clip déjà publié listé plus haut (même streamer, même scène). Un thème proche (police, voiture, prison) chez un autre streamer n'est pas un doublon : false.
- raison : une ligne qui justifie la note.
- cam : true si on voit la webcam du streamer incrustée dans l'image du jeu (une petite vidéo de la personne réelle, souvent dans un coin), sur la première image.
- cam_zone : [x, y, largeur, hauteur] en pixels de la première image (960 pixels de large) : le rectangle qui contient UNIQUEMENT la vidéo de la webcam, sans le cadre décoratif, sans le nom, sans la barre d'objectif ni le logo collés autour. Mesure au pixel près les bords où l'image de la pièce s'arrête. [0, 0, 0, 0] s'il n'y a pas de cam.
- debut_s et fin_s : les secondes du clip à garder pour ne montrer que le moment fort, entre 8 et 20 s au total (25 s au plus si l'histoire l'exige) : commence juste avant que ça démarre (pas de longue mise en place), finis juste après la chute ou la réaction. Le moment doit rester compréhensible. Si tout le clip est utile et dure moins de 20 s : 0 et {duree}.
- note_6 : notre note du moment, de 1 à 6 étoiles (6 = moment à ne pas rater, 3 = sympa, 1 = faible). Elle est affichée sur la vidéo, sois honnête et exigeant.
- cam_bandeau : même format, le rectangle qui couvre la webcam ET tout ce qui y est collé (cadre, nom, barre de sub goal, logo ou bandeau de sponsor juste à côté). [0, 0, 0, 0] s'il n'y a pas de cam.
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


def analyser(source, titre, streamer, texte, jeu=None, recents=None, duree=None):
    c = config()
    jeu = jeu or c["jeu"]
    cle = _cle(c)
    voie = "api" if cle else ("cli" if c.get("analyse_par", "auto") in ("auto", "cli") and CLAUDE_CLI.exists() else None)
    if not voie:
        return dict(heuristique(titre, streamer, texte, jeu), indisponible=True)
    imgs = images_du_clip(source)
    if duree is None:
        try:
            duree = sonde(source)["duree"]
        except Exception:
            duree = 30.0
    consigne = CONSIGNE.format(jeu=jeu, streamer=streamer, titre=titre, texte=texte or "(rien d'audible)", duree=f"{duree:.1f}",
                               images="jointes" if voie == "api" else "lues",
                               recents="\n".join(f"- {x}" for x in (recents or [])) or "(aucun)")
    if voie == "cli":
        try:
            a = _nettoyer_sortie(_via_cli(c, imgs, consigne))
            a["voie"] = "cli"
            return a
        except Exception as e:
            log(f"analyse : claude -p en échec ({type(e).__name__}: {e}), règles simples")
            m = str(e).lower()
            # Claude indisponible pour tout le monde (limite d'abonnement, jeton) : à distinguer d'un échec propre au clip
            panne = any(k in m for k in ("usage_limit", "rate_limit", "429", "limit", "not logged in", "/login", "overloaded", "529"))
            return dict(heuristique(titre, streamer, texte, jeu, "analyse Claude en échec"), indisponible=panne)
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
        modele = c.get("modele_claude", "claude-opus-5-5")
        if "haiku" in modele:  # Haiku 5.5 n'a pas de repli côté serveur : appel simple
            r = client.messages.create(
                model=modele, max_tokens=4000,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": contenu}],
            )
        else:
            r = client.beta.messages.create(
                model=modele,
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
        log(f"analyse : erreur API ({type(e).__name__}: {str(e)[:200]}), règles simples")
        a = heuristique(titre, streamer, texte, jeu, "erreur de l'API Claude")
        # crédit épuisé, clé refusée, surcharge : le clip attend le passage suivant au lieu d'être jeté
        if isinstance(e, (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.AuthenticationError,
                          anthropic.PermissionDeniedError, anthropic.InternalServerError)) or \
                any(m in str(e).lower() for m in ("credit", "billing", "429", "529", "overloaded", "rate_limit", "rate limit")):
            a["indisponible"] = True
        return a


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
    appel = c.get("appel_abonnement", "")  # 18/11 : chaque post GTA 6 invite à s'abonner
    if appel and jeu and re.search(r"\b(VI|6)\b", jeu):
        base_legende = f"{base_legende}\n\n{appel}"
    return f"{base_legende}\n\n🎮 {streamer} en live sur twitch.tv/{streamer}\n\n{' '.join(tags)}"
