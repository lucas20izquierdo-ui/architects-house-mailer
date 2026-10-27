#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Precios de Airbnb -> overrides de la web.

Airbnb no pone el precio en el iCal. Este módulo lee el precio que ve el
huésped (el de la ficha, ya con el descuento de estancia, sin la tasa) y lo
deja en `overrides`, que es lo que la web usa por noche.

Solo para los alojamientos con anuncio: el enlace en `airbnb`, o el id que
ya va en la URL del calendario iCal. No toca las casas sin anuncio.
"""

import base64
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API_KEY = 'd306zoyjsyarp7ifhu67rjxn52tv0t20'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
DIAS = 183         # noches hacia delante: horizonte móvil de seis meses
PAUSA = 0.3        # segundos entre consultas, para no saturar


def listing_id(p):
    """Id numérico del anuncio, si lo hay."""
    for raw in (p.get('airbnb') or '', p.get('ical') or ''):
        m = re.search(r'(?:rooms|ical)/(\d{6,})', str(raw))
        if m:
            return m.group(1)
    return ''


def _get(url, accept='*/*', timeout=25):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': accept,
        'Accept-Language': 'en-US,en;q=0.9',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        if e.code == 410:
            raise RuntimeError('el anuncio ya no existe en Airbnb')
        if e.code in (403, 429):
            raise RuntimeError('Airbnb ha bloqueado la consulta (%s)' % e.code)
        raise RuntimeError('Airbnb respondió %s' % e.code)


def parse_money(s):
    """'Fr. 1,093.30' / 'Fr. 215' / '-Fr. 27.20' -> float."""
    if not s:
        return None
    t = str(s).replace('\u00a0', ' ').replace('\u202f', ' ')
    neg = bool(re.search(r'[-−]', t))
    m = re.search(r'\d[\d\s\'’,\.]*', t)
    if not m:
        return None
    raw = m.group(0).replace(' ', '').replace("'", '').replace('’', '')
    if ',' in raw and '.' in raw:
        if raw.rfind('.') > raw.rfind(','):
            raw = raw.replace(',', '')
        else:
            raw = raw.replace('.', '').replace(',', '.')
    elif ',' in raw:
        raw = raw.replace(',', '') if re.search(r',\d{3}$', raw) else raw.replace(',', '.')
    try:
        v = float(raw)
    except ValueError:
        return None
    return -v if neg and v > 0 else v


def cargar(listing):
    """Plantilla de la ficha + hash actual de la consulta de precio."""
    status, raw = _get('https://www.airbnb.com/rooms/%s' % listing, 'text/html')
    if status != 200:
        raise RuntimeError('la ficha respondió %s' % status)
    html = raw.decode('utf-8', 'replace')
    m = re.search(r'<script id="data-deferred-state-0"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError('la ficha no trae datos')
    data = json.loads(m.group(1))
    plantilla = None
    for entry in data.get('niobeClientData') or []:
        if isinstance(entry, list) and entry and str(entry[0]).startswith('StaysPdpSections:'):
            plantilla = json.loads(str(entry[0]).split(':', 1)[1])
            break
    if not plantilla:
        raise RuntimeError('no se encontró la ficha del anuncio')
    src = re.search(r'https://a0\.muscache\.com/airbnb/static/packages/[^"\']+PdpPlatformRoute[^"\']+\.js', html)
    if not src:
        raise RuntimeError('no se encontró el script de precios')
    _, js = _get(src.group(0), 'application/javascript')
    hm = re.search(r"name:'StaysPdpSections',type:'query',operationId:'([a-f0-9]{64})'", js.decode('utf-8', 'replace'))
    if not hm:
        raise RuntimeError('Airbnb cambió la consulta de precios')
    return plantilla, hm.group(1)


def cotizar(plantilla, op, listing, ci, noches, adultos=2, ninos=0):
    """Total que ve el huésped para esa estancia, o None si no está libre."""
    co = (dt.date.fromisoformat(ci) + dt.timedelta(days=noches)).isoformat()
    vars = json.loads(json.dumps(plantilla))
    vars['dateRange'] = {'startDate': ci, 'endDate': co}
    guest_counts = vars.get('guestCounts') or {}
    guest_counts['numberOfAdults'] = int(adultos or 2)
    guest_counts['numberOfChildren'] = int(ninos or 0)
    vars['guestCounts'] = guest_counts
    req = vars.get('pdpSectionsRequest') or {}
    req['checkIn'] = ci
    req['checkOut'] = co
    req['adults'] = str(int(adultos or 2))
    req['children'] = str(int(ninos or 0))
    req['sectionIds'] = ['BOOK_IT_SIDEBAR']
    vars['pdpSectionsRequest'] = req
    vars['includePdpMigrationBookItSidebarFragment'] = True
    params = {
        'operationName': 'StaysPdpSections',
        'locale': 'en',
        'currency': 'CHF',
        'variables': json.dumps(vars, separators=(',', ':')),
        'extensions': json.dumps({'persistedQuery': {'version': 1, 'sha256Hash': op}}, separators=(',', ':')),
    }
    url = 'https://www.airbnb.com/api/v3/StaysPdpSections?' + urllib.parse.urlencode(params)
    reqh = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'application/json',
        'X-Airbnb-API-Key': API_KEY,
        'Referer': 'https://www.airbnb.com/rooms/%s' % listing,
    })
    with urllib.request.urlopen(reqh, timeout=25) as r:
        data = json.loads(r.read().decode('utf-8', 'replace'))
    if data.get('errors') and not data.get('data'):
        raise RuntimeError(data['errors'][0].get('message') or 'error de Airbnb')
    secs = (((data.get('data') or {}).get('presentation') or {}).get('stayProductDetailPage') or {}).get('sections', {}).get('sections') or []
    if not secs:
        return None
    sec = secs[0].get('section') or {}
    if not sec.get('available'):
        return None
    primary = ((sec.get('structuredDisplayPrice') or {}).get('primaryLine') or {})
    total = parse_money(primary.get('discountedPrice') or primary.get('price') or primary.get('accessibilityLabel'))
    return total


def _redondeo(v):
    return int(float(v) + 0.5)


def _pct(total, base):
    if not total or not base or base <= 0:
        return None
    return int((1 - float(total) / float(base)) * 100 + 0.5)


def precios_de(listing, min_stay=2, adultos=2, dias=DIAS, hoy=None, log=None,
               cotizaciones=False, cotizaciones_desde=0, cotizaciones_hasta=None,
               cotizaciones_adultos=None, cotizaciones_ninos=0):
    """Devuelve precios diarios y cotizaciones exactas 3/7/9 en un lote.

    La caché admite una combinación adulta/niños por ejecución para evitar
    agotar el timeout de GitHub Actions y reducir picos de peticiones a Airbnb.
    """
    log = log or (lambda *a: None)
    hoy = hoy or dt.date.today()
    if cotizaciones_hasta is None:
        cotizaciones_hasta = dias
    noches = max(1, int(min_stay or 1))
    plantilla, op = cargar(listing)
    out = {}
    stay_quotes = {}
    primera = None
    for i in range(dias):
        ci = (hoy + dt.timedelta(days=i)).isoformat()
        usado = noches
        try:
            total = cotizar(plantilla, op, listing, ci, noches, adultos)
            if not total and noches > 1:
                time.sleep(PAUSA)
                total = cotizar(plantilla, op, listing, ci, 1, adultos)
                usado = 1
        except Exception as e:
            log('  ! %s %s' % (ci, str(e)[:80]))
            time.sleep(PAUSA)
            continue
        time.sleep(PAUSA)
        if not total or total <= 0:
            continue
        por_noche = _redondeo(total / usado)
        if por_noche <= 0:
            continue
        out[ci] = por_noche
        if primera is None:
            primera = ci
        log('  %s  %d CHF/noche  (estancia %d = %.0f)' % (ci, por_noche, usado, total))
        if cotizaciones and cotizaciones_desde <= i < cotizaciones_hasta:
            cotizaciones_dia = {}
            for largo in (3, 7, 9):
                try:
                    exacto = cotizar(
                        plantilla, op, listing, ci, largo,
                        cotizaciones_adultos or adultos,
                        cotizaciones_ninos,
                    )
                except Exception as e:
                    log('  ! cotización %d noches, %d adulto(s) + %d niño(s), %s: %s' % (largo, cotizaciones_adultos or adultos, cotizaciones_ninos, ci, str(e)[:80]))
                    time.sleep(PAUSA)
                    continue
                time.sleep(PAUSA)
                if exacto and exacto > 0:
                    cotizaciones_dia[str(largo)] = round(float(exacto), 2)
            if cotizaciones_dia:
                stay_quotes[ci] = cotizaciones_dia
    desc = {}
    if primera:
        for largo, clave in ((3, '3'), (7, '7')):
            if largo <= noches:
                continue
            try:
                total = cotizar(plantilla, op, listing, primera, largo, adultos)
            except Exception as e:
                log('  ! descuento %d noches: %s' % (largo, str(e)[:80]))
                continue
            time.sleep(PAUSA)
            if not total:
                continue
            base = 0
            ok = True
            d0 = dt.date.fromisoformat(primera)
            for k in range(largo):
                d = (d0 + dt.timedelta(days=k)).isoformat()
                if d not in out:
                    ok = False
                    break
                base += out[d]
            if ok:
                pct = _pct(total, base)
                if pct and pct > 0:
                    desc[clave] = pct
                    log('  descuento desde %d noches: %d%%' % (largo, pct))
    return out, desc, stay_quotes
