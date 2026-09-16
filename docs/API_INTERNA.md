# Mapa de la API interna de PXSol

Relevado el 2026-09-16 sobre Edenia (hotel_id 24419, company_id 23386) mirando las
llamadas de red del PMS. **Todo es no oficial y puede cambiar.** Solo lectura.

## Autenticación (dos tipos)

| Tipo | Para qué | Cómo se obtiene |
|---|---|---|
| **Cookie de sesión** | endpoints `pms.pxsol.com/bookings/*.php` | login Auth0 una vez; se guarda en `state.json` |
| **Bearer (Auth0 access token)** | `api-1-*.pxsol.io`, `api-2-pms-prod.pxsol.com`, `apex` | se "escucha" del propio SPA al abrir una reserva; dura ~horas |

La lista de reservas también sale de la **API pública** (`gateway-prod.pxsol.com/v2`,
con `PXSOL_API_KEY`), pero la interna da bastante más.

## Lo más valioso

### 1) `POST api-2-pms-prod.pxsol.com/api/v2/reservations/list`  (Bearer)
Body mínimo: `{"company_id":23386,"per_page":N,"current_page":1}`. Paginado por
`pagination.next_cursor` (`has_more`). **Devuelve todo de cada reserva en una sola
llamada**, sin falta de sub-totales como en la API pública:

`cid, nombre, apellido, documento, email, nacionalidad, razon_social,
medio, source, via, proveedor, agencia_vendedora, agent_name, organization_name,
establecimiento, punto_venta, instancia, equipo, equipo_vendedor,
check_in, check_out, fecha_arribo, fecha_salida, fecha_caida, fecha_confirmacion_reserva,
fecha_ingreso_sistema, fecha_preventa, fecha_maxima_bloqueo, fecha_ultima_accion,
noches, pax, huespedes, cant_habitaciones, habitaciones_fisicas, rooms, es_grupo,
tipo, estado, status_label, moneda,
total_reserva, total_alojamiento, pagado, saldo,
early_check_in, late_check_out, memo, codigo_pms, codigo_externo_reserva, ...`

→ `total_reserva`, `pagado`, `saldo` y `moneda` ya vienen acá. Esto podría
reemplazar el combo actual (API pública `booking/list` + `summary` por reserva).

### 2) `GET pms.pxsol.com/bookings/pagos_pms.php?CID=<cid>`  (cookie) — HTML
**El libro de pagos real** de la reserva: por fila `Fecha, Cuenta (método),
Concepto, Folio, Ingresos, Egresos, Usuario, Modificado`. Ejemplos reales:
`TARJETA DE CREDITO`, `MERCADO PAGO (boton)`, e incluso el cambio aplicado
(`ARS 514135.45 = USD 355.68`). Da método, monto, fecha, quién lo registró y FX.

### 3) `GET pms.pxsol.com/bookings/budgets.php?CID=<cid>`  (cookie) — HTML
Presupuesto / desglose de tarifa por noche de la reserva.

### 4) `GET api-2-pms-prod.pxsol.com/api/booking_activity_log/list/<cid>`  (Bearer)
Auditoría completa: `usuario, tarea, fecha, hora, ip, request` por cada acción.

## Resto de endpoints vistos

### pms.pxsol.com/bookings/*.php (cookie) — fragmentos HTML por pestaña
| Endpoint | Contenido |
|---|---|
| `cid.php?CID=` | ficha de la reserva |
| `files.php?CID=` | pestaña Archivos: comprobantes + link S3 de descarga |
| `botones.php?CID=` | panel de acciones + aviso de saldo |
| `historial.php?CID=` | historial / timeline |
| `budgets.php?CID=` | presupuesto / tarifas |
| `pagos_pms.php?CID=` | libro de pagos (ver arriba) |
| `facturacion.php?CID=` | facturación |
| `v1/rooms_and_guests.php?CID=` | habitaciones + huéspedes + estado housekeeping |
| `avanzado.php?CID=` | web check-in y opciones avanzadas |
| `registros.php?CID=` | reglas / registros |
| `programados.php?CID=` | eventos programados |
| `list.php` | grilla de reservas (HTML) |
| `historial/encuesta.php` (POST) | encuesta post-estadía |

### api-2-pms-prod.pxsol.com/api (Bearer, JSON)
| Endpoint | Contenido |
|---|---|
| `v2/reservations/list` (POST) | grilla completa (ver arriba) |
| `v2/reservations/counts` (POST) | conteos por estado |
| `v2/reservations/{cid}/summary` | totales: total, paid, unpaid, currency |
| `v2/reservations/{cid}/card-request/status` | estado de pedido de tarjeta |
| `v2/reservations/set_filters` | filtros/columnas de la grilla |
| `booking_activity_log/list/{cid}` | auditoría (ver arriba) |
| `custom_table_views/list` | vistas guardadas de la grilla |
| `notifications/alert/count/user` | contador de alertas |
| `users/{id}/settings` | preferencias del usuario |
| `tickets/numbers` | soporte |
| `integrations/{hotel}/pms` | integraciones |
| `resellers/info` | info del reseller |
| `permissions/criteria` (POST) | permisos |
| `channels/authenticate` (POST) | auth de canales |

### api-1-pms-prod.pxsol.io (Bearer, JSON)
| Endpoint | Contenido |
|---|---|
| `product/info?id=<hotel>` | config del hotel (nombre, logo, company/team, provider…) |
| `agreement/list?id=<hotel>&pos=<pos>` | acuerdos / config del motor de reservas |
| `pos/info?pos=<pos>` | info del punto de venta |
| `booking/alarm?CID=` | alarmas de la reserva |
| `user/valid` | validación de sesión |

### Otros hosts vistos
| Host | Para qué |
|---|---|
| `apex.pxsol.com/reservas[/{cid}]` | app React nueva de reservas (UI de próxima generación) |
| `api-1-eb.pxsol.io` | motor de reservas / inbox / CRM (`inbox/tags`, `ticket/agents`, `budget/policy_change_log`, `px/lead_convertion`, `apps/shortcuts`) |
| `conversations-back-prod.pxsol.io` | chat / conversaciones |
| `nexus.getpxsol.com/api/popups/eligible` | popups in-app |

## Qué más podríamos construir con esto

- Reemplazar el pull actual por `reservations/list` paginado: una llamada trae
  total/pagado/saldo/canal/huésped de todas las reservas (no solo check-outs).
- Conciliar contra el **libro de pagos** (`pagos_pms.php`): método real, fecha,
  usuario y el FX que usó el hotel, en vez de inferirlo.
- Panel de caja: ingresos por método (tarjeta/MP/transferencia) por día.
- Auditoría: quién tocó cada reserva y cuándo (`booking_activity_log`).
- Housekeeping / ocupación desde `rooms_and_guests`.
