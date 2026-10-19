#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
send_emails.py · Architect's House  (v2 · API REST de Firestore)
Envía automáticamente los correos de reserva leyendo Firestore y mandándolos con Resend.

  confirm    al reservar (y hasta 2 días después si el reloj estaba parado)
  before     3 días antes de la entrada
  checkin    el mismo día de la entrada
  during     1 día después de la entrada
  departure  1 día antes de la salida
  after      el día de la salida
  review     2 días después de la salida

Uso:
  python send_emails.py             envío real
  python send_emails.py --dry-run   muestra qué enviaría, sin mandar nada
  python send_emails.py --check     comprueba claves y conexión, sin enviar
  python send_emails.py --selftest  prueba la lógica sin red

Variables de entorno (GitHub Secrets):
  FIREBASE_SA_JSON  contenido del JSON de la cuenta de servicio de Firebase
  RESEND_API_KEY    clave de API de Resend
  FROM_EMAIL        remitente:  Architect's House <reservas@tudominio.ch>
  REPLY_TO          (opcional) dirección de respuesta
  SITE_URL          (opcional) https://architectshouse-78dd4.web.app
  DRY_RUN           (opcional) 1 = no enviar
"""
import os, sys, json, signal, datetime as dt
import urllib.request, urllib.error, urllib.parse

TZ = dt.timezone(dt.timedelta(hours=2))
TODAY = lambda: dt.datetime.now(TZ).date()
WATCHDOG = 240          # segundos máximo de ejecución

def log(msg):
    print(msg, flush=True)

# ==================== Firestore por REST (sin librerías pesadas) ==============
SCOPES = ['https://www.googleapis.com/auth/datastore']
BASE = 'https://firestore.googleapis.com/v1/projects/{pid}/databases/(default)/documents'

class Firestore:
    def __init__(self, sa_json):
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        self._Request = Request
        try:
            info = json.loads(sa_json)
        except Exception as e:
            log('=' * 60)
            log('ERROR: FIREBASE_SA_JSON no contiene un JSON válido.')
            log('Debe ser el contenido íntegro del archivo .json descargado de Firebase')
            log('(Project settings → Service accounts → Generate new private key).')
            log('Detalle: %s' % e)
            log('=' * 60)
            sys.exit(3)
        if 'private_key' not in info:
            log('ERROR: el JSON no trae la clave privada. Descárgalo de nuevo de Firebase.')
            sys.exit(3)
        self.pid = info.get('project_id')
        self.creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        self.base = BASE.format(pid=self.pid)

    def _token(self):
        if not self.creds.valid:
            self.creds.refresh(self._Request())
        return self.creds.token

    def _call(self, path, method='GET', body=None, timeout=25):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={'Authorization': 'Bearer ' + self._token(),
                     'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode() or '{}')
        except urllib.error.HTTPError as e:
            return {'__error__': e.code, '__detail__': e.read().decode()[:400]}
        except Exception as e:
            return {'__error__': -1, '__detail__': str(e)}

    # --- conversión de valores Firestore -------------------------------------
    @staticmethod
    def decode_value(v):
        for k in ('stringValue', 'booleanValue', 'doubleValue'):
            if k in v: return v[k]
        if 'integerValue' in v:
            try: return int(v['integerValue'])
            except Exception: return v['integerValue']
        if 'nullValue' in v: return None
        if 'timestampValue' in v: return str(v['timestampValue'])[:10]
        if 'mapValue' in v: return Firestore.decode_fields(v['mapValue'].get('fields', {}))
        if 'arrayValue' in v: return [Firestore.decode_value(x) for x in v['arrayValue'].get('values', [])]
        return None

    @staticmethod
    def decode_fields(fields):
        return {k: Firestore.decode_value(v) for k, v in (fields or {}).items()}

    @staticmethod
    def encode_value(v):
        if v is None: return {'nullValue': None}
        if isinstance(v, bool): return {'booleanValue': v}
        if isinstance(v, int): return {'integerValue': str(v)}
        if isinstance(v, float): return {'doubleValue': v}
        return {'stringValue': str(v)}

    # --- operaciones ----------------------------------------------------------
    def docs(self, collection, timeout=25):
        """Devuelve [(id, campos)] de una colección."""
        out, token = [], None
        for _ in range(20):
            path = '/' + collection + '?pageSize=300'
            if token: path += '&pageToken=' + urllib.parse.quote(token)
            res = self._call(path, timeout=timeout)
            if '__error__' in res:
                log('  ✗ Firestore(%s): HTTP %s %s' % (collection, res['__error__'], res.get('__detail__', '')[:200]))
                return out
            for d in res.get('documents', []):
                out.append((d['name'].split('/')[-1], self.decode_fields(d.get('fields', {}))))
            token = res.get('nextPageToken')
            if not token: break
        return out

    def get(self, path, timeout=25):
        res = self._call('/' + path, timeout=timeout)
        if '__error__' in res: return None
        return self.decode_fields(res.get('fields', {}))

    def set(self, collection, doc_id, fields, timeout=25):
        body = {'fields': {k: self.encode_value(v) for k, v in fields.items()}}
        path = '/' + collection + '?documentId=' + urllib.parse.quote(doc_id, safe='')
        return self._call(path, method='POST', body=body, timeout=timeout)

# ==================== plantillas y formato ====================================
OFFSETS = {
    'before':    ('ci', -3),
    'checkin':   ('ci',  0),
    'during':    ('ci',  1),
    'departure': ('co', -1),
    'after':     ('co',  0),
    'review':    ('co',  2),
}
MESES = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto',
         'septiembre','octubre','noviembre','diciembre']
DIAS = ['lunes','martes','miércoles','jueves','viernes','sábado','domingo']

def parse_date(s):
    if not s: return None
    if isinstance(s, dt.datetime): return s.date()
    if isinstance(s, dt.date): return s
    try: return dt.date.fromisoformat(str(s)[:10])
    except ValueError: return None

def fmt_long(d):
    if not d: return ''
    return f'{DIAS[d.weekday()]}, {d.day} de {MESES[d.month-1]} de {d.year}'

def chf(n):
    try: n = float(n)
    except (TypeError, ValueError): n = 0
    return 'CHF ' + f'{round(n):,}'.replace(',', "'")

def render(tpl, r, p, site_url):
    if tpl is None: return ''
    vals = {
        'guest':        str(r.get('first', '') or ''),
        'property':     p.get('name', '') or '',
        'checkin':      fmt_long(parse_date(r.get('ci'))),
        'checkout':     fmt_long(parse_date(r.get('co'))),
        'checkintime':  p.get('checkin', '') or '',
        'checkouttime': p.get('checkout', '') or '',
        'total':        chf(r.get('total')),
        'booking':      r.get('id', '') or '',
        'link':         site_url.rstrip('/') + '/#/stay/' + str(r.get('id', '')),
    }
    out = str(tpl)
    for k, v in vals.items():
        out = out.replace('{{%s}}' % k, str(v))
    return out

def html_wrap(text, site_url):
    body = (text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    body = body.replace('\n', '<br>')
    brand = (site_url or '').replace('https://', '').replace('http://', '').rstrip('/')
    return ('<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'color:#1b1b19;max-width:560px;margin:0 auto;padding:24px">'
            '<div style="font-size:20px;margin-bottom:18px">Architect\'s<i>House</i></div>'
            f'<div style="font-size:15px;line-height:1.7">{body}</div>'
            '<hr style="border:0;border-top:1px solid #e6e4dc;margin:26px 0">'
            f'<p style="font-size:11px;color:#9a9a94">{brand}</p></div>')

DEFAULT = {
    'confirm':   ('Your stay at {{property}} is confirmed',
                  'Dear {{guest}},\n\nThank you for booking directly with Architect’sHouse. Your stay at {{property}} from {{checkin}} to {{checkout}} is confirmed (booking {{booking}}).\n\nTotal paid: {{total}}\n\nEverything you need is on your stay page: {{link}}\n\nWarm regards,\nLucas, Architect’sHouse'),
    'before':    ('Your stay is coming up',
                  'Dear {{guest}},\n\nWe are looking forward to welcoming you at {{property}} on {{checkin}}. Your arrival details, parking and access code are on your stay page: {{link}}'),
    'checkin':   ('Here is everything you need for your arrival',
                  'Dear {{guest}},\n\nToday is the day. Check-in is from {{checkintime}}. Your key box code and Wi-Fi details are on your stay page: {{link}}'),
    'during':    ('Is everything going well?',
                  'Dear {{guest}},\n\nWe hope you are settling in well at {{property}}. If anything is missing, just reply to this message.'),
    'departure': ('Your check-out instructions',
                  'Dear {{guest}},\n\nCheck-out is until {{checkouttime}} tomorrow. A short checklist is on your stay page: {{link}}'),
    'after':     ('Thank you for staying with us',
                  'Dear {{guest}},\n\nThank you for staying at {{property}}. We hope to welcome you again — next time, book directly and save.'),
    'review':    ('We would love to hear about your experience',
                  'Dear {{guest}},\n\nCould you spare a minute to tell us about your stay at {{property}}? Your review helps other travellers and helps us improve.'),
}

def pick_template(settings, key):
    try:
        e = (settings or {}).get('emails', {}).get(key)
        if e and e.get('s') is not None:
            return e.get('s'), e.get('b')
    except Exception:
        pass
    return DEFAULT.get(key, ('', ''))

def due_messages(reservations, props, settings, site_url, today=None, catchup=2):
    today = today or TODAY()
    out = []
    for r in reservations:
        if str(r.get('status', '')) == 'cancelled': continue
        email = (r.get('email') or '').strip()
        if not email or '@' not in email: continue
        p = props.get(r.get('propId'), {})
        ci, co = parse_date(r.get('ci')), parse_date(r.get('co'))
        if not ci or not co: continue
        created = parse_date(r.get('created'))
        if created and 0 <= (today - created).days <= catchup:
            s, b = pick_template(settings, 'confirm')
            out.append(dict(key='confirm', res=r, prop=p, to=email,
                            subject=render(s, r, p, site_url), body=render(b, r, p, site_url)))
        for key, (field, delta) in OFFSETS.items():
            base = ci if field == 'ci' else co
            when = base + dt.timedelta(days=delta)
            lag = (today - when).days
            if lag < 0 or lag > catchup: continue
            if lag > 0 and (co + dt.timedelta(days=3)) < today: continue
            s, b = pick_template(settings, key)
            out.append(dict(key=key, res=r, prop=p, to=email,
                            subject=render(s, r, p, site_url), body=render(b, r, p, site_url)))
    return out

# ==================== Resend ==================================================
def send_resend(api_key, from_email, to, subject, body, site_url, reply_to=None, dry=False):
    payload = {'from': from_email, 'to': [to], 'subject': subject,
               'html': html_wrap(body, site_url), 'text': body}
    if reply_to: payload['reply_to'] = reply_to
    if dry:
        log('  [dry-run] -> %s | %s' % (to, subject))
        return True, 'dry-run'
    req = urllib.request.Request(
        'https://api.resend.com/emails', data=json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, json.loads(resp.read().decode()).get('id', '')
    except Exception as e:
        detail = ''
        try: detail = e.read().decode()[:300]
        except Exception: pass
        return False, '%s %s' % (e, detail)

# ==================== modos ===================================================
def selftest():
    today = dt.date(2026, 10, 20)
    props = {'p1': {'name': 'Maison du Lac', 'checkin': '16:00', 'checkout': '11:00'}}
    res = [{'id': 'AH0007', 'propId': 'p1', 'first': 'Lucas', 'last': 'Izquierdo',
            'email': 'lucas20.izquierdo@gmail.com', 'ci': '2026-10-20', 'co': '2026-10-23',
            'total': 1234.5, 'status': 'paid', 'created': '2026-10-20'}]
    msgs = due_messages(res, props, {}, 'https://architectshouse-78dd4.web.app', today=today)
    log('SELFTEST · hoy %s · %d mensaje(s)' % (today, len(msgs)))
    for m in msgs:
        log('\n--- %s -> %s' % (m['key'], m['to']))
        log('ASUNTO: ' + m['subject'])
        log(m['body'][:200])
    assert msgs, 'debería tocar al menos el correo de check-in'
    log('\nOK: la lógica funciona.')
    return 0

def check_mode():
    """Comprueba claves y conexión sin enviar nada."""
    sa = os.environ.get('FIREBASE_SA_JSON')
    api = os.environ.get('RESEND_API_KEY')
    frm = os.environ.get('FROM_EMAIL')
    ok = True
    log('[1/4] Comprobando secretos…')
    for n, v in (('FIREBASE_SA_JSON', sa), ('RESEND_API_KEY', api), ('FROM_EMAIL', frm)):
        estado = 'OK' if v else 'FALTA'
        if not v: ok = False
        log('      %-18s %s' % (n, estado))
    if not (sa and api and frm):
        log('\nFaltan secretos. Créalos en Settings → Secrets and variables → Actions.')
        return 2

    log('[2/4] Validando el JSON de Firebase…')
    try:
        info = json.loads(sa)
        log('      proyecto: %s' % info.get('project_id'))
        log('      cuenta:   %s' % info.get('client_email'))
    except Exception as e:
        log('      JSON inválido: %s' % e)
        return 3

    log('[3/4] Conectando con Firestore…')
    fs = Firestore(sa)
    props = dict((i, d) for i, d in fs.docs('props'))
    res = fs.docs('reservations')
    log('      alojamientos leídos: %d' % len(props))
    log('      reservas leídas:     %d' % len(res))
    if not props and not res:
        log('      AVISO: no he leído nada. Revisa permisos del service account.')
        ok = False

    log('[4/4] Comprobando la clave de Resend…')
    req = urllib.request.Request('https://api.resend.com/domains',
                                 headers={'Authorization': 'Bearer ' + api}, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            doms = [d.get('name') for d in (data.get('data') or [])]
            log('      clave válida. Dominios: %s' % (', '.join(doms) if doms else 'ninguno verificado'))
    except Exception as e:
        detail = ''
        try: detail = e.read().decode()[:200]
        except Exception: pass
        log('      clave NO válida: %s %s' % (e, detail))
        ok = False

    log('\nRESULTADO: %s' % ('todo correcto, ya puedes enviar' if ok else 'hay algo que arreglar'))
    return 0 if ok else 4

def main():
    if '--selftest' in sys.argv: return selftest()
    if '--check' in sys.argv:    return check_mode()
    dry = '--dry-run' in sys.argv or os.environ.get('DRY_RUN') == '1'

    def _timeout(signum, frame):
        log('\nTIEMPO AGOTADO (%ss). El programa se ha atascado; revisa la conexión con Firestore.' % WATCHDOG)
        sys.exit(4)
    try:
        signal.signal(signal.SIGALRM, _timeout); signal.alarm(WATCHDOG)
    except Exception:
        pass

    sa   = os.environ.get('FIREBASE_SA_JSON')
    api  = os.environ.get('RESEND_API_KEY')
    frm  = os.environ.get('FROM_EMAIL')
    site = os.environ.get('SITE_URL') or 'https://architectshouse-78dd4.web.app'
    reply = os.environ.get('REPLY_TO') or None
    catchup = int(os.environ.get('CATCHUP_DAYS', '2') or 2)

    faltan = [n for n, v in (('FIREBASE_SA_JSON', sa), ('RESEND_API_KEY', api), ('FROM_EMAIL', frm)) if not v]
    if faltan:
        log('Faltan variables de entorno: ' + ', '.join(faltan))
        log('Créalas en Settings → Secrets and variables → Actions.')
        return 2

    log('[1/5] Conectando con Firestore…')
    fs = Firestore(sa)
    log('      proyecto %s' % fs.pid)

    log('[2/5] Leyendo alojamientos…')
    props = dict((i, d) for i, d in fs.docs('props'))
    log('      %d alojamientos' % len(props))

    log('[3/5] Leyendo reservas…')
    reservations = [d for _, d in fs.docs('reservations')]
    log('      %d reservas' % len(reservations))

    log('[4/5] Leyendo plantillas…')
    settings = fs.get('private/data/meta/main').get('settings') or {}
    log('      plantillas: %s' % (', '.join(sorted((settings.get('emails') or {}).keys())) or 'ninguna (uso las de fábrica)'))

    pend = due_messages(reservations, props, settings, site, catchup=catchup)
    log('[5/5] Mensajes que tocan hoy: %d%s' % (len(pend), '  (DRY RUN)' if dry else ''))

    sent = skipped = failed = 0
    for m in pend:
        rid = m['res'].get('id', '')
        if not dry and fs.get('mail_log/%s_%s' % (rid, m['key'])) is not None:
            skipped += 1
            log('  · ya enviado: %s %s' % (rid, m['key']))
            continue
        ok, info = send_resend(api, frm, m['to'], m['subject'], m['body'], site, reply, dry)
        if ok:
            sent += 1
            if not dry:
                fs.set('mail_log', '%s_%s' % (rid, m['key']), {
                    'resId': rid, 'key': m['key'], 'to': m['to'], 'subject': m['subject'],
                    'date': TODAY().isoformat(), 'sentAt': dt.datetime.now(TZ).isoformat(),
                    'provider': 'resend', 'msgId': info, 'status': 'sent'})
            log('  ✓ %s %s -> %s' % (rid, m['key'], m['to']))
        else:
            failed += 1
            log('  ✗ %s %s -> %s: %s' % (rid, m['key'], m['to'], info))

    log('Resumen: %d enviados · %d ya enviados · %d fallidos' % (sent, skipped, failed))
    return 0

if __name__ == '__main__':
    sys.exit(main())
