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
BRAND = "Architect’sHouse"
WATCHDOG = 240          # segundos máximo de ejecución
UA = 'architects-house-mailer/1.0'   # Cloudflare bloquea peticiones sin User-Agent

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

# Hora mínima (hora local de Suiza) a la que se envía cada mensaje.
# Así, si a una estancia corta le tocan dos correos el mismo día, salen
# separados: las instrucciones por la mañana y la despedida por la tarde.
HORA = {
    'confirm':    0,    # en cuanto se hace la reserva
    'before':    10,
    'checkin':    9,    # primera hora de la mañana del día de llegada
    'during':    11,
    'departure': 17,    # la víspera de salir, a última hora de la tarde
    'after':     12,    # el día de salida, ya pasada la hora de salida
    'review':    12,
}


def ahora_local():
    """Hora actual en Suiza (el servidor de GitHub va en UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo('Europe/Zurich'))
    except Exception:
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
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

def stay_link(r, site_url):
    return (site_url or '').rstrip('/') + '/#/stay/' + str(r.get('id', '') or '')


def hero_image(prop):
    """Primera foto del alojamiento que sea una URL normal (ni data URI ni placeholders)."""
    if not prop:
        return ''
    cands = []
    if prop.get('photo'):
        cands.append(prop.get('photo'))
    for ph in (prop.get('photos') or []):
        cands.append(ph.get('src') if isinstance(ph, dict) else ph)
    for c in cands:
        if isinstance(c, str) and c.startswith('http') and len(c) < 400:
            return c
    return ''


def html_wrap(text, site_url, link='', prop=None):
    """Correo con marca: cabecera, foto del alojamiento, botón y pie."""
    body = (text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    body = body.replace('\n', '<br>')
    host = (site_url or '').replace('https://', '').replace('http://', '').rstrip('/')
    # Fondo transparente: así el logo se funde con la cabecera en modo claro
    # y en modo oscuro, sin dejar un rectángulo de otro color.
    logo = (os.environ.get('LOGO_URL')
            or '').strip()
    # El logo va dentro de una tabla que ocupa el 58% de la cabecera y la imagen
    # rellena esa tabla al 100%. Así, sea cual sea el ancho del móvil, el logo
    # guarda la proporción y nunca se estira de borde a borde.
    # La marca va como TEXTO, no como imagen: así se ve idéntica y nítida en Gmail,
    # en el móvil y en cualquier cliente, aunque el cliente bloquee las imágenes
    # (Gmail hace eso con los remitentes nuevos y deja un recuadro vacío).
    # Si se define LOGO_URL se sigue pudiendo usar una imagen.
    if logo.startswith('http'):
        cabecera = ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                    'class="ah-logo" style="width:58%%;max-width:250px">'
                    '<tr><td><img src="%s" alt="%s" width="1434" height="151" '
                    'style="width:100%%;height:auto;display:block;border:0" /></td></tr></table>'
                    % (logo, BRAND))
    else:
        cabecera = ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                    'class="ah-logo"><tr>'
                    '<td style="width:20px;height:20px;background:#B8956A;border-radius:3px;'
                    'line-height:20px;font-size:1px">&nbsp;</td>'
                    '<td class="ah-word" style="padding-left:11px;font-family:Georgia,'
                    "'Times New Roman',Times,serif;font-size:25px;line-height:1.15;"
                    'letter-spacing:.2px;color:#ffffff;white-space:nowrap">'
                    "Architect’s<span style=\"font-style:italic\">House</span></td>"
                    '</tr></table>')
    img = hero_image(prop)
    hero = ('<tr><td><img src="%s" alt="" style="width:100%%;display:block;'
            'max-height:240px;object-fit:cover"></td></tr>' % img) if img else ''
    boton = ('<tr><td style="padding:2px 30px 26px"><a href="%s" style="display:inline-block;'
             'background:#1b1b19;color:#ffffff;text-decoration:none;padding:12px 22px;'
             'border-radius:8px;font-size:14px">Open your stay page</a></td></tr>' % link) if link else ''
    return ('<div style="background:#f4f2ec;padding:26px 12px">'
            '<style>@media only screen and (max-width:620px){'
            '.ah-card{width:100%!important} .ah-word{font-size:21px!important}'
            '.ah-pad{padding-left:20px!important;padding-right:20px!important}}</style>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            '<tr><td align="center">'
            '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
            'class="ah-card" style="width:600px;max-width:100%;background:#ffffff;border-radius:14px;'
            'overflow:hidden;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'color:#1b1b19;text-align:left">'
            f'<tr><td class="ah-pad" style="background:#1b1b19;padding:20px 30px;color:#ffffff">{cabecera}</td></tr>'
            f'{hero}'
            f'<tr><td style="padding:28px 30px 18px;font-size:15px;line-height:1.75">{body}</td></tr>'
            f'{boton}'
            '<tr><td style="padding:18px 30px 24px;border-top:1px solid #efece4;'
            'font-size:11px;line-height:1.6;color:#9a9a94">'
            f'<b style="color:#6b6b64">{BRAND}</b> · {host}<br>'
            'You are receiving this email because you have a booking with us. '
            'Just reply to this message if you need anything.'
            '</td></tr>'
            '</table></td></tr></table></div>')

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

def due_messages(reservations, props, settings, site_url, today=None, catchup=2,
                hour=None):
    today = today or TODAY()
    out = []
    for r in reservations:
        if str(r.get('status', '')) == 'cancelled': continue
        email = (r.get('email') or '').strip()
        if not email or '@' not in email: continue
        p = props.get(r.get('propId'), {})
        ci, co = parse_date(r.get('ci')), parse_date(r.get('co'))
        if not ci or not co: continue
        noches = (co - ci).days
        created = parse_date(r.get('created'))
        if created and 0 <= (today - created).days <= catchup:
            s, b = pick_template(settings, 'confirm')
            out.append(dict(key='confirm', res=r, prop=p, to=email,
                            subject=render(s, r, p, site_url), body=render(b, r, p, site_url),
                            link=stay_link(r, site_url)))
        for key, (field, delta) in OFFSETS.items():
            if hour is not None and HORA.get(key, 0) > hour: continue
            if key == 'during' and noches <= 1: continue   # ya se ha ido ese mismo día
            base = ci if field == 'ci' else co
            when = base + dt.timedelta(days=delta)
            lag = (today - when).days
            if lag < 0 or lag > catchup: continue
            if lag > 0 and (co + dt.timedelta(days=3)) < today: continue
            s, b = pick_template(settings, key)
            out.append(dict(key=key, res=r, prop=p, to=email,
                            subject=render(s, r, p, site_url), body=render(b, r, p, site_url),
                            link=stay_link(r, site_url)))
    return out

# ==================== Modo prueba completa ====================================
# Para cada tipo de correo, fecha de entrada y noches que lo provocan hoy.
TEST_CASES = [
    ('confirm',    5, 3, 0),     # creada hoy
    ('before',     3, 3, None),  # entra en 3 días
    ('checkin',    0, 3, None),  # entra hoy
    ('during',    -1, 3, None),  # entró ayer
    ('departure', -2, 3, None),  # sale mañana
    ('after',     -3, 3, None),  # sale hoy
    ('review',    -5, 3, None),  # salió hace 2 días
]


def test_all_mode(fs, api, frm, site, reply, dry, test_to):
    """Crea una reserva falsa por cada tipo de correo y la envía a test_to.
    No escribe nada en Firestore: tus datos quedan intactos."""
    props = dict((i, d) for i, d in fs.docs('props'))
    settings = fs.get('private/data/meta/main').get('settings') or {}
    if props:
        prop_id, prop = list(props.items())[0]
    else:
        prop_id, prop = 'DEMO', {'name': "Maison du Lac", 'checkin': '16:00', 'checkout': '11:00'}
    log('Alojamiento de ejemplo: %s' % prop.get('name', prop_id))

    dest = (test_to or '').strip().lower()
    if not dest:
        log('Para este modo hay que indicar un correo: variable TEST_TO o: python send_emails.py --test-all tu@correo.com')
        return 2
    hoy = TODAY()
    enviados = fallidos = 0
    for key, d_ci, noches, creada in TEST_CASES:
        ci = hoy + dt.timedelta(days=d_ci)
        co = ci + dt.timedelta(days=noches)
        r = {'id': 'PRUEBA-' + key.upper(), 'propId': prop_id,
             'first': 'Lucas', 'last': 'Prueba', 'email': dest,
             'ci': ci.isoformat(), 'co': co.isoformat(),
             'total': 1234, 'status': 'confirmed'}
        if creada is not None:
            r['created'] = (hoy - dt.timedelta(days=creada)).isoformat()
        mensajes = [m for m in due_messages([r], props, settings, site, catchup=2)
                    if m['key'] == key]
        if not mensajes:
            log('  ✗ %-10s no se ha podido generar' % key)
            fallidos += 1
            continue
        m = mensajes[0]
        asunto = '[PRUEBA] ' + m['subject']
        ok, info = send_resend(api, frm, dest, asunto, m['body'], site, reply, dry,
                                     m.get('link', ''), m.get('prop'))
        if ok:
            enviados += 1
            log('  ✓ %-10s -> %s  | %s' % (key, dest, asunto))
        else:
            fallidos += 1
            log('  ✗ %-10s -> %s: %s' % (key, dest, info))
    log('Resumen prueba: %d enviados · %d fallidos' % (enviados, fallidos))
    return 0 if fallidos == 0 else 4


# ==================== Resend ==================================================
def send_resend(api_key, from_email, to, subject, body, site_url, reply_to=None, dry=False,
                link='', prop=None):
    payload = {'from': from_email, 'to': [to], 'subject': subject,
               'html': html_wrap(body, site_url, link, prop), 'text': body}
    if reply_to: payload['reply_to'] = reply_to
    if dry:
        log('  [dry-run] -> %s | %s' % (to, subject))
        return True, 'dry-run'
    req = urllib.request.Request(
        'https://api.resend.com/emails', data=json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json',
                 'User-Agent': UA},
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
                                 headers={'Authorization': 'Bearer ' + api, 'User-Agent': UA},
                                 method='GET')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            doms = [d.get('name') for d in (data.get('data') or [])]
            log('      clave válida. Dominios: %s' % (', '.join(doms) if doms else 'ninguno verificado'))
            if not doms:
                log('      AVISO: sin dominio verificado solo se puede enviar en modo prueba.')
    except urllib.error.HTTPError as e:
        try: detail = e.read().decode()[:200]
        except Exception: detail = ''
        nombre = ''
        try: nombre = (json.loads(detail) or {}).get('name', '') or ''
        except Exception: nombre = ''
        if e.code == 403 or nombre == 'restricted_api_key':
            log('      clave de SOLO ENVÍO: es válida, pero no puede listar dominios.')
            log('      Es la opción recomendada y segura; se verificará al enviar.')
        else:
            log('      clave NO válida (HTTP %s): %s' % (e.code, detail))
            ok = False
    except Exception as e:
        log('      no se pudo comprobar: %s' % e)

    log('\nRESULTADO: %s' % ('todo correcto, ya puedes enviar' if ok else 'hay algo que arreglar'))
    return 0 if ok else 4

def main():
    if '--selftest' in sys.argv: return selftest()
    if '--check' in sys.argv:    return check_mode()
    dry = '--dry-run' in sys.argv or os.environ.get('DRY_RUN') == '1'
    test_all = '--test-all' in sys.argv
    if test_all:
        i = sys.argv.index('--test-all')
        if i + 1 < len(sys.argv) and '@' in sys.argv[i + 1]:
            os.environ['TEST_TO'] = sys.argv[i + 1].strip()

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
    # Se normaliza a minúsculas: Resend compara la dirección tal cual y
    # rechaza "Lucas20.…" si la cuenta es "lucas20.…".
    test_to = (os.environ.get('TEST_TO') or '').strip().lower()   # modo prueba
    if test_to:
        log('MODO PRUEBA ACTIVADO: todos los correos irán a %s' % test_to)
        log('(no se guarda registro, así que luego se volverán a enviar de verdad)')

    if frm and '<' not in frm:
        frm = '%s <%s>' % (BRAND, frm.strip())
        log('      remitente: %s' % frm)

    faltan = [n for n, v in (('FIREBASE_SA_JSON', sa), ('RESEND_API_KEY', api), ('FROM_EMAIL', frm)) if not v]
    if faltan:
        log('Faltan variables de entorno: ' + ', '.join(faltan))
        log('Créalas en Settings → Secrets and variables → Actions.')
        return 2

    log('[1/5] Conectando con Firestore…')
    fs = Firestore(sa)
    log('      proyecto %s' % fs.pid)

    if test_all:
        log('MODO PRUEBA COMPLETA: un correo de cada tipo, todos a %s' % (test_to or '—'))
        return test_all_mode(fs, api, frm, site, reply, dry, test_to)

    log('[2/5] Leyendo alojamientos…')
    props = dict((i, d) for i, d in fs.docs('props'))
    log('      %d alojamientos' % len(props))

    log('[3/5] Leyendo reservas…')
    reservations = [d for _, d in fs.docs('reservations')]
    log('      %d reservas' % len(reservations))

    log('[4/5] Leyendo plantillas…')
    settings = fs.get('private/data/meta/main').get('settings') or {}
    log('      plantillas: %s' % (', '.join(sorted((settings.get('emails') or {}).keys())) or 'ninguna (uso las de fábrica)'))

    ahora = ahora_local()
    log('      hora local Suiza: %s' % ahora.strftime('%H:%M'))
    pend = due_messages(reservations, props, settings, site, catchup=catchup,
                        hour=ahora.hour)
    log('[5/5] Mensajes que tocan hoy: %d%s' % (len(pend), '  (DRY RUN)' if dry else ''))

    sent = skipped = failed = 0
    for m in pend:
        rid = m['res'].get('id', '')
        if not dry and not test_to and fs.get('mail_log/%s_%s' % (rid, m['key'])) is not None:
            skipped += 1
            log('  · ya enviado: %s %s' % (rid, m['key']))
            continue
        dest   = test_to or m['to']
        asunto = ('[PRUEBA] ' + m['subject']) if test_to else m['subject']
        ok, info = send_resend(api, frm, dest, asunto, m['body'], site, reply, dry,
                                     m.get('link', ''), m.get('prop'))
        if ok:
            sent += 1
            if not dry and not test_to:
                fs.set('mail_log', '%s_%s' % (rid, m['key']), {
                    'resId': rid, 'key': m['key'], 'to': dest, 'subject': asunto,
                    'date': TODAY().isoformat(), 'sentAt': dt.datetime.now(TZ).isoformat(),
                    'provider': 'resend', 'msgId': info, 'status': 'sent'})
            log('  ✓ %s %s -> %s%s' % (rid, m['key'], dest, '  [PRUEBA]' if test_to else ''))
        else:
            failed += 1
            log('  ✗ %s %s -> %s: %s' % (rid, m['key'], dest, info))

    log('Resumen: %d enviados · %d ya enviados · %d fallidos' % (sent, skipped, failed))
    return 0

if __name__ == '__main__':
    sys.exit(main())
