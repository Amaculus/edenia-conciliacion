# Conciliación de cobros — Edenia Hotel & Nature

Pipeline automático que, para cada reserva con check-out, verifica si tiene su
comprobante de pago cargado en PXSol y concilia el monto contra lo esperado.
Genera un reporte priorizado (HTML) y guarda todo en una base SQLite.

Corre sobre PXSol; es **solo lectura**: no cobra ni envía nada.

## Cómo funciona (pipeline)

1. **Presencia + totales.** Saca de la API pública de PXSol las reservas con
   check-out en la ventana, y de la API interna el total real de cada una
   (esperado / pagado / pendiente), igual que el panel "Pago y Facturación".
2. **Comprobantes.** Entra a cada reserva por el endpoint interno
   `bookings/files.php?CID=<reserva>` (con la cookie del login) y baja los
   archivos de la pestaña Archivos desde S3.
3. **Lectura.** PDF por texto (PyMuPDF), imágenes por OCR (Tesseract). Decide si
   es comprobante por palabras (comprobante/transferencia/pago) y por los datos
   obligatorios de una transferencia argentina (CUIT/CUIL, CBU/CVU). Saca monto y
   fecha. Si una imagen no se puede leer y hay `OPENAI_API_KEY`, usa IA solo en
   ese caso.
4. **Lotes.** Cuando un mismo comprobante (misma huella de archivo) cubre varias
   reservas, las agrupa y compara el total contra la suma del grupo.
5. **Conciliación.** Compara el monto del comprobante con lo esperado, usando el
   dólar oficial BCRA **del día del pago** (no el de hoy).
6. **Reporte + base.** Arma `hallazgos.html` priorizado y guarda la corrida en
   `edenia.db` (histórico por corrida).

## Requisitos

- **Python 3.10+**
- **Tesseract OCR** con español:
  - Ubuntu/Debian: `sudo apt install tesseract-ocr tesseract-ocr-spa`
  - Windows: instalar Tesseract (UB-Mannheim) y, si el español no viene, copiar
    `spa.traineddata` a `pipeline/tessdata/` junto con `eng.traineddata` y
    `osd.traineddata` (el código usa esa carpeta si existe).
  - macOS: `brew install tesseract tesseract-lang`
- **Navegador de Playwright** (solo se usa para loguear): `playwright install chromium`

## Instalación

```bash
git clone <este-repo>
cd edenia-conciliacion
python -m pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env      # y completá los valores
```

## Configuración (`.env`)

| Variable | Para qué |
|---|---|
| `PXSOL_API_KEY` | API pública de PXSol (lista de reservas) |
| `PXSOL_WEB_USER` / `PXSOL_WEB_PASS` | Login del PMS (Auth0) para comprobantes y totales |
| `OPENAI_API_KEY` | Opcional: leer imágenes ilegibles con IA |
| `OPENAI_MODEL` | Modelo (default `gpt-5.6-luna`) |
| `USD_ARS_RATE` | Fallback de tipo de cambio si BCRA no responde |

## Uso

Correr todo:

```bash
bash pipeline/conciliar.sh 14          # revisa los últimos 14 días
bash pipeline/conciliar.sh 60          # backfill inicial (60 días)
bash pipeline/conciliar.sh 14 --no-ai  # sin IA (solo OCR)
```

Salidas en `pipeline/out/`:

- `hallazgos.html` — el reporte para abrir en el navegador (lo principal).
- `hallazgos.txt` — el mismo reporte en texto.
- `comprobantes.csv` / `comprobantes_files.csv` — detalle por reserva y por archivo.
- `archivos_<N>d.csv` — presencia + estado de pago por reserva.
- `edenia.db` — base SQLite con el histórico de corridas.

Primera vez o si el login venció, se loguea solo con `PXSOL_WEB_USER/PASS`. Para
forzar un login manual (por si Auth0 pide algo): `python pipeline/archivos_check.py login --manual`.

## Correr todos los días (cron)

Ejemplo a las 08:00, revisando 14 días hacia atrás (para agarrar comprobantes
subidos tarde):

```cron
0 8 * * *  cd /ruta/edenia-conciliacion && bash pipeline/conciliar.sh 14 >> pipeline/out/cron.log 2>&1
```

## Cómo leer el reporte

Secciones por prioridad (rojo = urgente). Las tarjetas de arriba linkean a cada
sección:

1. **Cobro no registrado** — hay comprobante pero el sistema figura impago. Revisar.
2. **Sin cobrar Directo/Agencia** — reservas nuestras impagas. Perseguir el cobro.
3. **Monto no coincide** — el comprobante no cierra con lo esperado.
4. **Comprobante ilegible** — no se pudo leer, mirar a mano.
5. **Sin cobrar OTA** — flujo de la OTA (Despegar/Booking/Expedia).
6. **Cobrado sin comprobante / Conciliado** — informativo.

Cada reserva linkea a PXSol y muestra esperado vs recibo. Abajo, las
transferencias en lote con su cobertura.

## Consultar la base

```bash
sqlite3 pipeline/out/edenia.db \
  "SELECT round(sum(unpaid),2) FROM v_latest_reservations WHERE currency='USD';"
```

Vistas útiles: `v_latest_reservations`, `v_latest_files` (última corrida).

## Notas y límites

- Es solo lectura; no ejecuta cobros ni envía mensajes.
- Los montos leídos de comprobantes son best-effort; un PDF raro puede leerse mal.
- Las OTAs cobran ellas: su "sin comprobante" suele ser normal.
- El navegador se usa solo para loguear; el resto es HTTP directo, por eso es rápido.
