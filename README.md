# API RDI

Genera un PDF de dos páginas para registros de la tabla AppSheet `Historial RDI`.

## Endpoints

- `GET /health`
- `GET /test_appsheet?id=<ID_RDI>`
- `GET /generar?id=<ID_RDI>`

## Variables de entorno

- `APPSHEET_APP_ID`
- `APPSHEET_ACCESS_KEY`
- `APPSHEET_TABLE` (opcional; por defecto `Historial RDI`)

## Cloud Run

Configuración sugerida:

- Región: `southamerica-west1`
- Puerto: `8080`
- Memoria: `1 GiB`
- CPU: `1`
- Timeout: `600 segundos`
- Acceso público
- Facturación basada en solicitudes
- Instancias mínimas: `0`
- Instancias máximas: `3`
