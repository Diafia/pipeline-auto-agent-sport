"""
pipeline_article.py — Pipeline éditorial multi-agents basé sur autoagent 0.6.0
================================================================================
Flux : recherche → validation (reboucle jusqu'à 10) → reformatage → rédaction
       → validation du ton → SEO → génération du site.

Le domaine (sport | medecine) est un simple paramètre :
    python pipeline_article.py sport
    python pipeline_article.py medecine

Prérequis :
    - autoagent >= 0.6.0 dans le PYTHONPATH
    - OPENAI_API_KEY (ou autre provider) dans l'env
    - implémenter search_web() avec ta vraie API de recherche (voir TODO)
"""


from __future__ import annotations  # LIGNE 1 : Toujours celle-ci en premier !

import os

# Injection forcée de ta clé Gemini juste après
os.environ["GEMINI_API_KEY"] = "CLE_API_GENAI"  # Remets ta vraie clé ici
os.environ["TAVILY_API_KEY"] = "CLE_API_TRAVELI"

import json
import sys
from pathlib import Path

from autoagent import (
    Agent,
    AgentTurnContext,
    MaxStepsExceeded,
    Message,
    ProjectWorkspace,
    TraceEmitter,
)

# ─────────────────────────────────────────────────────────────────────────────
# 0. Configuration — le domaine est un paramètre, rien d'autre ne change
# ─────────────────────────────────────────────────────────────────────────────

DOMAIN = (sys.argv[1] if len(sys.argv) > 1 else "sport").lower()
assert DOMAIN in {"sport", "medecine"}, "usage: python pipeline_article.py [sport|medecine]"

DOMAIN_PROFILES = {
    "sport": {
        "label": "le sport",
        "angle": "actualité sportive, performance, entraînement",
        "sources_fiables": "médias sportifs reconnus, fédérations, études sur la performance",
    },
    "medecine": {
        "label": "la médecine",
        "angle": "santé, recherche médicale, prévention",
        "sources_fiables": "revues à comité de lecture, institutions de santé (OMS, Inserm, HAS)",
    },
}
PROFILE = DOMAIN_PROFILES[DOMAIN]

TARGET_ARTICLES = 10
MAX_SEARCH_ROUNDS = 5      # garde-fou : évite une boucle infinie de recherche
MAX_REVISIONS = 2          # nombre max de passes de révision du ton

PROVIDER = "gemini"
MODEL = "gemini-2.5-flash"

