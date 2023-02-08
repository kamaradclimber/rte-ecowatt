# My EcoWatt by RTE pour Home Assistant

Composant pour exposer les niveaux Ecowatt dans un avenir prévisible. Voir https://www.monecowatt.fr/ pour l'accès web.

L'intégration expose également optionellement des informations sur les délestages locaux via le site d'Enedis.
Cette fonctionnalité est désactivée par défaut car elle partage des information de localisation avec un site tiers. Elle peut-être configurée dans la page /config/integrations en cliquant sur "Configurer".

## Installation

Utilisez [hacs](https://hacs.xyz/).
[![Ouvrez votre instance Home Assistant et ouvrez un référentiel dans la boutique communautaire Home Assistant.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kamaradclimber&repository=rte-ecowatt&category=integration)

## Configuration

### Obtenir un accès API pour les API RTE

- Créer un compte sur [site API RTE](https://data.rte-france.com/web/guest)
- Inscrivez-vous à l'[API Ecowatt](https://data.rte-france.com/catalog/-/api/consumption/Ecowatt/v4.0) et cliquez sur "Abonnez-vous à l'API", créez un nouvelle application
- obtenir le `client_id` et `client_secret` (uuid dans les deux cas)

### Configurer home-assistant

La méthode de configuration préférée consiste à utiliser l'interface utilisateur.
![image info](/img/integration.png)

![image info](/img/credential.png)

![image info](//img/success.png)

![image info](/img/check.png)

Vous pouvez configurer deux types de capteurs :
- Capteurs "horaire" pour regarder le niveau écowatt X heures dans le futur. Vous pouvez regarder jusqu'à 96h pour le moment.
- Capteurs "journalier" pour examiner le niveau d'écowatt X jours dans le futur. Vous pouvez regarder jusqu'à 3d à l'avance pour le moment. La valeur du capteur "jours" est la pire de toutes les heures de ce jour.
![image info](/img/sensor.png)

Deux capteurs sont générés par défaut : "maintenant" (0 heure d'avance) et "aujourd'hui" (0 jour d'avance).

Un capteur supplémentaire exposant la prochaine période dégradée est également ajouté par défaut (non configurable). Il montre un début de période prochaine avec des tensions sur le réseau électrique. Pendant une telle période, il indique le début de l'heure suivante.
