#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
send_emails.py · Architect's House
Envía automáticamente los correos de reserva leyendo Firestore y mandándolos con Resend.

Correos que envía (las mismas plantillas que editas en Ajustes → plantillas de correo):
  confirm    al reservar (hasta 2 días después si el cron estaba parado)
  before     3 días antes de la entrada
  checkin    el mismo día de la entrada
  during     1 día después de la entrada
  departure  1 día antes de la salida
  after      el día de la salida
  review     2 días después de la salida

Uso:
  python send_emails.py            # envío real
  python send_emails.py --dry-run  # muestra qué enviaría, sin mandar nada
  python send_emails.py --selftest # prueba la lógica con datos falsos (sin red)

Variables de entorno:
  FIREBASE_SA_JSON   contenido del JSON de la cuenta de servicio de Firebase
  RESEND_API_KEY     clave de API de Resend
  FROM_EMAIL         remitente, p.ej. "Architect's House <reservas@tudominio.ch>"
  REPLY_TO           (opcional) dirección de respuesta
  SITE_URL           (opcional) https://architectshouse-78dd4.web.app  (para {{link}})
  CATCHUP_DAYS       (opcional, 2) días de retraso que aún se envían
  DRY_RUN            (opcional) 1 = no enviar
"""
import os, sys, json, datetime as dt

TZ = dt.timezone(dt.timedelta(hours=2))  # Europe/Zurich (verano); el invierno da igual a estos efectos
TODAY = lambda: dt.datetime.now(TZ).date()

# --- desplazamientos: clave -> (campo, días)  (idéntico a la web) -------------
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

# --- utilidades de formato (equivalentes a las de la web) ---------------------
def parse_date(s):
    if not s: return None
    if isinstance(s, dt.datetime): return s.date()
    if isinstance(s, dt.date): return s
    s = str(s)[:10]
    try: return dt.date.fromisoformat(s)
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
    link = site_url.rstrip('/') + '/#/stay/' + str(r.get('id', ''))
    vals = {
        'guest':         (str(r.get('first', '')).strip() or '').strip(),
        'property':      p.get('name', '') or '',
        'checkin':       fmt_long(parse_date(r.get('ci'))),
        'checkout':      fmt_long(parse_date(r.get('co'))),
        'checkintime':   p.get('checkin', '') or '',
        'checkouttime':  p.get('checkout', '') or '',
        'total':         chf(r.get('total')),
        'booking':       r.get('id', '') or '',
        'link':          link,
    }
    out = str(tpl)
    for k, v in vals.items():
        out = out.replace('{{%s}}' % k, str(v))
    return out

def html_wrap(text, site_url):
    body = (text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    body = body.replace('\n', '<br>')
    brand = (site_url or '').replace('https://', '').replace('http://', '').rstrip('/')
    return f'''<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1b1b19;max-width:560px;margin:0 auto;padding:24px">
  <div style="font-size:20px;margin-bottom:18px">Architect's<i>House</i></div>
  <div style="font-size:15px;line-height:1.7">{body}</div>
  <hr style="border:0;border-top:1px solid #e6e4dc;margin:26px 0">
  <p style="font-size:11px;color:#9a9a94">{brand}</p>
</div>'''

# --- plantillas por defecto (si aún no hay nada en Firestore) -----------------
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

# --- qué toca enviar hoy ------------------------------------------------------
def due_messages(reservations, props, settings, site_url, today=None, catchup=2):
    """Devuelve lista de dicts listos para enviar."""
    today = today or TODAY()
    out = []
    for r in reservations:
        if str(r.get('status', '')) == 'cancelled': continue
        email = (r.get('email') or '').strip()
        if not email or '@' not in email: continue
        p = props.get(r.get('propId'), {})
        ci, co = parse_date(r.get('ci')), parse_date(r.get('co'))
        if not ci or not co: continue

        # confirmación: cuando la reserva es reciente (hoy o los 2 días anteriores)
        created = parse_date(r.get('created'))
        if created and 0 <= (today - created).days <= catchup:
            s, b = pick_template(settings, 'confirm')
            out.append(dict(key='confirm', res=r, prop=p, to=email,
                            subject=render(s, r, p, site_url), body=render(b, r, p, site_url)))

        # resto: por fecha relativa a entrada/salida
        for key, (field, delta) in OFFSETS.items():
            base = ci if field == 'ci' else co
            when = base + dt.timedelta(days=delta)
            lag = (today - when).days
            if lag < 0 or lag > catchup: continue     # aún no toca, o demasiado viejo
            if lag > 0 and (co + dt.timedelta(days=3)) < today: continue
            s, b = pick_template(settings, key)
            out.append(dict(key=key, res=r, prop=p, to=email,
                            subject=render(s, r, p, site_url), body=render(b, r, p, site_url)))
    return out

# --- envío con Resend ---------------------------------------------------------
def send_resend(api_key, from_email, to, subject, body, site_url, reply_to=None, dry=False):
    import urllib.request
    payload = {
        'from': from_email,
        'to': [to],
        'subject': subject,
        'html': html_wrap(body, site_url),
        'text': body,
    }
    if reply_to: payload['reply_to'] = reply_to
    if dry:
        print(f'  [dry-run] -> {to} | {subject}')
        return True, 'dry-run'
    req = urllib.request.Request(
        'https://api.resend.com/emails',
        data=json.dumps(payload).encode(),
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, json.loads(resp.read().decode()).get('id', '')
    except Exception as e:
        detail = ''
        try: detail = e.read().decode()[:300]
        except Exception: pass
        return False, f'{e} {detail}'

# --- Firestore ----------------------------------------------------------------
def load_firestore(sa_json):
    from google.cloud import firestore
    from google.oauth2 import service_account
    info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(info)
    return firestore.Client(project=info.get('project_id'), credentials=creds)

def fetch(db):
    props = {}
    for d in db.collection('props').stream():
        props[d.id] = d.to_dict() or {}
    reservations = [d.to_dict() for d in db.collection('reservations').stream()]
    settings = {}
    try:
        meta = db.document('private/data/meta/main').get()
        if meta.exists:
            settings = (meta.to_dict() or {}).get('settings') or {}
    except Exception as e:
        print('  aviso: no se pudo leer settings:', e)
    return props, reservations, settings

def already_sent(db, res_id, key):
    return db.document(f'mail_log/{res_id}_{key}').get().exists

def mark_sent(db, res_id, key, to, subject, when, msg_id):
    db.document(f'mail_log/{res_id}_{key}').set({
        'resId': res_id, 'key': key, 'to': to, 'subject': subject,
        'date': when.isoformat(), 'sentAt': dt.datetime.now(TZ).isoformat(),
        'provider': 'resend', 'msgId': msg_id, 'status': 'sent',
    })

# --- selftest (sin red) -------------------------------------------------------
def selftest():
    today = dt.date(2026, 10, 20)
    props = {'p1': {'name': 'Maison du Lac', 'checkin': '16:00', 'checkout': '11:00'}}
    res = [{'id': 'AH0007', 'propId': 'p1', 'first': 'Lucas', 'last': 'Izquierdo',
            'email': 'lucas20.izquierdo@gmail.com', 'ci': '2026-10-20', 'co': '2026-10-23',
            'total': 1234.5, 'status': 'paid', 'created': '2026-10-20'}]
    msgs = due_messages(res, props, {}, 'https://architectshouse-78dd4.web.app', today=today)
    print(f'SELFTEST · hoy {today} · {len(msgs)} mensaje(s) que tocarían:')
    for m in msgs:
        print(f'\n--- {m["key"]} -> {m["to"]}')
        print('ASUNTO:', m['subject'])
        print(m['body'][:220].replace('\n', ' | '))
    assert msgs, 'debería tocar al menos el correo de check-in'
    print('\nOK: la lógica funciona.')
    return 0

# --- main ---------------------------------------------------------------------
def main():
    if '--selftest' in sys.argv:
        return selftest()
    dry = '--dry-run' in sys.argv or os.environ.get('DRY_RUN') == '1'
    sa = os.environ.get('FIREBASE_SA_JSON')
    api = os.environ.get('RESEND_API_KEY')
    frm = os.environ.get('FROM_EMAIL')
    site = os.environ.get('SITE_URL') or 'https://architectshouse-78dd4.web.app'
    reply = os.environ.get('REPLY_TO') or None
    catchup = int(os.environ.get('CATCHUP_DAYS', '2') or 2)

    missing = [n for n, v in (('FIREBASE_SA_JSON', sa), ('RESEND_API_KEY', api), ('FROM_EMAIL', frm)) if not v]
    if missing:
        print('Faltan variables de entorno:', ', '.join(missing)); return 2

    db = load_firestore(sa)
    props, reservations, settings = fetch(db)
    print(f'Firestore: {len(props)} alojamientos · {len(reservations)} reservas')

    pend = due_messages(reservations, props, settings, site, catchup=catchup)
    print(f'Mensajes que tocan hoy: {len(pend)}')

    sent = skipped = failed = 0
    for m in pend:
        rid = m['res'].get('id', '')
        if not dry and already_sent(db, rid, m['key']):
            skipped += 1
            print(f'  · ya enviado: {rid} {m["key"]}')
            continue
        ok, info = send_resend(api, frm, m['to'], m['subject'], m['body'], site, reply, dry)
        if ok:
            sent += 1
            if not dry:
                try: mark_sent(db, rid, m['key'], m['to'], m['subject'], TODAY(), info)
                except Exception as e: print('  aviso: no se pudo registrar:', e)
            print(f'  ✓ {rid} {m["key"]} -> {m["to"]}')
        else:
            failed += 1
            print(f'  ✗ {rid} {m["key"]} -> {m["to"]}: {info}')

    print(f'Resumen: {sent} enviados · {skipped} ya enviados · {failed} fallidos')
    return 0

if __name__ == '__main__':
    sys.exit(main())
