# Sous-titres traduits en direct pour Zoom (Gemini 3.5 Live Translate)

Le script écoute **votre micro** et **le son de Zoom** (les participants). Il envoie le son
en continu à **Gemini 3.5 Live Translate**, qui traduit au fil de la parole
(français ⇄ anglais par défaut), et affiche la traduction dans un bandeau en bas de votre
écran. Si vous partagez votre **écran entier** dans Zoom, tout le monde voit les
sous-titres sans rien installer.

```
Votre micro ─► anti-écho ─┐                     ┌─► session « vers français »  ─┐
                          ├─► chaque source ────┤                               ├─► bandeau
Son Zoom ─► BlackHole ────┘   (référence écho)  └─► session « vers anglais »  ──┘
```

Chaque phrase est envoyée à deux sessions Gemini, l'une vers le français, l'autre vers
l'anglais. Une session reste muette quand la phrase est déjà dans sa langue : il n'y a donc
qu'une seule traduction par phrase, dans la bonne direction.

---

## 1. Installer les outils (une seule fois)

1. **Python 3.11 ou plus récent**, depuis <https://www.python.org/downloads/macos/>.
   Prenez l'installeur officiel : il contient Tkinter, qui sert au bandeau.
2. **BlackHole**, un câble audio virtuel qui permet de récupérer le son de Zoom :
   `brew install blackhole-2ch`, ou téléchargez-le sur <https://existential.audio/blackhole/>.
   Redémarrez le Mac ensuite.
3. **Les dépendances du script.** Dans un Terminal, depuis le dossier du script :
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Une clé Gemini API** : créez-la sur <https://aistudio.google.com/apikey>, puis :
   ```bash
   export GEMINI_API_KEY="votre-clé"
   ```
   Pour ne pas la retaper à chaque fois, ajoutez cette ligne à `~/.zshrc`.
   **Activez la facturation sur le projet** (voir « Coût et confidentialité » plus bas).

## 2. Envoyer le son de Zoom vers BlackHole (une seule fois)

Le but est d'entendre les participants dans les haut-parleurs **et** d'envoyer leur voix au
script.

1. Ouvrez **Configuration audio et MIDI** (Applications → Utilitaires).
2. Cliquez sur **+** en bas à gauche → **Créer un périphérique à sorties multiples**.
3. Cochez **vos haut-parleurs** et **BlackHole 2ch**.
   Placez les haut-parleurs en premier et cochez « Correction de dérive » pour BlackHole.
4. Dans **Zoom → Paramètres → Audio → Haut-parleur**, choisissez
   **Périphérique à sorties multiples**. Ne changez pas le micro.

## 3. L'anti-écho (sans casque)

Sans casque, votre micro capte aussi la voix des participants qui sort des haut-parleurs.
Le script sait exactement quand un participant parle, puisque leur son arrive aussi par
BlackHole. Pendant ce temps, il **coupe le micro** côté traduction, avec une petite marge
avant et après pour couvrir la réverbération de la pièce. Zoom, lui, continue de recevoir
votre micro normalement.

La contrepartie : si vous parlez **en même temps** qu'un participant, vos mots prononcés
pendant ce chevauchement ne sont pas traduits. Pour les laisser passer quand votre voix
couvre nettement l'écho, ajoutez `--double-talk`. Le script apprend alors le niveau d'écho
de la pièce pendant les premières secondes où les participants parlent seuls.

**Vérifiez le réglage avant la réunion :**

```bash
python3 soustitres_zoom.py --list-devices   # repérez le nom de votre micro
python3 soustitres_zoom.py --test-audio     # niveaux et état de l'anti-écho
```

Faites parler quelqu'un dans Zoom, ou lancez une vidéo dans une réunion test :
- Le micro doit afficher **« COUPÉ (écho) »** pendant que la personne parle.
- Il doit afficher **« ouvert »** quand vous parlez seul.

