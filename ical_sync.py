#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sincronización de calendarios iCal para Architect'sHouse.

Hace dos cosas, sin servidor y sin coste:

  IMPORTAR  Lee el calendario iCal de Airbnb / Booking.com / VRBO de cada
            alojamiento (guardado en el campo `ical` del alojamiento) y marca
            esas noches como bloqueadas en la web. Se hace desde GitHub
            Actions, así que no hay problema de CORS (el navegador no puede).

  EXPORTAR  Escribe en la carpeta ical/ un archivo .ics por alojamiento con
            TUS reservas (las de la web) y tus bloqueos manuales. Ese archivo
            se publica y la URL se pega en Airbnb / Booking.com para que allí
            también se bloqueen esas fechas.

Uso:
  python ical_sync.py             sincroniza
  python ical_sync.py --check     sólo comprueba la configuración
  python ical_sync.py --selftest  prueba la lógica con datos de ejemplo
"""

import datetime as dt
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from send_emails import Firestore, log
except Exception as e:                                   # pragma: no cover
    print('No se puede importar send_emails.py: %s' % e)
    sys.exit(3)

UA = 'ArchitectsHouse-ical-sync/1.0 (+https://architectshouse-78dd4.web.app)'
OUT_DIR = 'ical'
HOY = dt.date.today()


# ---------------------------------------------------------------- utilidades --
def iso(d):
    return d.strftime('%Y-%m-%d')


def desde_iso(s):
    try:
        p = [int(x) for x in str(s)[:10].split('-')]
        return dt.date(p[0], p[1], p[2])
    except Exception:
        return None


def nights(ci, co):
    """Noches ocupadas: se cuenta la entrada, NO la salida."""
    a, b = desde_iso(ci), desde_iso(co)
    out = []
    if not a:
        return out
    if not b or b <= a:
        b = a + dt.timedelta(days=1)
    d = a
    while d < b:
        out.append(iso(d))
        d += dt.timedelta(days=1)
    return out


def descargar(url, timeout=20):
    # Airbnb a veces entrega la dirección con webcal:// (no es un protocolo
    # que se pueda descargar): se cambia por https:// y listo.
    if url.lower().startswith('webcal://'):
        url = 'https://' + url[len('webcal://'):]
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/calendar,*/*'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ('utf-8', 'latin-1'):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode('utf-8', 'replace')


# ------------------------------------------------------------- lector de ics --
def unfold(text):
    """Junta las líneas partidas (empiezan por espacio o tabulador)."""
    return re.sub(r'\r?\n[ \t]', '', str(text))


def valor_fecha(valor):
    """'20261103' o '20261103T150000Z' -> '2026-11-03'."""
    m = re.match(r'(\d{4})(\d{2})(\d{2})', valor or '')
    if not m:
        return None
    return '%s-%s-%s' % (m.group(1), m.group(2), m.group(3))


def parse_ics(text):
    """Devuelve [(ci, co), ...] con co exclusivo (día de salida)."""
    out = []
    cur = None
    for linea in unfold(text).splitlines():
        linea = linea.strip()
        if linea == 'BEGIN:VEVENT':
            cur = {}
        elif linea == 'END:VEVENT':
            if cur and cur.get('ci'):
                co = cur.get('co') or iso(desde_iso(cur['ci']) + dt.timedelta(days=1))
                out.append((cur['ci'], co))
            cur = None
        elif cur is not None:
            if linea.startswith('DTSTART'):
                cur['ci'] = valor_fecha(linea.split(':', 1)[-1])
            elif linea.startswith('DTEND'):
                cur['co'] = valor_fecha(linea.split(':', 1)[-1])
            elif linea.startswith('STATUS') and 'CANCELLED' in linea.upper():
                cur['cancelled'] = True
    return [(ci, co) for ci, co in out if desde_iso(ci)]


# ------------------------------------------------------------------ Firestore --
def enc(v):
    """Codifica un valor para Firestore.

    El encode_value de send_emails.py no sabe convertir diccionarios ni listas:
    los pasaba a texto y el alojamiento acababa con blocks = "{}" (una cadena),
    con lo que el calendario del panel dejaba de funcionar. Aquí sí se hace
    bien: {} -> mapValue y [] -> arrayValue.
    """
    if v is None:            return {'nullValue': None}
    if isinstance(v, bool):  return {'booleanValue': v}
    if isinstance(v, int):   return {'integerValue': str(v)}
    if isinstance(v, float): return {'doubleValue': v}
    if isinstance(v, dict):  return {'mapValue': {'fields': dict((str(k), enc(x)) for k, x in v.items())}}
    if isinstance(v, (list, tuple)): return {'arrayValue': {'values': [enc(x) for x in v]}}
    return {'stringValue': str(v)}


def normalizar(v):
    """Si el campo llegó a guardarse como texto, se recupera lo que se pueda."""
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, str):
        t = v.strip()
        if t in ('', '{}', 'null', 'None'):
            return {}
        try:
            import ast
            d = ast.literal_eval(t)
            if isinstance(d, dict):
                return dict(d)
        except Exception:
            pass
    return {}


def parchear(fs, coleccion, doc_id, campos):
    """Actualiza sólo los campos indicados (PATCH con updateMask)."""
    mask = ''.join('&updateMask.fieldPaths=' + c for c in campos)
    body = {'fields': {k: enc(v) for k, v in campos.items()}}
    return fs._call('/%s/%s?%s' % (coleccion, doc_id, mask.lstrip('&')),
                    method='PATCH', body=body, timeout=25)


# ------------------------------------------------------------------ exportar --
def generar_ics(nombre, rangos):
    """rangos: [(ci, co, uid_extra), ...] con co exclusivo."""
    ahora = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    ev = []
    for ci, co, extra in rangos:
        ci2, co2 = (ci or '').replace('-', ''), (co or '').replace('-', '')
        if len(ci2) != 8 or len(co2) != 8:
            continue
        ev.append('\r\n'.join([
            'BEGIN:VEVENT',
            'UID:%s@architectshouse-78dd4.web.app' % extra,
            'DTSTAMP:' + ahora,
            'DTSTART;VALUE=DATE:' + ci2,
            'DTEND;VALUE=DATE:' + co2,
            'SUMMARY:%s' % nombre.replace(',', ' ').replace(';', ' '),
            'END:VEVENT',
        ]))
    return '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'CALSCALE:GREGORIAN',
                        'PRODID:-//ArchitectsHouse//iCal sync//EN',
                        'X-WR-CALNAME:%s' % nombre.replace(',', ' ')] + ev + ['END:VCALENDAR']) + '\r\n'


def juntar(rangos):
    """Une noches seguidas en un solo evento (más limpio para las plataformas)."""
    dias = sorted(set(d for ci, co, _ in rangos for d in nights(ci, co)))
    out, ini, ant = [], None, None
    for d in dias + [None]:
        if d and ant and desde_iso(d) == desde_iso(ant) + dt.timedelta(days=1):
            ant = d
            continue
        if ini:
            out.append((ini, iso(desde_iso(ant) + dt.timedelta(days=1)), 'ah-' + ini))
        ini, ant = d, d
    return out


# ---------------------------------------------------------------------- main --
def sincronizar(fs):
    props = fs.docs('props')
    reservas = [d for _, d in fs.docs('reservations')]
    log('%d alojamientos · %d reservas' % (len(props), len(reservas)))

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)

    tocados = 0
    for pid, p in props:
        nombre = p.get('name') or pid
        url = (p.get('ical') or '').strip()

        # ---------- IMPORTAR ------------------------------------------------
        if url:
            try:
                texto = descargar(url)
                rangos = parse_ics(texto)
                nuevos = set(d for ci, co in rangos for d in nights(ci, co))
                nuevos = set(d for d in nuevos if d >= iso(HOY - dt.timedelta(days=30)))
                viejos = set(normalizar(p.get('icalDates')).keys())
                bloqueos = normalizar(p.get('blocks'))

                quitados = 0
                for d in viejos - nuevos:
                    if bloqueos.get(d) == 'blocked':
                        bloqueos.pop(d, None)
                        quitados += 1
                anadidos = 0
                for d in sorted(nuevos):
                    if bloqueos.get(d) in (None, '', 'blocked'):
                        if bloqueos.get(d) != 'blocked':
                            anadidos += 1
                        bloqueos[d] = 'blocked'

                if not rangos:
                    log('  ! %-22s el calendario no trae ningún evento; no se toca nada' % nombre[:22])
                    parchear(fs, 'props', pid, {
                        'icalLastSync': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                        'icalLastError': 'El calendario no trae ningún evento.',
                        'icalNights': 0,
                    })
                    p['blocks'], p['icalDates'] = bloqueos, {}
                else:
                    parchear(fs, 'props', pid, {
                    'blocks': bloqueos,
                    'icalDates': dict((d, '1') for d in sorted(nuevos)),
                    'icalLastSync': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'icalLastError': '',
                    'icalNights': len(nuevos),
                    })
                log('  ✓ %-22s %3d noches bloqueadas (+%d / −%d)'
                    % (nombre[:22], len(nuevos), anadidos, quitados))
                p['blocks'], p['icalDates'] = bloqueos, dict((d, '1') for d in sorted(nuevos))
                tocados += 1
            except Exception as e:
                parchear(fs, 'props', pid, {'icalLastError': str(e)[:180]})
                log('  ✗ %-22s %s' % (nombre[:22], str(e)[:120]))

        # ---------- EXPORTAR ------------------------------------------------
        externos = set(normalizar(p.get('icalDates')).keys())
        propios = []
        for r in reservas:
            if r.get('propId') != pid:
                continue
            if str(r.get('status') or '').lower() == 'cancelled':
                continue
            if r.get('ci') and r.get('co'):
                propios.append((r['ci'][:10], r['co'][:10],
                                'res-' + str(r.get('code') or r.get('id') or r['ci'])))
        for d, motivo in sorted(normalizar(p.get('blocks')).items()):
            if d in externos:                      # ya vienen de fuera: no se reexportan
                continue
            if desde_iso(d) and d >= iso(HOY - dt.timedelta(days=30)):
                propios.append((d, iso(desde_iso(d) + dt.timedelta(days=1)), 'blk-' + d))

        rangos = juntar(propios)
        ics = generar_ics(nombre + ' — no disponible', rangos)
        slug = re.sub(r'[^a-z0-9\-]+', '-', (p.get('slug') or pid).lower()).strip('-') or pid
        ruta = os.path.join(OUT_DIR, slug + '.ics')
        antes = open(ruta, encoding='utf-8').read() if os.path.exists(ruta) else ''
        if antes != ics:
            open(ruta, 'w', encoding='utf-8').write(ics)
            log('  · feed %s (%d eventos) ACTUALIZADO' % (ruta, len(rangos)))
        else:
            log('  · feed %s (%d eventos) sin cambios' % (ruta, len(rangos)))

    log('Alojamientos sincronizados: %d' % tocados)
    return 0


def selftest():
    ics = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20261103\r
DTEND;VALUE=DATE:20261106\r
SUMMARY:Reserva Airbnb\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20261110\r
DTEND;VALUE=DATE:2026111 2\r
SUMMARY:partida\r
END:VEVENT\r
END:VCALENDAR"""
    r = parse_ics(ics)
    assert r[0] == ('2026-11-03', '2026-11-06'), r
    assert nights(*r[0]) == ['2026-11-03', '2026-11-04', '2026-11-05'], nights(*r[0])
    assert r[1][0] == '2026-11-10'
    assert juntar([('2026-11-03', '2026-11-04', 'a'), ('2026-11-04', '2026-11-05', 'b')]) == \
        [('2026-11-03', '2026-11-05', 'ah-2026-11-03')], juntar([('2026-11-03', '2026-11-04', 'a'),
                                                                 ('2026-11-04', '2026-11-05', 'b')])
    salida = generar_ics('Casa — prueba', [('2026-11-03', '2026-11-05', 'ah-1')])
    assert 'DTSTART;VALUE=DATE:20261103' in salida and 'DTEND;VALUE=DATE:20261105' in salida
    assert salida.startswith('BEGIN:VCALENDAR') and salida.strip().endswith('END:VCALENDAR')
    log('selftest OK: 2 eventos leídos, noches y feed correctos')
    return 0


def main():
    if '--selftest' in sys.argv:
        return selftest()
    sa = os.environ.get('FIREBASE_SA_JSON')
    if not sa:
        log('Falta la variable FIREBASE_SA_JSON.')
        return 2
    if '--check' in sys.argv:
        fs = Firestore(sa)
        props = fs.docs('props')
        con_url = [p.get('name') for _, p in props if (p.get('ical') or '').strip()]
        log('Conexión OK · %d alojamientos · %d con calendario iCal: %s'
            % (len(props), len(con_url), ', '.join(con_url) or 'ninguno'))
        return 0
    fs = Firestore(sa)
    log('=== iCal sync %s ===' % iso(HOY))
    return sincronizar(fs)


if __name__ == '__main__':
    sys.exit(main())
