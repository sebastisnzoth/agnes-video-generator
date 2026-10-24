# AGNES MUSIC VIDEO STUDIO — PLAN MAESTRO

Fecha de inicio: 2026-10-23. Repositorio oficial: `sebastisnzoth/agnes-video-generator`. Rama principal `master`; rama de ejecución `feat/agnes-music-studio-delivery-20261023`.

## Misión y condición de entrega

Una aplicación web en **Vercel**, independiente de la Mac, debe permitir: subir una canción MP3/WAV/M4A y una foto de un protagonista autorizado, seleccionar estilo y formato, generar storyboard y escenas con IA, conservar el audio original, mostrar progreso persistente, recuperar el trabajo y descargar un MP4 reproducible.

**No se declara terminado por CI verde, PR fusionada o despliegue READY.** Termina al pasar el E2E real desde la URL Production, con la Mac apagada.

## Alcance y exclusiones

- Reutilizar `core/pipelines/music_video.py`, `frontend/src/components/forms/MusicVideoForm.vue` y las pruebas existentes.
- No modificar UGO. No reescribir el proyecto sin pruebas que lo justifiquen.
- Lip-sync avanzado es posterior al MVP y requiere evaluación audiovisual real.
- No suponer generación ilimitada, proveedor estable, worker gratis ni cero errores.

## Arquitectura objetivo

Navegador -> Vercel (UI Vue + API ligera FastAPI) -> DB/cola persistente -> worker Python independiente (Agnes IA + FFmpeg) -> almacenamiento de fotos, audio, clips y MP4.

Archivos grandes se suben directamente al almacenamiento; no pasan por cuerpos HTTP de Vercel. Los trabajos usan idempotencia, estados, lease/heartbeat, reintentos limitados, detección de trabajos huérfanos y resultado durable. Las credenciales viven únicamente en el servidor.

## Fases y gates

| Fase | Trabajo | Evidencia exigida | Gate |
|---|---|---|---|
| F0 | Baseline, AGENTS.md, revisar PR #1, comparar fuentes musicales, CI, seguridad | SHA, diferencias y pruebas | Aprobado antes de editar producción |
| F1 | Reparar 401 y probar generación MP3 + foto | Proveedor responde; video MP4 real, inspeccionado y con audio original | Sin un clip real no ampliar arquitectura |
| F2 | UX Vue completa | carga, vista previa, storyboard, errores, progreso, descarga; responsive | E2E de navegador |
| F3 | Persistencia y worker remoto | trabajo sobrevive recarga/reinicio, no duplica, usuario recupera MP4 | Test de recuperación y Mac apagada |
| F4 | Seguridad, CI, E2E, costos | Pytest/Vitest/build/lint, RLS/autorización, escaneo secretos, quotas | Cero P0/P1 abiertos |
| F5 | Preview -> Production | E2E real en ambos, URL, video, SHA, logs y rollback | Entrega final |

## Reglas para agentes

Un orquestador coordina tareas de backend, video, frontend, infraestructura, seguridad y QA. Codebuff/OpenCode/Codex son opciones de ejecución **si están disponibles**, Agent Skills define procedimientos. Nunca dos agentes escriben los mismos archivos en paralelo. Cada cambio debe incluir causa, prueba, resultado, SHA y reversión.

Prohibido: tocar UGO, comprometer secretos, simular generación real, ejecutar video largo dentro de una función Vercel, forzar ramas, fusionar CI rojo, contratar servicios pagos sin autorización o desplegar antes de Gate F5.

## Dependencias de viabilidad

1. Clave Agnes AI válida y cuotas suficientes, o alternativa autorizada.
2. Worker independiente apto para FFmpeg y tiempos de generación.
3. DB/almacenamiento con acceso seguro, límites y retención definidos.
4. Credenciales históricamente expuestas revocadas y rotadas.
5. Pruebas humanas de imagen/video y gastos autorizados cuando correspondan.

## Handoff

Leer este archivo, `AGENTS.md`, `docs/AGNES_EXECUTION_STATUS.md` y `docs/AGNES_RELEASE_CHECKLIST.md`. Continuar el primer gate no verificado y actualizar evidencia tras cada paso.