Si de l'écho passe encore à la fin des phrases des participants, augmentez
`--echo-hold 0.8`. Si le micro se coupe alors que personne ne parle dans Zoom, montez
`--zoom-threshold-db -45`.

Au premier lancement, macOS demande l'accès au micro pour le Terminal : acceptez.
Si vous avez refusé, allez dans Réglages Système → Confidentialité et sécurité → Micro.

## 4. Pendant la réunion

```bash
source .venv/bin/activate
python3 soustitres_zoom.py --mic "MacBook" --save transcript.txt
```

- Le terminal affiche quatre lignes « ✓ Connecté à Gemini » (2 sources × 2 langues), puis
  le message « Sous-titres actifs — parlez pour tester » s'affiche en bas de l'écran.
- Dans Zoom, faites **Partager l'écran → Écran** (l'écran entier).
  Si vous partagez une seule fenêtre, le bandeau n'apparaîtra pas chez les participants.
- La traduction s'affiche **au fil de la parole**. Le texte original s'affiche en gris
  au-dessus.
- Vous pouvez déplacer le bandeau à la souris. **Double-cliquez dessus** ou faites **Ctrl+C**
  dans le Terminal pour l'arrêter.
- Chaque réplique terminée est écrite dans le Terminal et, avec `--save`, dans un fichier.
- Si Gemini coupe une session (c'est prévu de temps en temps), le script se reconnecte
  tout seul. Vous perdez au pire une ou deux secondes.

## Options utiles

| Besoin | Option |
|---|---|
| Autres langues (ex. espagnol et français) | `--langs fr,es` |
| Plus de deux langues dans la réunion | `--langs fr,en,es` (une session de plus par langue) |
| Texte plus grand ou plus petit | `--font-size 36` |
| Masquer le texte original (gris) | `--no-original` |
| Changer les noms affichés | `--mic-label "Marie" --zoom-label "Invité"` |
| Laisser passer votre voix quand vous coupez la parole | `--double-talk` |
| Avec un casque (anti-écho inutile) | `--no-echo-gate` |

Pour la liste complète : `python3 soustitres_zoom.py --help`.

## Coût et confidentialité

- **Le son de la réunion part chez Google** pour être traduit : c'est le principe de
  Gemini Live Translate. **Prévenez les participants** au début de la réunion.
- **Activez la facturation** sur votre projet Google AI Studio. Sur l'offre gratuite de
  l'API Gemini, Google peut utiliser les données pour améliorer ses produits. Sur l'offre
  payante, il ne le fait pas. Dans un cadre universitaire, prenez l'offre payante, ou
  demandez à la DSI si l'université a un accord avec Google Cloud (Vertex AI).
- **Coût** : Google ne publie pas encore de tarif clair pour ce modèle (il est en
  « preview »). D'après les tarifs affichés ailleurs, une heure de réunion devrait coûter
  quelques dollars. Surveillez la facturation lors des premières réunions.
- Le modèle est en **preview** : son nom ou son fonctionnement peut changer. Si la connexion
  échoue avec une erreur de modèle introuvable, passez le nouveau nom avec `--model`.

## Bonus : sous-titres natifs de Zoom, sans partage d'écran

Votre compte peut activer les **Sous-titres manuels**, qui acceptent un service de
sous-titrage tiers :

1. Sur le portail web Zoom, allez dans Paramètres → Réunion, puis activez **Sous-titres manuels**.
2. Pendant la réunion, ouvrez **Sous-titres → Outils de l'hôte**, choisissez
   « Utiliser un service tiers » et **copiez le jeton d'API**.
3. Lancez :
   ```bash
   python3 soustitres_zoom.py --zoom-token "https://wmcc.zoom.us/closedcaption?id=…"
   ```
   Ajoutez `--no-overlay` si vous ne voulez pas du bandeau.

Dans ce mode, Zoom reçoit chaque réplique quand elle est terminée, et non au fil de la
parole. Cette fonction n'a pas pu être testée sur un vrai compte Zoom.
