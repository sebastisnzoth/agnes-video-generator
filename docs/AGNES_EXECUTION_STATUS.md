# AGNES — ESTADO DE EJECUCIÓN

Fecha de corte: 2026-10-23.

## Hecho verificado
- GitHub `master`: `9bb34f1c7521130bcbbca737f24359d62c12f65d` (revisión de 2026-10-23).
- `core/pipelines/music_video.py` incluye pipeline musical y restricciones de carga.
- CI del SHA revisado: workflow 39064475250, todos sus jobs completados correctamente (17/10).
- PR #1 `feat/vercel-worker-architecture` abierta y draft, con `core/remote_queue.py`, `worker.py` y `supabase_schema.sql`. No fusionada.
- Proyecto Vercel `agnes-video-generator`, id `prj_7h8TdD93comV3XY2YZvY0O2S8Lff`. Existen despliegues READY: no se ha demostrado funcionamiento de generación real.
- Rama segura de ejecución `feat/agnes-music-studio-delivery-20261023` creada desde el SHA verificado.

## Pendiente / bloqueado
- Reproducción y reparación comprobada del error 401 con credenciales **no expuestas**.
- Test real MP3 + foto -> MP4 descargable.
- Evaluación de la PR #1 frente al código musical y al SHA actual, sin fusionar a ciegas.
- Validación de cola/worker, almacenamiento, costos y recuperación de tareas.
- Revocación/rotación confirmada de credenciales históricamente filtradas.
- E2E Preview y Production; prueba con Mac apagada.

## Próxima acción
Revisar los nueve archivos de PR #1 y sus pruebas contra `master`. Ejecutar pruebas de pipeline musical y contrato de API sin credenciales. Gestionar acceso seguro a credencial de prueba y demostrar una generación real corta antes de integración con worker.

## Actualización obligatoria
Por tarea: fecha | SHA | archivos | ejecución | evidencia URL/log/video | estado HECHO/PENDIENTE/BLOQUEADO/DESCONOCIDO | próximo paso. No cambiar estados sin evidencias.


## Actualización 2026-10-24 — GitHub Pages
- **Corregido en rama, pendiente de pruebas:** `frontend/src/api/index.ts` ahora enruta peticiones a `VITE_AGNES_BACKEND_URL` cuando está configurado, y preserva el comportamiento relativo de Vercel/local si no lo está. Incluye URLs de artefactos.
- **Pruebas añadidas:** `frontend/src/api/backendOrigin.test.ts` cubre URL configurada, `submitMusicVideo`, URL de artefactos y modo sin backend configurado.
- Commits de implementación: `b7423bdb4ab9acf5063ca972195e6f2a42aac5ea`, `b31cde128e3a4014f1c2b65d5933e23db680e12e`.
- **Sin CI comprobado todavía:** no se observó workflow ejecutado para esta rama. No se fusionó ni desplegó.
- **Pendientes:** variable pública `AGNES_BACKEND_URL` de GitHub Actions apuntando a API real, CORS del backend, autenticación y worker persistente; GitHub Pages sin backend aún no genera videos.

## Actualización 2026-10-24 — puente completo Pages/API
- GitHub Pages workflow ahora bloquea build sin `AGNES_BACKEND_URL` HTTPS público configurado (`ff532ddd`).
- Se centralizó `resolveApiUrl` en `frontend/src/api/backendOrigin.ts`; `api/index.ts` y `api/fetchPatch.ts` usan ese resolver para enrutar tanto llamadas del cliente como los `fetch('/api/...')` directos de componentes.
- Se agregó prueba `frontend/src/api/fetchPatch.backend.test.ts` para la ruta directa. Commits: `8cef45a`, `945c377`, `65419d3`, `196c623`.
- **Estado:** cambios implementados en rama; CI/compilación y E2E todavía sin ejecutar/verificar. No hay despliegue nuevo.
- **Bloqueo:** configurar backend HTTPS funcional, CORS `AGNES_CORS_ORIGINS`, credencial válida y worker remoto; sin ellos Pages no produce MP4.

## 2026-10-24 — corrección comprobada en código
- Se detectó import duplicado de `resolveApiUrl` en `frontend/src/api/fetchPatch.ts`, introducido en la rama; corregido en `57b9ac37919134accac8b4d32975d9dc786bef53`.
- Lectura posterior de GitHub verificó import único, ausencia del helper `routeApi` obsoleto y uso de `resolveApiUrl` para Request, string y fallback.
- No se observan ejecuciones de GitHub Actions en la rama PR #12 (consulta devolvió 0). La instalación local `npm ci --offline` no pudo ejecutarse porque la copia ZIP carece de package-lock en frontend; NO equivale a aprobación de build ni pruebas.
- Siguiente gate: ejecutar CI con checkout completo y lockfile, corregir fallos, probar pipeline musical con credencial segura; no fusionar/desplegar antes.
