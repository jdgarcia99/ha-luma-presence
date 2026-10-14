<p align="center">
  <img src="images/icon.png" alt="LumaPresence" width="128">
</p>

# LumaPresence

Presence simulation for Home Assistant that makes an empty house look lived in. LumaPresence switches lights on and off in a random but believable way, works alongside your alarm, and learns how your home is really used.

> **Status: phase 1 (v0.1.0).** Sun and Time modes plus learning data collection. Alarm integration, the night sequence, and the Replay and Dream modes arrive in phases 2 and 3.

## Installation (HACS)

1. In HACS, open the ⋮ menu → **Custom repositories**, add `https://github.com/jdgarcia99/ha-luma-presence` with category **Integration**.
2. Download **LumaPresence** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → LumaPresence**.

## Configuration

| Option | Purpose |
|---|---|
| Simulation lights | Lights and switches the simulation may control |
| Front lights | Subset visible from the street; a minimum of them is kept on |
| People | When nobody is home, learning pauses |
| Alarm panel | While the alarm is armed away, learning pauses |
| Learning margins | Hours discarded before an absence, and hours learning stays paused after returning |
| Learn only from human actions | Ignores changes made by automations and scripts |

All options can be changed later from the integration's **Configure** button.

## Entities

- `switch.luma_presence_simulation`: master switch. Its state is restored after a restart, and the simulation resumes if it is inside its window.
- `select.luma_presence_mode`: **Sun** (starts at sunset) or **Time** (starts at a fixed time).
- `select.luma_presence_stop_behavior`: what to do with the lights the simulation turned on when it is stopped manually: keep them on, turn them off at once, turn them off gradually, or keep them on only at night.
- Settings: minimum and maximum lights on, minimum front lights on, dwell times, switch-off interval, start, end and late end times, late arrival, and random start and end delays.
- Sensors: status, next event, last action, simulating, learning, and valid learning days.

The simulation always ends at sunrise at the latest. Scheduled ends turn the lights off gradually.

### Ownership rule

LumaPresence only turns off lights that it turned on itself and that nobody has touched since. If you come home and switch on a light, it will never turn it off.

## Learning

From day one, LumaPresence stores real light usage in `.storage`, per day and only while the house is occupied, so it does not depend on the recorder's retention.

Learning pauses while the simulation is enabled, the alarm is armed away, nobody is home, or it has been paused manually. Excluded days never count as days without activity: statistics are based only on observed time. After an absence longer than 48 hours, the whole day of the return is excluded.

## Services

- `luma_presence.start` / `luma_presence.stop`: turn the simulation on or off. `stop` accepts an optional `lights_behavior` that overrides the configured stop behaviour.
- `luma_presence.forget_period`: deletes everything learned between two dates.

## Translations

The user interface is available in English and Spanish. Contributions for other languages are welcome: add a file to `custom_components/luma_presence/translations/`.

## Roadmap

- **Phase 2**: alarm integration, night sequence, and Replay mode (replays your real usage from previous days).
- **Phase 3**: Dream mode (new, believable patterns generated from what has been learned, with optional AI), repairs and diagnostics.

## Development

```bash
pip install pytest-homeassistant-custom-component
pytest
```

## License

MIT
