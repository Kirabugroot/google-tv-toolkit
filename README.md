# google-tv-toolkit

Tout ce que j'ai mis en place autour de ma Chromecast (Google TV) : la config adb, les applis installées, deux commandes pour envoyer des trucs sur la télé depuis le Mac, et une intégration Discord (statut "Regarde ...", historique dans un salon, bot télécommande).

Testé sur une Chromecast HD (Android 14) et un Mac. Ça doit marcher sur n'importe quel boîtier Android TV / Google TV, à part deux ou trois détails indiqués plus bas.

## Sommaire

- [Connexion adb](#connexion-adb)
- [Applis installées sur la télé](#applis-installées-sur-la-télé)
- [Libérer de la place](#libérer-de-la-place)
- [Commandes Mac](#commandes-mac)
- [Discord](#discord)
- [Limites](#limites)

## Connexion adb

Sur le Mac :

```bash
brew install --cask android-platform-tools
```

Sur la télé :

1. Paramètres > Système > À propos, 7 appuis sur "Build du système d'exploitation Android TV"
2. Paramètres > Système > Options pour les développeurs > activer "Débogage USB" et "Débogage sans fil"
3. Dans "Débogage sans fil", noter l'IP et le port, puis "Associer l'appareil avec un code d'association"

L'association se fait une seule fois, avec le port et le code affichés dans la fenêtre d'association (ce n'est pas le même port que celui de connexion) :

```bash
adb pair 192.168.1.50:36981 123456
adb connect 192.168.1.50:35147
adb devices -l
```

Le port de connexion change à chaque redémarrage de la Chromecast. Pas besoin de refaire l'association, il suffit de le retrouver :

```bash
adb mdns services
```

## Applis installées sur la télé

| Appli | Source | Pourquoi |
|---|---|---|
| SmartTube | [GitHub](https://github.com/yuliskov/SmartTube/releases) | YouTube sans pub, SponsorBlock |
| Stremio | Play Store | centre multimédia |

SmartTube n'est pas sur le Play Store, il faut prendre l'APK qui correspond au processeur :

```bash
adb shell getprop ro.product.cpu.abilist
adb install SmartTube_stable_xx.xx_armeabi-v7a.apk
```

La Chromecast HD est en 32 bits (`armeabi-v7a`), la plupart des autres boîtiers sont en `arm64-v8a`.

Pour une appli du Play Store, on peut ouvrir sa fiche directement sur la télé et cliquer sur Installer à la télécommande :

```bash
adb shell am start -a android.intent.action.VIEW -d "market://details?id=com.stremio.one"
```

## Libérer de la place

La Chromecast HD n'a que 4 Go utilisables, c'est vite plein.

```bash
adb shell df -h /data
adb shell pm trim-caches 4G
adb shell pm list packages -3
adb uninstall com.exemple.appli
```

`pm trim-caches` vide le cache de toutes les applis sans toucher aux comptes ni aux réglages. Chez moi ça a rendu environ 200 Mo.

## Commandes Mac

```bash
./install.sh
echo 192.168.1.50 > ~/.config/tvpresence/tv_ip
```

`install.sh` installe adb, scrcpy et catt s'ils manquent, crée le venv Python, et met `tvcast` et `tvscreen` dans `~/.local/bin`.

### tvcast

Envoie un lien sur la télé. Sans argument, prend le lien du presse-papier.

```bash
tvcast https://www.youtube.com/watch?v=...
tvcast ~/Downloads/video.mp4
```

- lien YouTube : ouvert dans SmartTube via adb
- autre lien : `catt` (yt-dlp) cherche la vidéo dans la page et la caste
- pas de vidéo dans la page : la page elle-même est affichée

### tvscreen

Ouvre l'écran de la télé dans une fenêtre du Mac avec scrcpy, souris et clavier compris. Pratique pour taper une recherche ou configurer une appli sans la télécommande.

## Discord

Trois choses, qui tournent dans le même processus (`discord/tvbot.py`) lancé au login par launchd :

**Statut.** Le script regarde toutes les 15 secondes ce qui est au premier plan sur la télé et met à jour la Rich Presence : "Regarde Night Call (2014)", avec l'affiche, la progression et des boutons bande-annonce / fiche IMDb. Le titre vient de la session média Android quand l'appli en publie une. Pour IPTV Smarters, qui n'en publie pas, il est lu dans la barre d'infos du lecteur avec `uiautomator dump`. Les affiches viennent d'IMDb pour les films, de TVmaze pour les séries (avec le titre de l'épisode), et de la miniature pour YouTube.

**Historique.** Chaque titre est posté dans un salon via un webhook. Le message passe de "en cours" à "terminé, regardé 1h52" quand on arrête. `tvrecap.py` poste un récap le dimanche à 21h (temps total, top 3, appli la plus utilisée).

**Bot télécommande.** `/tv panneau` affiche une télécommande dans le salon : lecture, flèches, volume, lancement d'appli, capture d'écran, cast d'un lien, saisie de texte. Seul le propriétaire de l'application Discord peut s'en servir. Il y a aussi `/tv cast` et `/tv capture`.

### Configuration

Créer une application sur le [portail développeur Discord](https://discord.com/developers/applications), puis remplir `~/.config/tvpresence/` avec un fichier par valeur :

| Fichier | Contenu | Obligatoire |
|---|---|---|
| `tv_ip` | IP de la télé | oui |
| `client_id` | Application ID | oui |
| `webhook_url` | URL du webhook du salon | non, sans lui pas d'historique |
| `bot_token` | token du bot | non, sans lui pas de télécommande |
| `tv_name` | nom affiché sur le panneau | non |

```bash
chmod 600 ~/.config/tvpresence/*
```

Pour inviter le bot (remplacer `CLIENT_ID`) :

```
https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=248832
```

Si un rôle `cinéma` existe sur le serveur, le bot le mentionne quand un film démarre.

Les logs sont dans `discord/tvbot.log`. Pour relancer après un changement :

```bash
launchctl kickstart -k gui/$(id -u)/tv.toolkit.bot
```

## Limites

- Le statut Discord passe par l'appli de bureau : il faut que le Mac soit allumé avec Discord ouvert. L'historique et le bot marchent sans.
- Smarters n'affiche sa barre d'infos que quelques secondes. Si le titre n'a pas été attrapé au lancement, un appui sur OK suffit.
- Netflix, Disney+ et Prime sortent en noir dans les captures et dans scrcpy (DRM).
- Sur la Chromecast HD, la vidéo est rendue sur une couche matérielle : les captures ne montrent que l'interface et les sous-titres, pas l'image. Passer le lecteur VLC de Smarters en "Decoding Acceleration" la rend capturable, mais la lecture se met à saccader. Du coup pas de stream de l'écran de la télé dans un vocal.
- Android peut couper le débogage sans fil après un redémarrage. Il faut alors le réactiver dans les options développeur.
