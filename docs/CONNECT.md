# SaveFamily Connect backend

*[Français ci-dessous](#backend-savefamily-connect)*

## Classic and Connect

SaveFamily runs two separate platforms, each with its own app:

| | Classic | Connect |
|---|---|---|
| App | SaveFamily Classic (`com.tgelec.savefamily`) | SaveFamily Connect (`com.savefamily.app`, Grupo SaveFamily SL) |
| Backend | LinksField/tgelec, `*.myaqsh.com` | `https://api-platform.pro.savefamily.app/gateway/api` |
| Protocol | Mutual TLS, AES-CBC envelope, signed form parameters | Plain JSON over HTTPS |
| Login | Account + password, per region | Email + password, then a code sent by email |

A watch belongs to one platform only. A Connect watch cannot be reached through the Classic client: every `myaqsh.com` region rejects the account, and the Classic app itself says the watch works with the newer app. That is why the config flow starts by asking which app the watch uses.

## How the Connect API was mapped

The Connect app is written in Flutter (Dart compiled ahead of time in `libapp.so`), with no certificate pinning, no mutual TLS and no bundled client certificate. The endpoint paths come from the strings in the app binary and from the JavaScript of the SaveFamily web portal (`portal.savefamily.app`), which calls the same API. Every request below was then checked live against a real account and watch.

## Authentication

1. `POST /auth/login` with `{email, password}` returns a short-lived 2FA session `token` and the `availableMethods` (on the account tested: `EMAIL`, the default).
2. `POST /auth/2fa/request-code` with `{token, methodType}` emails a code. The response body is a bare `OK`, not JSON.
3. `POST /auth/twofa/login` with `{token, code, methodType, platform: "web", rememberMe: true}` returns `{accessToken, refreshToken, tokenType, expiresIn, sessionExpiresAt}`. `platform` must be lowercase `web`; `WEB` is rejected with 403.
4. Authenticated requests send the access token as the `token` cookie. The gateway ignores an `Authorization: Bearer` header on its own; the integration sends both.
5. `POST /auth/refresh` with the JSON body `{refreshToken}` returns the same shape as step 3. Without that body the server answers 500.

The access token lasts 15 minutes (`expiresIn: 900`). The refresh token is **rotated on every refresh**, each time with a new 30-day window. So after one login with the emailed code, the integration keeps the session alive indefinitely, as long as Home Assistant keeps polling and stores each new refresh token. If Home Assistant is off for more than 30 days, or the refresh token is rejected, the entry asks to reauthenticate: password, then a new emailed code. The server answers a rejected refresh token with HTTP 401; the integration treats any 4xx other than 408 and 429 the same way. The server does not invalidate a refresh token when it rotates it: in tests, a token already used still refreshed 10 minutes after the rotation, and reusing it did not end the session. A token that had never been used was refused a few minutes after a newer one had been issued from an older token, so the integration always stores the newest one at once. A refresh and the storage of its new token run together and are not interrupted by an unload or a shutdown, which wait for them.

The account API key feature (`/users/{id}/api-key`) is reserved for admin roles; a normal family account gets 401, so it cannot replace the login above.

## Data

- `GET /devices/` returns `{total, items, page, pages}`; `?page=N` selects a page, and the integration reads them all. Each item has `id` (UUID, used in other paths), `identificator` (the number shown in the app), `imei`, `battery`, `carrierName` (the child's name), `type` (model, e.g. `savewatch-2`), `protocol`, `lastConnection` (epoch ms), plus `settings`, `capabilities` and `flags`.
- `GET /devices/{id}/positions/latest` returns `{item}` with `latitude`, `longitude`, `hpe` (accuracy in metres), `steps`, `type` (`GPS`, `WIFI`, …), `positionDate` (epoch ms), `address`, and `networks` (nearby Wi-Fi SSIDs and BSSIDs).
- Device `flags`: `isDisconnect`, `isBatteryLow`, `isInOrOut` (`in`/`out` of a safe zone). Device `settings`: `frequency` (location reporting interval, seconds), `soundMode`, plus volume, language, fall detection and others. `capabilities.commands.types` lists the commands the watch accepts (47 on a SaveWatch 2).
- `GET /devices/{id}/alerts` returns `{total, items, page, pages}`; each alert has `type` (`sos`, `lowBattery`, `geofenceIn`, `geofenceOut`, `disconnect`, `reconnect`), `createdAt` and `reportedAt` (epoch ms) and `deviceName`.
- List endpoints take the portal's criteria as query parameters: `page`, `pageSize`, and `orderBy` / `filters` as base64-encoded JSON, e.g. `orderBy` = base64 of `[{"field": "createdAt", "sortDirection": "DESC"}]`. Without `orderBy`, lists come oldest first.
- `GET /devices/{id}/contact-lists` returns the watch's contact lists (`emergency`, `secondary`, `app-contacts`); `GET /devices/{id}/geofences` returns its safe zones.
- `POST /commands` with `{device: <identificator>, command: <name>, data: {}}` sends a command, e.g. `POSITION_REQUEST`, `FIND_DEVICE`, `RESTART`, `SHUTDOWN`.
- `GET /devices/{id}/positions` returns the position history (not used yet).
- `wss://api-platform.pro.savefamily.app/websocket` authenticates with the same `token` cookie and greets with `{"type": "connected"}`. It then pushes notices such as `position-processed`, `steps-processed`, `heartbeats-processed`, `oxygen-processed` and `photo-created`, each with the watch's `deviceIdentificator` and no other data the integration needs, and alert notices such as `sos` without it.
- The web portal also calls endpoints for Do Not Disturb, watch alarm clocks, contacts, safe zones, photos, heart rate, oxygen, call history and installed apps (not used yet).

## In the integration

- `custom_components/savefamily/connect/client.py` implements the steps above and maps devices, positions and alerts to the same `SaveFamilyWatch` / `SaveFamilyWatchState` / `SaveFamilyAlarm` dataclasses as Classic, so the device tracker, battery, steps, last fix, stale-location, online and alarm entities work unchanged. The watch's device id is its `identificator`.
- Connect adds battery-low, in-safe-zone (disabled by default), last-connection, location-reporting-interval and sound-mode entities, and the online sensor follows the server's disconnect flag instead of the age of the last fix.
- Alerts become `savefamily_alarm` events and fire the alarm event entity, newest 20 per poll; alerts already present at startup are not replayed. Alerts have no id: the integration derives one from `createdAt` and the type, so two alerts created in the same millisecond both fire. `disconnect` and `reconnect` are new event types; the alarm message stays empty because the alert only carries the child's name.
- The refresh location, find watch, restart and power off buttons send `POSITION_REQUEST`, `FIND_DEVICE`, `RESTART` and `SHUTDOWN`. A button is available only while the watch lists the command in its capabilities, and while the server reports the watch disconnected a press shows the offline notification instead of sending.
- `connect/push.py` keeps the websocket open and polls as soon as a `position-processed` or `steps-processed` notice names a known watch, or an alert notice (`sos`, `lowBattery`, ...) arrives; other notices (heart rate, oxygen, photos) are ignored and the 5-minute poll remains the fallback. It reconnects with a backoff up to 60 s, reset once a connection has stayed open for a minute. When the handshake is refused it refreshes the token once; if the fresh token is refused too, or the session needs a new login, it waits 15 minutes between attempts. While a new login is pending, notices trigger no poll.
- The SOS numbers and safe zones sensors show counts only (emergency contacts and geofences), read every 30 minutes. Contact names and numbers, and zone places, are not kept.
- The config entry stores the email, `backend: connect` and the current refresh token. **The password is not stored**; the setup form keeps it in memory only so that "Send me a new code" can request another code if the first one expires. The entry data is updated on every token rotation, so Connect entries do not reload when their data changes.
- A 401/403 on the device list triggers one token refresh and, if it persists, a reauthentication. A 401/403 on one watch's position does not: that watch keeps its last known state and the others still update.
- `networks`, `address` and `message` are dropped from positions before they reach Home Assistant, so nearby Wi-Fi networks never end up in entity attributes or the recorder. A latest position that is missing, has no coordinates or is at (0, 0) keeps the previous one, as on Classic.
- Connect entries load `device_tracker`, `sensor`, `binary_sensor`, `event` and `button`. The Classic-only actions (Do Not Disturb, SOS numbers, call back, position history), the work mode selector, the take-photo button and the photo wall are not available on Connect yet; the actions answer with a clear "not supported on SaveFamily Connect" error. An action whose target reaches Connect watches only through an area, floor or label skips them; naming a Connect watch, or reaching only Connect watches, gives that error.

## Not done yet

- Writes: Do Not Disturb, watch alarm clocks, contacts, safe zones, reporting interval, sound mode.
- Photos (take photo, latest photo), heart rate and oxygen, position history action.
- The other watch commands, e.g. `SPY_CALL` (listen in) and `REQUEST_HEART_RATE`.

---

# Backend SaveFamily Connect

## Classic et Connect

SaveFamily exploite deux plateformes distinctes, chacune avec son application :

| | Classic | Connect |
|---|---|---|
| Application | SaveFamily Classic (`com.tgelec.savefamily`) | SaveFamily Connect (`com.savefamily.app`, Grupo SaveFamily SL) |
| Backend | LinksField/tgelec, `*.myaqsh.com` | `https://api-platform.pro.savefamily.app/gateway/api` |
| Protocole | TLS mutuel, enveloppe AES-CBC, paramètres de formulaire signés | JSON simple sur HTTPS |
| Connexion | Compte + mot de passe, par région | Email + mot de passe, puis un code envoyé par email |

Une montre n'appartient qu'à une seule plateforme. Une montre Connect est inaccessible au client Classic : toutes les régions `myaqsh.com` refusent le compte, et l'application Classic indique elle-même que la montre fonctionne avec la nouvelle application. C'est pourquoi la configuration commence par demander quelle application utilise la montre.

## Comment l'API Connect a été cartographiée

L'application Connect est écrite en Flutter (Dart compilé à l'avance dans `libapp.so`), sans épinglage de certificat, sans TLS mutuel et sans certificat client embarqué. Les chemins des points d'accès proviennent des chaînes du binaire de l'application et du JavaScript du portail web SaveFamily (`portal.savefamily.app`), qui appelle la même API. Chaque requête ci-dessous a ensuite été vérifiée en direct avec un vrai compte et une vraie montre.

## Authentification

1. `POST /auth/login` avec `{email, password}` renvoie un `token` de session 2FA de courte durée et les `availableMethods` (sur le compte testé : `EMAIL`, par défaut).
2. `POST /auth/2fa/request-code` avec `{token, methodType}` envoie un code par email. Le corps de la réponse est un simple `OK`, pas du JSON.
3. `POST /auth/twofa/login` avec `{token, code, methodType, platform: "web", rememberMe: true}` renvoie `{accessToken, refreshToken, tokenType, expiresIn, sessionExpiresAt}`. `platform` doit être `web` en minuscules ; `WEB` est refusé avec une 403.
4. Les requêtes authentifiées envoient le jeton d'accès dans le cookie `token`. La passerelle ignore un en-tête `Authorization: Bearer` seul ; l'intégration envoie les deux.
5. `POST /auth/refresh` avec le corps JSON `{refreshToken}` renvoie la même forme qu'à l'étape 3. Sans ce corps, le serveur répond 500.

Le jeton d'accès dure 15 minutes (`expiresIn: 900`). Le jeton de rafraîchissement **change à chaque rafraîchissement**, avec à chaque fois une nouvelle fenêtre de 30 jours. Après une seule connexion avec le code reçu par email, l'intégration maintient donc la session indéfiniment, tant que Home Assistant continue d'interroger le serveur et conserve chaque nouveau jeton. Si Home Assistant reste éteint plus de 30 jours, ou si le jeton est refusé, l'entrée demande une nouvelle authentification : mot de passe, puis un nouveau code par email. Le serveur répond à un jeton refusé par une HTTP 401 ; l'intégration traite de même toute 4xx autre que 408 et 429. Le serveur n'invalide pas un jeton quand il le renouvelle : lors des tests, un jeton déjà utilisé fonctionnait encore 10 minutes après le renouvellement, et le réutiliser ne mettait pas fin à la session. Un jeton jamais utilisé a été refusé quelques minutes après qu'un jeton plus récent a été émis à partir d'un plus ancien : l'intégration enregistre donc toujours le plus récent immédiatement. Un rafraîchissement et l'enregistrement de son nouveau jeton s'exécutent ensemble et ne sont pas interrompus par un déchargement ou un arrêt, qui les attendent.

La fonction de clé d'API du compte (`/users/{id}/api-key`) est réservée aux rôles administrateurs ; un compte familial normal reçoit une 401, elle ne peut donc pas remplacer la connexion ci-dessus.

## Données

- `GET /devices/` renvoie `{total, items, page, pages}` ; `?page=N` choisit une page, et l'intégration les lit toutes. Chaque élément contient `id` (UUID, utilisé dans les autres chemins), `identificator` (le numéro affiché dans l'application), `imei`, `battery`, `carrierName` (le prénom de l'enfant), `type` (modèle, par ex. `savewatch-2`), `protocol`, `lastConnection` (epoch en ms), ainsi que `settings`, `capabilities` et `flags`.
- `GET /devices/{id}/positions/latest` renvoie `{item}` avec `latitude`, `longitude`, `hpe` (précision en mètres), `steps`, `type` (`GPS`, `WIFI`, …), `positionDate` (epoch en ms), `address` et `networks` (SSID et BSSID des réseaux Wi-Fi proches).
- `flags` de l'appareil : `isDisconnect`, `isBatteryLow`, `isInOrOut` (`in`/`out` d'une zone de sécurité). `settings` de l'appareil : `frequency` (intervalle d'envoi de la position, en secondes), `soundMode`, ainsi que le volume, la langue, la détection de chute et d'autres. `capabilities.commands.types` liste les commandes acceptées par la montre (47 sur une SaveWatch 2).
- `GET /devices/{id}/alerts` renvoie `{total, items, page, pages}` ; chaque alerte a un `type` (`sos`, `lowBattery`, `geofenceIn`, `geofenceOut`, `disconnect`, `reconnect`), `createdAt` et `reportedAt` (epoch en ms) et `deviceName`.
- Les listes acceptent les critères du portail en paramètres de requête : `page`, `pageSize`, et `orderBy` / `filters` en JSON encodé en base64, par ex. `orderBy` = base64 de `[{"field": "createdAt", "sortDirection": "DESC"}]`. Sans `orderBy`, les listes commencent par le plus ancien.
- `GET /devices/{id}/contact-lists` renvoie les listes de contacts de la montre (`emergency`, `secondary`, `app-contacts`) ; `GET /devices/{id}/geofences` renvoie ses zones de sécurité.
- `POST /commands` avec `{device: <identificator>, command: <nom>, data: {}}` envoie une commande, par ex. `POSITION_REQUEST`, `FIND_DEVICE`, `RESTART`, `SHUTDOWN`.
- `GET /devices/{id}/positions` renvoie l'historique des positions (pas encore utilisé).
- `wss://api-platform.pro.savefamily.app/websocket` s'authentifie avec le même cookie `token` et salue avec `{"type": "connected"}`. Il pousse ensuite des avis comme `position-processed`, `steps-processed`, `heartbeats-processed`, `oxygen-processed` et `photo-created`, chacun avec le `deviceIdentificator` de la montre et aucune autre donnée utile à l'intégration, ainsi que des avis d'alerte comme `sos` sans cet identifiant.
- Le portail web appelle aussi des points d'accès pour Ne pas déranger, les réveils de la montre, les contacts, les zones de sécurité, les photos, la fréquence cardiaque, l'oxygène, l'historique des appels et les applications installées (pas encore utilisés).

