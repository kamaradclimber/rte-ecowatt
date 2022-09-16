# My EcoWatt by RTE for Home Assistant

Component to expose Ecowatt levels for the foreseable future. See https://www.monecowatt.fr/ for web access.

## Installation

Use [hacs](https://hacs.xyz/).

## Configuration

### Get api access for RTE APIs

- Create an account on [RTE API website](https://data.rte-france.com/web/guest)
- Register to the [Ecowatt API](https://data.rte-france.com/catalog/-/api/consumption/Ecowatt/v4.0) and click on "Abonnez-vous à l'API", create a new application
- get the `client_id` and `client_secret`

### Configure home-assistant

Quick reference:
```
sensor:
  - platform: rte_ecowatt
    api_client_id: XXXX
    api_client_secret: YYYY
    sensors:
      - unit: days
        shift: 2
      - unit: days
        shift: 0
      - unit: hours
        shift: 7
      - unit: hours
        shift: 36
```

Each sensor is configured to look at ecowatt level in the future. You can configure X hours in advance (up to 96 at the moment) to get the precise level at a given point in time.
You can also configure X days in advance (up to 3 days) to get the worst level of that day.
