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

## 2026-10-24 — pruebas ejecutadas y corrección de seguridad
- PR #12 HEAD previo: `47a7bc072b5b76df2d47846f61e722e36414fccc`; `master` sigue en `9bb34f1` según consulta.
- `frontend/src/api/backendOrigin.ts`: validación de URL pública mediante `URL`, exigiendo HTTPS, sin credenciales incrustadas, sin pathname no raíz ni query/hash. Commits finales `860a9131`. `frontend/src/api/backendOrigin.test.ts`: regresión para destinos inseguros, commit `db841395`.
- **Prueba real intentada (NO PASA):** `npm ci --offline --ignore-scripts --no-audit --no-fund` en ZIP `AGNES-MUSICAL.zip` (con `frontend/package-lock.json`) falló `ENOTCACHED` para `yocto-queue-1.2.2.tgz`. El problema no es falta de lockfile; es ausencia de dependencias en caché sin acceso a npm registry.
- **Prueba real intentada (NO PASA):** `PYTHONPATH=. python -m pytest tests/test_music_video_timeline.py tests/test_music_video_lyrics.py -q --disable-warnings --maxfail=1` falló durante importación por `ModuleNotFoundError: No module named 'edge_tts'`. No implica que los tests funcionales estén fallando; todavía no pudieron arrancar.
- No se comprobó CI de GitHub para esta rama, ni generación real, ni despliegue. Mantener ambas PR draft.
- Próximo paso verificable: resolver dependencias en entorno con acceso de red (Actions / runner autorizado), ejecutar `npm ci && npm test && npm run build` y suite Pytest; integrar la ruta musical con el worker tras aprobar gates.

## 2026-10-24 — primera integración de music-video con cola
- Se incorporaron desde PR #1 a la rama de PR #12 `core/remote_queue.py`, `worker.py`, `supabase_schema.sql` y `tests/test_remote_queue.py` sin fusionar PR #1; son componentes existentes, **no probados en producción**.
- `web/routes/task_creation_routes.py`: para `remote_queue.enabled()` serializa y sube el estado/archivos por `asyncio.to_thread(remote_queue.enqueue_state, ...)`, devuelve éxito solo tras persistencia; elimina archivos temporales y retorna 503 si falla. Commit `0d138aa6f5b625317322c408bcf83c9a9f4df415`.
- `tests/test_music_video_remote_queue.py`: regresión de encolado real frente a ejecución local y limpieza/fallo de almacenamiento. Commit `d02779171b72387c130fd44c3f51c7bedfb84d23`.
- **Verificación pendiente:** CI/pytest no ejecutados para estos cambios; no se ha probado Supabase real, worker, autenticación por usuario ni MP4, y faltan conectar endpoints de estado/artefactos a la cola. No se debe fusionar ni desplegar.

## 2026-10-24 — worker output safety hotfix
- `worker.py` now requires `final_video_file` to exist before upload, instead of selecting an arbitrary .mp4/.webm from state; it stores private `supabase://` object reference rather than an expiring seven-day download URL (commits `76d81ce`, `1f21241`).
- Worker `main` fails fast unless `AGNES_TASK_BACKEND=supabase`, avoiding accidental local SQLite polling.
- `core/remote_queue.py` signed URL helper errors on missing URL and resolves relative URLs properly (commit `ab741f8`).
- Regression tests added: `tests/test_music_worker_safety.py` (commit `0feae5e`).
- **Warning:** converting private `supabase://` reference into a short-lived signed download link still needs an authenticated task/artifact API, so do not expose queue rows directly to public users. No verified video generation; CI not executed; do not merge or deploy.

## 2026-10-24 — corrección de transferencia binaria de audio/video
- `core/remote_queue.py`: subida a Supabase Storage ahora utiliza streaming desde descriptor de archivo y MIME detectado (`audio/mpeg`, `video/mp4`, etc.), evitando `application/json` y lectura total en RAM. Commit `c2aa980547d8c3d8f51ba7ae5fb9e55342f7d752`.
- `tests/test_remote_queue.py`: regresión con HTTP simulado para asegurar MIME, bytes transmitidos y referencia persistente. Commit `04f02ee86ad9e51ae93c39274ee740af6e64aff5`.
- **Sin evidencia de validación E2E o CI para estos commits**. Persisten los bloqueos críticos de autenticación, acceso privado al MP4, workers ejecutándose en hosting persistente, 401 de Agnes y generación real. Ningún despliegue ni fusión realizados.

## 2026-10-24 — cola Render PostgreSQL + aislamiento de activos
- Base Agnes propia en Render PostgreSQL `dpg-dbe6252vcj2c73br0frg-a`, plan gratuito, estado `available` verificado; caduca 2026-11-23. No se tocó ninguna base existente.
- Supabase no permitió proyecto gratuito nuevo por límite 2/2. Vercel Blob privado respondió 403 forbidden. Ningún bucket público se configuró.
- `core/postgres_queue.py`: crear/consultar/listar/actualizar/claim tareas con SQL parametrizado, `FOR UPDATE SKIP LOCKED`, conexión a `AGNES_DATABASE_URL` (commit `5e8d932`).
- `core/remote_queue.py`: delega persistencia al driver postgres si `AGNES_TASK_BACKEND=postgres` y falla cerrado al subir archivos si `AGNES_ASSET_BACKEND` privado no está configurado (commits `891b074`, `93f1e14`).
- `worker.py`: permite PostgreSQL además de Supabase (`9f301e7`). `requirements.txt`: psycopg 3 agregado (`9ca0d92`). `render_postgres_schema.sql` agregado (`def24c0`). Tests sin ejecutar `tests/test_render_postgres_queue.py` (`cbbf41f`).
- Render MCP read-only SQL no logró conectarse: su IP no está permitida en la allowlist del PostgreSQL. No se aplicó migración SQL ni se verificó escritura de tareas. Restringir acceso de red; no abrir públicamente para forzar un test.
- Bloqueo real de entrega: almacenamiento privado compatible con worker, credencial válida Agnes AI, ejecución de migración PostgreSQL, despliegue de worker y pruebas CI/E2E. No se desplegó ni fusionó.
