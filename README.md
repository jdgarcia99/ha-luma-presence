<p align="center">
  <img src="images/icon.png" alt="LumaPresence" width="128">
</p>

# LumaPresence

Presence simulation for Home Assistant that makes an empty house look lived in. LumaPresence switches lights on and off in a random but believable way, works alongside your alarm, and learns how your home is really used.

> **Status: phase 2 (v0.3.0).** Sun and Time modes, learning data collection and night sequences. The Replay mode comes next, and the Dream mode in phase 3.

## Installation (HACS)

1. In HACS, open the ⋮ menu → **Custom repositories**, add `https://github.com/jdgarcia99/ha-luma-presence` with category **Integration**.
2. Download **LumaPresence** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → LumaPresence**.

## Configuration

Setup walks through the same sections that the integration's **Configure** button later shows as a menu. In the menu, each section edits a working copy and returns to the menu; nothing is stored until you choose **Save**.

| Section | Options |
|---|---|
| Simulation lights | Lights and switches the simulation may control |
| Facades | Front lights and rear lights, chosen among the simulation lights |
| Learning | Indoor floors, indoor areas, extra entities, excluded entities, *Automatically exclude indicator LEDs*, and *Learn only from human actions* (ignores changes made by automations and scripts) |
| Learning preview | Read-only list of the entities that will be learned, and of the indicator LEDs left out automatically |
| People and alarm | People, alarm panel, and the learning margins: hours discarded before an absence, and hours learning stays paused after returning |
| Night sequences | Delay between the steps of a route and time spent at its destination (see [Night sequences](#night-sequences)) |
| Monitoring | Minutes a light may stay unavailable before an alert (see [Unavailable lights](#unavailable-lights)) |

### Facades

Each simulation light is a **front** light (visible from the street), a **rear** light (visible from the back of the house) or, by default, an **interior** light. While simulating, LumaPresence keeps at least *Minimum front lights on* and *Minimum rear lights on* lit, front first, as long as *Maximum lights on* allows it. A facade light is never switched off if that would leave its facade below its minimum, unless the house is above the maximum and there is no other light to switch off.

### Learning scope

LumaPresence learns from every real indoor light, not only from the simulation lights:

1. Every `light.*` entity in the selected floors and areas. An entity's own area wins; otherwise the area of its device is used.
2. Disabled and hidden entities, and configuration or diagnostic entities, are left out automatically. This removes status LEDs, access point LEDs and similar lights.
3. With *Automatically exclude indicator LEDs* (on by default), a light is also left out when its device has a `switch.*` entity: smart plugs and relays (ESPHome, Shelly…) often expose their status LED as a light on the same device as the plug. The **Learning preview** lists these indicator LEDs so you can spot a real lamp among them.
4. **Extra entities** are added, even if a rule above left them out. Use them for room lights exposed as `switch.*` relays, for lamps without an area, and for a lamp wrongly taken for an indicator LED.
5. The simulation lights are always added.
6. **Excluded entities** are removed. Use them for lights that are not room lights, such as fridge lights or Christmas lights.

The list is recomputed a few seconds after an entity, device, area or floor changes, and each learned event stores the area of the light.

Installations upgraded from v0.1 learn from every floor that exists at upgrade time; data learned before the upgrade is kept.

## Night sequences

During the night, every now and then, someone seems to get up, walk to the bathroom and go back to bed. Each walk follows a **route**.

### Routes

Add routes with **Add route** on the integration page; each one can be edited or deleted later. A route has:

| Field | Example |
|---|---|
| Name | Bathroom |
| Origin | Bedroom light |
| Path (optional, in walking order) | Hallway light |
| Destination | Bathroom light |

Route lights do not need to be simulation lights. The origin and the destination must be different, and a light can appear only once in a route.

A route turns on the origin, then each path light in order, then the destination, with a random step delay between lights. It stays at the destination for a while, then walks back: destination, path lights in reverse order, origin. Only the lights the route turned on are switched off at the end, so a light that was already on stays on.

### When sequences run

Night sequences run from *Sequence start* (`time.luma_presence_sequence_start_time`, 00:45 by default) until sunrise, only while both the master switch and *Night sequences* (`switch.luma_presence_sequences`, off by default) are on. The first route starts at *Sequence start*, plus up to 30 minutes when *Random start delay* is on; then a new route runs every *Minimum interval between sequences* to *Maximum interval between sequences* minutes (45 to 90 by default). Routes are picked at random, never the same one twice in a row when another can run, and routes whose origin or destination is unavailable are skipped.

The step delays (10 to 30 seconds by default) and the time at the destination (2 to 5 minutes by default) are set in the **Night sequences** section of **Configure**.

### Interaction with the simulation

- While a route runs, its lights are reserved: the simulation counts them for the minimum, maximum and facade limits, but never switches them.
- At sunrise, or when *Night sequences* is turned off, the running route stops and its lights are switched off gradually.
- When the master switch is turned off, the running route stops at once and its lights follow *Lights when stopping*, together with the simulation's lights.
- A route interrupted by a restart is not resumed; its lights are switched off.
- Learning ignores every light change made by LumaPresence, including routes.

To try a route at home, press **Test sequence** (`button.luma_presence_test_sequence`) or call `luma_presence.run_sequence`. It runs at once, even with the master switch off or outside the window, and does not pause learning.

## Entities

- `switch.luma_presence_simulation`: master switch. Its state is restored after a restart, and the simulation resumes if it is inside its window.
- `select.luma_presence_mode`: **Sun** (starts at sunset) or **Time** (starts at a fixed time).
- `select.luma_presence_stop_behavior`: what to do with the lights the simulation turned on when it is stopped manually: keep them on, turn them off at once, turn them off gradually, or keep them on only at night.
- Settings: minimum and maximum lights on, minimum front and rear lights on, dwell times, switch-off interval, start, end and late end times, late arrival, and random start and end delays.
- Sensors: status, next event, last action, simulating, learning, and valid learning days.
- Diagnostic sensors, to see from the device page which lights take part:
  - `sensor.luma_presence_simulation_lights`: number of simulation lights, with the `front`, `rear` and `interior` lists as attributes.
  - `sensor.luma_presence_learning_lights`: number of learned entities, with the `entities` list and the `auto_excluded` mapping as attributes. `auto_excluded` gives, for each indoor light left out automatically, the reason: `disabled`, `hidden`, `entity_category` or `indicator_led`.
- Night sequences: `switch.luma_presence_sequences`, `time.luma_presence_sequence_start_time`, `number.luma_presence_sequence_min_interval`, `number.luma_presence_sequence_max_interval`, `binary_sensor.luma_presence_sequence_running` (with the `route` and `step` attributes), `sensor.luma_presence_next_sequence` (with the configured `routes` as an attribute) and `button.luma_presence_test_sequence`.

The simulation always ends at sunrise at the latest. Scheduled ends turn the lights off gradually.

### Ownership rule

LumaPresence only turns off lights that it turned on itself and that nobody has touched since. If you come home and switch on a light, it will never turn it off.

## Learning

From day one, LumaPresence stores real usage of the lights in its [learning scope](#learning-scope) in `.storage`, per day and only while the house is occupied, so it does not depend on the recorder's retention.

Learning pauses while the simulation is enabled, the alarm is armed away, nobody is home, or it has been paused manually. Excluded days never count as days without activity: statistics are based only on observed time. After an absence longer than 48 hours, the whole day of the return is excluded.

Turning the simulation on while someone is at home is treated as a **test**, not an absence (`simulation_test` in the `paused_reasons` attribute of `binary_sensor.luma_presence_learning_active`): what was observed until then is kept, and learning starts again as soon as the test ends, with no return margin. If everybody leaves or the alarm is armed away during the test, the normal absence rules apply from that moment. Without people configured, the simulation always counts as an absence. Light changes made by LumaPresence itself are never learned.

## Unavailable lights

LumaPresence watches every simulation light and every route light. When one stays `unavailable` or `unknown` for *Alert if a light is unavailable for (minutes)* (60 by default, in the **Monitoring** section of **Configure**; 0 turns it off), it raises a Repairs issue for that light, saying since when and what it is used for, and fires the `luma_presence_light_unavailable` event. As soon as the light reports any other state, the issue disappears and `luma_presence_light_available` is fired. After a restart, the clock starts again at startup.

Both events carry `entry_id`, `entity_id`, `since` and `roles`, so one automation can notify you about any light:

```yaml
automation:
  - alias: "LumaPresence: light unavailable"
    triggers:
      - trigger: event
        event_type: luma_presence_light_unavailable
    actions:
      - action: notify.notify
        data:
          title: "Light unavailable"
          message: >
            {{ trigger.event.data.entity_id }} has been unavailable since
            {{ trigger.event.data.since }} ({{ trigger.event.data.roles }}).
```

## Services

- `luma_presence.start` / `luma_presence.stop`: turn the simulation on or off. `stop` accepts an optional `lights_behavior` that overrides the configured stop behaviour.
- `luma_presence.forget_period`: deletes everything learned between two dates.
- `luma_presence.run_sequence`: runs a night route now. Accepts an optional `route` name; a random route otherwise.

## Translations

The user interface is available in English and Spanish. Contributions for other languages are welcome: add a file to `custom_components/luma_presence/translations/`.

## Roadmap

- **Phase 2**: night sequences (v0.3.0), then Replay mode (replays your real usage from previous days).
- **After Replay (optional)**: alarm integration, configurable from LumaPresence: arming the alarm in away mode turns the simulation on, and disarming it stops the simulation.
- **Phase 3**: Dream mode (new, believable patterns generated from what has been learned, with optional AI), repairs and diagnostics.

## Development

```bash
pip install pytest-homeassistant-custom-component
pytest
```

## License

MIT