## Dans l'intégration

- `custom_components/savefamily/connect/client.py` met en œuvre les étapes ci-dessus et convertit appareils, positions et alertes vers les mêmes dataclasses `SaveFamilyWatch` / `SaveFamilyWatchState` / `SaveFamilyAlarm` que Classic : le suivi de position, la batterie, les pas, la dernière position, la position périmée, l'état en ligne et l'entité d'alarme fonctionnent sans changement. L'identifiant de la montre est son `identificator`.
- Connect ajoute les entités batterie faible, dans une zone de sécurité (désactivée par défaut), dernière connexion, intervalle d'envoi de la position et mode sonore, et le capteur en ligne suit l'indicateur de déconnexion du serveur au lieu de l'âge de la dernière position.
- Les alertes deviennent des événements `savefamily_alarm` et déclenchent l'entité d'événement d'alarme, les 20 plus récentes à chaque interrogation ; les alertes déjà présentes au démarrage ne sont pas rejouées. Les alertes n'ont pas d'identifiant : l'intégration en dérive un de `createdAt` et du type, si bien que deux alertes créées à la même milliseconde se déclenchent toutes les deux. `disconnect` et `reconnect` sont de nouveaux types d'événement ; le message de l'alarme reste vide car l'alerte ne contient que le prénom de l'enfant.
- Les boutons actualiser la position, trouver la montre, redémarrer et éteindre envoient `POSITION_REQUEST`, `FIND_DEVICE`, `RESTART` et `SHUTDOWN`. Un bouton n'est disponible que tant que la montre liste la commande dans ses capacités, et tant que le serveur indique la montre déconnectée, un appui affiche la notification hors ligne au lieu d'envoyer la commande.
- `connect/push.py` garde le WebSocket ouvert et interroge le serveur dès qu'un avis `position-processed` ou `steps-processed` nomme une montre connue, ou qu'un avis d'alerte (`sos`, `lowBattery`, ...) arrive ; les autres avis (fréquence cardiaque, oxygène, photos) sont ignorés et l'interrogation toutes les 5 minutes reste la solution de repli. Il se reconnecte avec un délai croissant jusqu'à 60 s, remis à zéro quand une connexion est restée ouverte une minute. Quand la connexion est refusée, il rafraîchit le jeton une fois ; si le nouveau jeton est refusé aussi, ou si la session exige une nouvelle connexion, il attend 15 minutes entre deux essais. Tant qu'une nouvelle connexion est en attente, les avis ne déclenchent pas d'interrogation.
- Les capteurs numéros SOS et zones de sécurité n'affichent que des nombres (contacts d'urgence et zones), lus toutes les 30 minutes. Les noms et numéros des contacts, et les lieux des zones, ne sont pas conservés.
- L'entrée de configuration conserve l'email, `backend: connect` et le jeton de rafraîchissement courant. **Le mot de passe n'est pas conservé** ; le formulaire de configuration le garde seulement en mémoire pour que « M'envoyer un nouveau code » puisse demander un autre code si le premier expire. Les données de l'entrée sont mises à jour à chaque rotation du jeton ; les entrées Connect ne se rechargent donc pas quand leurs données changent.
- Une 401/403 sur la liste des appareils déclenche un rafraîchissement du jeton et, si elle persiste, une nouvelle authentification. Une 401/403 sur la position d'une montre, non : cette montre garde son dernier état connu et les autres continuent d'être mises à jour.
- `networks`, `address` et `message` sont retirés des positions avant d'arriver dans Home Assistant : les réseaux Wi-Fi proches ne se retrouvent jamais dans les attributs des entités ni dans l'historique. Une dernière position absente, sans coordonnées ou en (0, 0) garde la précédente, comme sur Classic.
- Les entrées Connect chargent `device_tracker`, `sensor`, `binary_sensor`, `event` et `button`. Les actions propres à Classic (Ne pas déranger, numéros SOS, rappel, historique des positions), le sélecteur de mode de fonctionnement, le bouton de prise de photo et le mur de photos ne sont pas encore disponibles sur Connect ; les actions répondent par une erreur claire « non pris en charge sur SaveFamily Connect ». Une action dont la cible n'atteint des montres Connect que par une pièce, un étage ou une étiquette les ignore ; nommer une montre Connect, ou n'atteindre que des montres Connect, donne cette erreur.

## Reste à faire

- Écritures : Ne pas déranger, réveils de la montre, contacts, zones de sécurité, intervalle d'envoi, mode sonore.
- Photos (prendre une photo, dernière photo), fréquence cardiaque et oxygène, action d'historique des positions.
- Les autres commandes de la montre, par ex. `SPY_CALL` (écoute) et `REQUEST_HEART_RATE`.