SITE_DIR = Path("./site")
SITE_DIR.mkdir(exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# 1. État partagé du pipeline (l'hôte est la source de vérité, pas le LLM)
# ─────────────────────────────────────────────────────────────────────────────

class PipelineState:
    def __init__(self) -> None:
        self.candidates: list[dict] = []   # articles trouvés, pas encore validés
        self.validated: list[dict] = []    # articles acceptés par le validateur
        self.rejected_urls: set[str] = set()
        self.formatted: list[dict] = []    # articles normalisés
        self.draft: str = ""               # article rédigé
        self.seo: dict = {}                # métadonnées SEO

    def missing(self) -> int:
        return TARGET_ARTICLES - len(self.validated)


state = PipelineState()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Tools partagés
# ─────────────────────────────────────────────────────────────────────────────

def search_web(query: str, max_results: int = 5) -> dict:
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key or api_key.startswith("tvly-votre"):
        print("[Chercheur] Erreur : Clé TAVILY_API_KEY manquante ou non valide.")
        return {"results": []}
        
    url = "https://api.tavily.com/search"
    headers = {"Content-Type": "application/json"}
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic"
    }
    
    try:
        import urllib.request
        import json
        req = urllib.request.Request(
            url, 
            data=json.dumps(payload).encode("utf-8"), 
            headers=headers, 
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            results = []
            for item in res_data.get("results", []):
                results.append({
                    "title": item.get("title", "Sans titre"),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", "")[:300]
                })
            return {"results": results}
            
    except Exception as e:
        print(f"\n[DEBUG SERIEUX] L'erreur de recherche est : {e}")
        import traceback
        traceback.print_exc()
        return {"results": []}


# ─────────────────────────────────────────────────────────────────────────────
# 3. ÉTAPE 1+2+3 — Recherche + validation + reboucle jusqu'à 10 articles
#    La reboucle est une boucle Python côté hôte : déterministe, auditable.
# ─────────────────────────────────────────────────────────────────────────────

def build_researcher(trace: TraceEmitter) -> Agent:
    agent = Agent.from_model(
        PROVIDER, MODEL,
        system_prompt=(
            f"Tu es un documentaliste spécialisé dans {PROFILE['label']} "
            f"({PROFILE['angle']}). Tu cherches des articles récents et fiables. "
            "Pour chaque article pertinent trouvé, appelle propose_article. "
            "Ne propose jamais deux fois la même URL."
        ),
        max_steps=12,
        trace=trace,
    )

    @agent.tool(permissions=["network"])
    def web_search(query: str) -> dict:
        """Cherche des articles sur le web. Retourne titre, URL et extrait."""
        try:
            return search_web(query)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    @agent.tool
    def propose_article(title: str, url: str, summary: str) -> dict:
        """Propose un article candidat (titre, URL, résumé en 2-3 phrases)."""
        if url in state.rejected_urls or any(c["url"] == url for c in state.candidates):
            return {"ok": False, "reason": "URL déjà proposée ou rejetée"}
        state.candidates.append({"title": title, "url": url, "summary": summary})
        return {"ok": True, "total_candidates": len(state.candidates)}

    return agent


def build_validator(trace: TraceEmitter) -> Agent:
    agent = Agent.from_model(
        PROVIDER, MODEL,
        system_prompt=(
            f"Tu es un fact-checker exigeant dans le domaine de {PROFILE['label']}. "
            f"Sources fiables attendues : {PROFILE['sources_fiables']}. "
            "Pour chaque candidat, appelle accept_article ou reject_article "
            "avec une justification courte."
        ),
        max_steps=TARGET_ARTICLES + 5,
        trace=trace,
    )

    @agent.tool
    def accept_article(url: str, justification: str) -> dict:
        """Accepte un article candidat comme source fiable et pertinente."""
        for c in state.candidates:
            if c["url"] == url:
                state.candidates.remove(c)
                c["justification"] = justification
                state.validated.append(c)
                return {"ok": True, "validated": len(state.validated)}
        return {"ok": False, "reason": "URL inconnue dans les candidats"}

    @agent.tool
    def reject_article(url: str, reason: str) -> dict:
        """Rejette un article candidat (source douteuse, hors sujet...)."""
        for c in state.candidates:
            if c["url"] == url:
                state.candidates.remove(c)
                state.rejected_urls.add(url)
                return {"ok": True, "reason": reason}
        return {"ok": False, "reason": "URL inconnue dans les candidats"}

    return agent


def build_supervisor(trace: TraceEmitter, researcher: Agent, validator: Agent) -> Agent:
    """Agent dédié à la REBOUCLE. C'est LUI qui exécute la boucle : dans son
    propre agent.run(), il enchaîne get_progress → lancer_recherche →
    lancer_validation → get_progress → ... jusqu'à atteindre l'objectif.
    Le chercheur et le validateur lui sont exposés comme des tools
    (pattern "agents as tools"). Le garde-fou est son max_steps :
    dépassé → MaxStepsExceeded (§14.2), catché par le host."""
    agent = Agent.from_model(
        PROVIDER, MODEL,
        system_prompt=(
            "Tu es le superviseur d'un pipeline de collecte d'articles. "
            f"Ton objectif : obtenir {TARGET_ARTICLES} articles VALIDÉS sur "
            f"{PROFILE['label']}.\n"
            "Ta boucle de travail :\n"
            "1. get_progress → regarde combien il en manque\n"
            "2. S'il en manque : lancer_recherche(consigne) avec une consigne "
            "précise (angles non couverts, requêtes à varier, sources déjà "
            "rejetées à éviter)\n"
            "3. lancer_validation() pour trier les candidats\n"
            "4. Retour en 1. Quand l'objectif est atteint, réponds "
            "'COLLECTE TERMINÉE' avec un bilan court."
        ),
        # Garde-fou de la reboucle : ~4 tours LLM par round de collecte.
        max_steps=MAX_SEARCH_ROUNDS * 4 + 2,
        trace=trace,
    )

    @agent.tool
    def get_progress() -> dict:
        """Retourne l'état de la collecte : validés, manquants, URLs rejetées."""
        return {
            "objectif": TARGET_ARTICLES,
            "valides": len(state.validated),
            "manquants": state.missing(),
            "titres_valides": [a["title"] for a in state.validated],
            "urls_rejetees": sorted(state.rejected_urls),
        }

    @agent.tool
    def lancer_recherche(consigne: str) -> dict:
        """Délègue une recherche d'articles à l'agent chercheur.
        `consigne` : instructions précises (angles, requêtes à varier...)."""
        result = researcher.run(
            f"Trouve {state.missing() + 2} articles récents et fiables sur "
            f"{PROFILE['label']}. Consigne du superviseur : {consigne}. "
            f"Propose chaque article via propose_article."
        )
        return {
            "ok": True,
            "candidats_en_attente": len(state.candidates),
            "resume_chercheur": result.output[:300],
        }

    @agent.tool
    def lancer_validation() -> dict:
        """Délègue le tri des candidats à l'agent validateur."""
        if not state.candidates:
            return {"ok": False, "reason": "Aucun candidat à valider."}
        payload = json.dumps(state.candidates, ensure_ascii=False, indent=2)
        result = validator.run(
            f"Voici {len(state.candidates)} candidats. Accepte ou rejette "
            f"chacun (accept_article / reject_article) :\n{payload}"
        )
        return {
            "ok": True,
            "valides": len(state.validated),
            "manquants": state.missing(),
            "resume_validateur": result.output[:300],
        }

    return agent


def collect_articles(trace: TraceEmitter) -> None:
    """La reboucle est exécutée PAR l'agent superviseur : un seul appel
    supervisor.run(), dans lequel son propre run_messages enchaîne
    progress → recherche → validation → progress... Le host ne boucle pas.
    Garde-fou : le max_steps du superviseur (MaxStepsExceeded si dépassé)."""
    researcher = build_researcher(trace)
    validator = build_validator(trace)
    supervisor = build_supervisor(trace, researcher, validator)

    try:
        result = supervisor.run(
            f"Lance la collecte : obtiens {TARGET_ARTICLES} articles validés "
            f"sur {PROFILE['label']}."
        )
        print(f"\n[superviseur] {result.output}")
    except MaxStepsExceeded:
        print("⚠ Le superviseur a épuisé son budget de tours (max_steps).")

    if state.missing() > 0:
        raise RuntimeError(
            f"Impossible d'atteindre {TARGET_ARTICLES} articles validés "
            f"après {MAX_SEARCH_ROUNDS} rounds ({len(state.validated)} obtenus)."
        )
    print(f"\n✔ {len(state.validated)} articles validés.")


# ─────────────────────────────────────────────────────────────────────────────
# 4. ÉTAPE 4 — Reformatage : normalisation en structure homogène
# ─────────────────────────────────────────────────────────────────────────────

def reformat_articles(trace: TraceEmitter) -> None:
    agent = Agent.from_model(
        PROVIDER, MODEL,
        system_prompt=(
            "Tu normalises des fiches d'articles. Pour chaque article, appelle "
            "save_formatted avec un titre propre, la source (domaine de l'URL), "
            "3 points clés, et le résumé réécrit en français neutre."
        ),
        max_steps=TARGET_ARTICLES + 4,
        trace=trace,
    )

    @agent.tool
    def save_formatted(url: str, title: str, source: str,
                       key_points: list[str], summary: str) -> dict:
        """Sauvegarde la fiche normalisée d'un article."""
        state.formatted.append({
            "url": url, "title": title, "source": source,
            "key_points": key_points, "summary": summary,
        })
        return {"ok": True, "formatted": len(state.formatted)}

    payload = json.dumps(state.validated, ensure_ascii=False, indent=2)
    agent.run(f"Reformate ces {len(state.validated)} articles :\n{payload}")
    print(f"✔ {len(state.formatted)} fiches normalisées.")


# ─────────────────────────────────────────────────────────────────────────────
# 5. ÉTAPE 5+6 — Rédaction + validation du ton (boucle de révision hôte)
#    post_turn_hook (§4.7) : force le rédacteur à appeler save_draft.
# ─────────────────────────────────────────────────────────────────────────────

def must_save_draft(ctx: AgentTurnContext) -> Message | None:
    if not any(tc.name == "save_draft" for tc in ctx.tool_calls):
        return Message(
            role="user",
            content="Tu n'as pas appelé save_draft. Sauvegarde l'article complet.",
        )
    return None


def write_and_review(trace: TraceEmitter) -> None:
    writer = Agent.from_model(
        PROVIDER, MODEL,
        system_prompt=(
            f"Tu es un rédacteur web spécialisé dans {PROFILE['label']}. "
            "Tu écris en français, ton informatif et accessible, 800-1200 mots, "
            "structuré en intro / 3-4 sections / conclusion. Tu cites tes sources "
            "en fin d'article. Quand l'article est prêt, appelle save_draft."
        ),
        max_steps=8,
        post_turn_hook=must_save_draft,
        max_corrections_per_run=1,
        trace=trace,
    )

    @writer.tool
    def save_draft(markdown: str) -> dict:
        """Sauvegarde l'article rédigé (markdown complet)."""
        state.draft = markdown
        return {"ok": True, "chars": len(markdown)}

    reviewer = Agent.from_model(
        PROVIDER, MODEL,
        system_prompt=(
            "Tu es relecteur en chef. Tu évalues le TON d'un article : "
            "clarté, neutralité, accessibilité, absence de sensationnalisme. "
            "Réponds UNIQUEMENT en JSON : "
            '{"verdict": "ok" | "reviser", "problemes": ["..."], "consignes": "..."}'
        ),
        max_steps=2,
        trace=trace,
    )

    sources = json.dumps(state.formatted, ensure_ascii=False, indent=2)
    writer.run(
        f"Rédige un article de synthèse sur {PROFILE['label']} à partir de ces "
        f"{len(state.formatted)} sources :\n{sources}"
    )

    for revision in range(MAX_REVISIONS + 1):
        review = reviewer.run(f"Évalue le ton de cet article :\n\n{state.draft}")
        try:
            verdict = json.loads(review.output.replace("```json", "").replace("```", "").strip())
        except json.JSONDecodeError:
            print("⚠ Relecteur : sortie non-JSON, on considère l'article accepté.")
            break
        if verdict.get("verdict") == "ok":
            print(f"✔ Ton validé (après {revision} révision(s)).")
            break
        if revision == MAX_REVISIONS:
            print("⚠ Max révisions atteint, on garde la dernière version.")
            break
        print(f"↻ Révision {revision + 1} : {verdict.get('problemes')}")
        writer.run(
            "Révise l'article ci-dessous selon ces consignes du relecteur : "
            f"{verdict.get('consignes')}\n\nProblèmes relevés : "
            f"{verdict.get('problemes')}\n\nArticle actuel :\n{state.draft}\n\n"
            "Sauvegarde la version corrigée via save_draft."
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. ÉTAPE 7 — SEO : métadonnées en JSON strict
# ─────────────────────────────────────────────────────────────────────────────

def optimize_seo(trace: TraceEmitter) -> None:
    agent = Agent.from_model(
        PROVIDER, MODEL,
        system_prompt=(
            "Tu es consultant SEO. À partir d'un article, appelle save_seo avec : "
            "un title (< 60 car.), une meta_description (< 155 car.), un slug, "
            "un h1, et 5 mots-clés."
        ),
        max_steps=4,
        trace=trace,
    )

    @agent.tool
    def save_seo(title: str, meta_description: str, slug: str,
                 h1: str, keywords: list[str]) -> dict:
        """Sauvegarde les métadonnées SEO de l'article."""
        state.seo = {
            "title": title, "meta_description": meta_description,
            "slug": slug, "h1": h1, "keywords": keywords,
        }
        return {"ok": True}

    agent.run(f"Optimise le SEO de cet article :\n\n{state.draft}")
    print(f"✔ SEO : {state.seo.get('title', '?')}")


# ─────────────────────────────────────────────────────────────────────────────
# 7. ÉTAPE 8 — Dev front : ProjectWorkspace borné (§9), extensions web only
# ─────────────────────────────────────────────────────────────────────────────

def build_site(trace: TraceEmitter) -> None:
    workspace = ProjectWorkspace(
        SITE_DIR,
        allowed_write_extensions={".html", ".css", ".js"},
    )

    agent = Agent.from_model(
        PROVIDER, MODEL,
        system_prompt=(
            "Tu es développeur front-end. Tu crées une page web statique, "
            "propre et responsive (HTML sémantique + CSS dans un fichier séparé), "
            "qui présente un article avec ses métadonnées SEO dans le <head>. "
            "Utilise write_file pour créer index.html et style.css."
        ),
        max_steps=10,
        trace=trace,
    )

    @agent.tool
    def list_files(subdir: str = "") -> dict:
        """Liste les fichiers du workspace du site."""
        return {"files": workspace.list_files(subdir)}

    @agent.tool(permissions=["filesystem.write"])
    def write_file(path: str, content: str, reason: str = "") -> dict:
        """Écrit un fichier dans le workspace du site (html/css/js uniquement)."""
        return workspace.write_file(path, content, reason)

    @agent.tool(permissions=["filesystem.write"])
    def rollback() -> dict:
        """Annule la dernière écriture."""
        return workspace.rollback_last_change()

    payload = json.dumps(state.seo, ensure_ascii=False)
    agent.run(
        f"Crée le site pour cet article.\n\nMétadonnées SEO : {payload}\n\n"
        f"Article (markdown à convertir en HTML) :\n{state.draft}"
    )
    print(f"✔ Site généré dans {SITE_DIR.resolve()}")


# ─────────────────────────────────────────────────────────────────────────────
# 8. Main — le pipeline complet, observé par un TraceEmitter (§4.5)
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Pipeline éditorial — domaine : {DOMAIN}")
    with TraceEmitter(file=f"pipeline_{DOMAIN}.jsonl") as trace:
        collect_articles(trace)      # étapes 1-3 : recherche + validation + reboucle
        reformat_articles(trace)     # étape 4
        write_and_review(trace)      # étapes 5-6 : rédaction + validation du ton
        optimize_seo(trace)          # étape 7
        build_site(trace)            # étape 8

    Path(f"resultat_{DOMAIN}.json").write_text(
        json.dumps(
            {"validated": state.validated, "seo": state.seo, "draft": state.draft},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

# ==========================================
    # INSPECTION ET SÉCURISATION DU PIPELINESTATE
    # ==========================================
    import os
    
    dossier_site = os.path.join(os.path.dirname(__file__), "site")
    os.makedirs(dossier_site, exist_ok=True)
    
    # 1. On liste TOUT ce qui se cache dans l'objet state pour trouver l'article
    print("\n--- [DIAGNOSTIC DU STATE] ---")
    attributs_disponibles = [a for a in dir(state) if not a.startswith('_')]
    print("Attributs trouvés dans 'state' :", attributs_disponibles)
    
    # Si le state a un dictionnaire interne, on l'affiche aussi
    contenu_interne = {}
    if hasattr(state, "data"): contenu_interne = state.data
    elif hasattr(state, "_data"): contenu_interne = state._data
    elif hasattr(state, "variables"): contenu_interne = state.variables
    
    if contenu_interne:
        print("Clés trouvées à l'intérieur :", list(contenu_interne.keys()))
    print("-----------------------------\n")

    # 2. Extraction automatique intelligente
    html_final = None
    
    # On cherche d'abord dans le dictionnaire interne (le plus probable)
    for cle in ["html_code", "html_content", "web_content", "article_final", "article", "content", "draft"]:
        if contenu_interne and cle in contenu_interne:
            html_final = contenu_interne[cle]
            print(f"[🔧 Code Hôte] Article trouvé dans la clé interne : '{cle}'")
            break
        if hasattr(state, cle) and getattr(state, cle):
            html_final = getattr(state, cle)
            print(f"[🔧 Code Hôte] Article trouvé dans l'attribut : '{cle}'")
            break

    # 3. Si on n'a toujours rien trouvé, on prend la dernière variable textuelle text-heavy du state
    if not html_final and contenu_interne:
        for k, v in contenu_interne.items():
            if isinstance(v, str) and len(v) > 500: # Un texte long est forcément notre article
                html_final = v
                print(f"[🔧 Code Hôte] Article détecté par sa longueur dans la clé : '{k}'")
                break

    # Fallback si le state est vraiment introuvable
    if not html_final:
        html_final = f"<h1>Oups ! L'article n'a pas pu être extrait</h1><p>Attributs inspectés : {attributs_disponibles}</p>"

    # 4. Enrobage HTML standard si nécessaire
    if "<html" not in str(html_final).lower():
        html_final = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <title>Article Éditorial Sport</title>
    <style>
        body {{ font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.7; max-width: 800px; margin: 40px auto; padding: 0 20px; color: #333; }}
        h1 {{ color: #111; border-bottom: 2px solid #e67e22; padding-bottom: 10px; }}
        h2 {{ color: #d35400; margin-top: 30px; }}
        p {{ margin-bottom: 1.5em; text-align: justify; }}
    </style>
</head>
<body>
    {html_final}
</body>
</html>"""

    # 5. Écriture finale
    chemin_html = os.path.join(dossier_site, "index.html")
    with open(chemin_html, "w", encoding="utf-8") as f:
        f.write(str(html_final))
        
    print(f"[🔧 Code Hôte] Fichier index.html mis à jour dans : {chemin_html}")

    print("\n✔ Pipeline terminé.")


if __name__ == "__main__":
    main()
