# LumaPresence

Simulación de presencia para Home Assistant que hace creíble una casa vacía: enciende y apaga luces de forma aleatoria pero realista, se integra con la alarma y aprende cómo se usa de verdad la casa.

> Estado: **fase 1 (v0.1.0)**. Modos Sol y Horario + recogida de datos de aprendizaje. Replay, Dream, secuencia nocturna e integración con la alarma llegan en las fases 2 y 3.

## Instalación (HACS)

1. HACS → menú ⋮ → *Repositorios personalizados* → `https://github.com/jdgarcia99/ha-luma-presence`, categoría *Integración*.
2. Descarga **LumaPresence** y reinicia Home Assistant.
3. *Ajustes → Dispositivos y servicios → Añadir integración → LumaPresence*.

## Configuración

| Campo | Para qué sirve |
|---|---|
| Luces de simulación | Luces e interruptores que puede controlar la simulación |
| Luces delanteras | Subconjunto visible desde la calle; se garantiza un mínimo encendidas |
| Personas | Si no hay nadie en casa, el aprendizaje se pausa |
| Panel de alarma | Con la alarma en *armada ausente*, el aprendizaje se pausa |
| Márgenes | Horas descartadas antes de una ausencia y pausa tras el regreso |
| Solo acciones humanas | Ignora cambios hechos por automatizaciones y scripts |

## Entidades

- `switch.luma_presence_simulation`: interruptor maestro. Se restaura tras un reinicio y retoma la simulación si toca.
- `select.luma_presence_mode`: **Sol** (desde la puesta de sol) u **Horario** (hora de inicio).
- `select.luma_presence_stop_behavior`: qué hacer con las luces al detener la simulación a mano.
- Ajustes: mínimo/máximo de luces, mínimo de delanteras, permanencia, intervalo de apagado, horas de inicio/fin/fin tardía, llegada tardía e inicio/fin aleatorios.
- Sensores: estado, próximo evento, última acción, simulando, aprendiendo y días de aprendizaje válidos.

La simulación termina siempre al amanecer como muy tarde. Los finales programados apagan las luces poco a poco.

### Regla de propiedad

LumaPresence solo apaga luces que encendió ella y que nadie ha tocado después. Si entras y enciendes una luz, nunca te la apagará.

## Aprendizaje

Desde el primer día, LumaPresence guarda en `.storage` el uso real de las luces, por días y solo con la casa habitada. El aprendizaje se pausa con la simulación activa, la alarma armada en ausente, nadie en casa o la pausa manual. Los días excluidos no cuentan como días sin actividad. Tras una ausencia de más de 48 h se excluye el día completo del regreso.

## Servicios

- `luma_presence.start` / `luma_presence.stop` (con `lights_behavior` opcional).
- `luma_presence.forget_period`: borra lo aprendido entre dos fechas.

## Hoja de ruta

- **Fase 2**: integración con la alarma, secuencia nocturna y modo Replay.
- **Fase 3**: modo Dream (patrones nuevos generados a partir de lo aprendido, con IA opcional), reparaciones y diagnósticos.
