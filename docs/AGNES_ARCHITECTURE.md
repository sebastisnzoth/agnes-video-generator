# AGNES — ARQUITECTURA OBJETIVO (SUJETA A PRUEBAS)

Vercel hospeda la interfaz Vue y una API FastAPI ligera que crea/consulta tareas y autoriza cargas directas de medios.

Una base de datos persistente conserva propietario, estado, progreso, idempotency key, lease y result_url. Una cola consultable por el worker externo permite recuperación tras fallos, sin depender del ciclo HTTP. El worker Python ejecuta Agnes AI y FFmpeg, y deja artefactos persistentes en almacenamiento privado.

Antes de implementar: revisar la PR #1 (9 archivos), comprobar compatibilidad con pipeline musical del SHA actual y verificar proveedor, worker gratuito realista, almacenamiento/costos, rutas y autorización.

No ejecutar la generación larga en Vercel Functions. No declarar un despliegue READY como prueba de producto terminado. No activar producción hasta completar `docs/AGNES_RELEASE_CHECKLIST.md`.
