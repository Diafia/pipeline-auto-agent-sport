# 🤖 Pipeline Éditorial Autonome (Multi-Agents)

Ce projet implémente un système multi-agents autonome capable de rechercher, valider, rédiger et optimiser des articles de fond de manière entièrement automatisée. L'architecture s'appuie sur la bibliothèque `autoagent` orchestrée par les modèles de génération **Gemini (Google)** et le moteur de recherche pour IA **Tavily**.

---

## 🏗️ Architecture des Agents

Le pipeline fait collaborer plusieurs rôles spécialisés :

1. **Superviseur** : Cadre le besoin et valide l'atteinte des objectifs.
2. **Chercheur (Scraper)** : Effectue des requêtes ciblées sur le web via Tavily API.
3. **Validateur de sources** : Filtre et vérifie la pertinence des articles trouvés.
4. **Normalisateur** : Transforme les articles en fiches d'information standardisées.
5. **Rédacteur (Writer)** : Rédige un article de fond de plus de 1500 mots.
6. **Critique / Correcteur** : Analyse le ton, la neutralité et corrige les erreurs factuelles (boucle de feedback).
7. **Expert SEO** : Génère les balises méta, titres et mots-clés optimisés.
8. **Développeur Front-End** : Met en page l'article final dans un rendu HTML/CSS propre.

---

## 🚀 Guide d'Exécution

### 1. Prérequis

Assurez-vous d'avoir **Python 3.11+** installé sur votre machine.

### 2. Installation et Configuration

1. Placez-vous dans le dossier du projet :
   ```bash
   cd "C:\Users\utilisateur\Desktop\NEXA Digital School\STAGE SYSTEMICS 2026\last_one\autoagent_lastone\exercice_auto_agent"
   ```
