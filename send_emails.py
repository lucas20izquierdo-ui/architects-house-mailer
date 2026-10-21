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
import time
import urllib.request, urllib.error, urllib.parse

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo('Europe/Zurich')      # hora de Suiza: cambia sola en verano/invierno
except Exception:
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
        if isinstance(v, dict):
            return {'mapValue': {'fields': dict((str(k), Firestore.encode_value(x))
                                                for k, x in v.items())}}
        if isinstance(v, (list, tuple)):
            return {'arrayValue': {'values': [Firestore.encode_value(x) for x in v]}}
        return {'stringValue': str(v)}

    def patch(self, path, campos, timeout=25):
        """Actualiza sólo los campos indicados, sin tocar el resto del documento."""
        mask = '&'.join('updateMask.fieldPaths=' + urllib.parse.quote(c, safe='')
                        for c in campos)
        body = {'fields': {k: self.encode_value(v) for k, v in campos.items()}}
        return self._call('/' + path + '?' + mask, method='PATCH', body=body,
                          timeout=timeout)

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
        res = self._call('/' + urllib.parse.quote(path, safe='/'), timeout=timeout)
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
MESES = {
    'es': ['enero','febrero','marzo','abril','mayo','junio','julio','agosto',
           'septiembre','octubre','noviembre','diciembre'],
    'en': ['January','February','March','April','May','June','July',
           'August','September','October','November','December'],
    'fr': ['janvier','février','mars','avril','mai','juin','juillet',
           'août','septembre','octobre','novembre','décembre'],
    'it': ['gennaio','febbraio','marzo','aprile','maggio','giugno','luglio',
           'agosto','settembre','ottobre','novembre','dicembre'],
}
DIAS = {
    'es': ['lunes','martes','miércoles','jueves','viernes','sábado','domingo'],
    'en': ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'],
    'fr': ['lundi','mardi','mercredi','jeudi','vendredi','samedi','dimanche'],
    'it': ['lunedì','martedì','mercoledì','giovedì','venerdì','sabato','domenica'],
}

def parse_date(s):
    if not s: return None
    if isinstance(s, dt.datetime): return s.date()
    if isinstance(s, dt.date): return s
    try: return dt.date.fromisoformat(str(s)[:10])
    except ValueError: return None

def fmt_long(d, lang=None):
    """Fecha larga en el idioma del huésped (si no se sabe, en inglés)."""
    if not d: return ''
    L = str(lang or 'en').split('-')[0].lower()
    if L not in MESES: L = 'en'
    dia, mes = DIAS[L][d.weekday()], MESES[L][d.month - 1]
    if L == 'en': return f'{dia}, {mes} {d.day}, {d.year}'
    if L == 'fr': return f'{dia} {d.day} {mes} {d.year}'
    if L == 'it': return f'{dia} {d.day} {mes} {d.year}'
    return f'{dia}, {d.day} de {mes} de {d.year}'

def chf(n):
    try: n = float(n)
    except (TypeError, ValueError): n = 0
    return 'CHF ' + f'{round(n):,}'.replace(',', "'")

def render(tpl, r, p, site_url, lang=None):
    if tpl is None: return ''
    idi = str(lang or r.get('lang') or 'en').split('-')[0].lower()
    vals = {
        'guest':        str(r.get('first', '') or ''),
        'property':     p.get('name', '') or '',
        'checkin':      fmt_long(parse_date(r.get('ci')), idi),
        'checkout':     fmt_long(parse_date(r.get('co')), idi),
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


def html_wrap(text, site_url, link='', prop=None, lang='en'):
    """Correo sencillo y limpio: nombre en texto, cuerpo, botón y pie.

    Sin imágenes y sin franjas de color: así se ve igual en Gmail, en el móvil
    y en modo oscuro, y nunca aparece un hueco de imagen bloqueada.
    """
    t = TEXTS.get(str(lang or 'en').split('-')[0].lower(), TEXTS['en'])
    body = (text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    body = body.replace('\n', '<br>')
    host = (site_url or '').replace('https://', '').replace('http://', '').rstrip('/')
    marca = ('<div style="font-family:Georgia,\'Times New Roman\',Times,serif;font-size:21px;'
             'letter-spacing:-.01em;line-height:1.25;color:#1b1b19">Architect’s'
             '<i style="font-style:italic">House</i></div>')
    boton = ('<a href="%s" style="display:inline-block;background:#1b1b19;color:#ffffff;'
             'text-decoration:none;padding:12px 22px;border-radius:8px;font-size:14px">'
             '%s</a>' % (link, t['boton'])) if link else ''
    fila_boton = ('<tr><td class="ah-pad" style="padding:2px 30px 28px">%s</td></tr>' % boton) if boton else ''
    idi = str(lang or 'en').split('-')[0].lower()
    return ('<!doctype html><html lang="%s"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="only light">'
            '<meta name="supported-color-schemes" content="only light">'
            '<title>Architect’sHouse</title>'
            '<style>:root{color-scheme:only light}'
            '@media only screen and (max-width:620px){'
            '.ah-pad{padding-left:20px!important;padding-right:20px!important}}</style>'
            '</head><body style="margin:0;background:#ffffff">'
            '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" '
            'style="background:#ffffff"><tr><td align="center">'
            '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
            'class="ah-card" style="width:600px;max-width:100%%;'
            'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1b1b19;'
            'text-align:left">'
            '<tr><td class="ah-pad" style="padding:26px 30px 16px">%s</td></tr>'
            '<tr><td><div style="border-top:1px solid #efece4"></div></td></tr>'
            '<tr><td class="ah-pad" style="padding:24px 30px 16px;font-size:15px;line-height:1.75">%s</td></tr>'
            '%s'
            '<tr><td class="ah-pad" style="padding:18px 30px 28px;border-top:1px solid #efece4;'
            'font-size:11px;line-height:1.6;color:#9a9a94">'
            '<b style="color:#6b6b64">%s</b> · %s<br>'
            '%s'
            '</td></tr></table></td></tr></table></body></html>'
            % (idi, marca, body, fila_boton, BRAND, host, t['pie']))


# ------------------------------------------------------------------
# Plantillas de fabrica en los cuatro idiomas de la web.
# El idioma lo decide la reserva: es el que el huesped estaba usando
# cuando reservo. Si no se sabe (reservas antiguas o creadas a mano),
# se usa el ingles. Puedes reescribir cualquier texto desde el panel:
# Ajustes -> Email templates -> desplegable de idioma.
# ------------------------------------------------------------------
DEFAULT = {
    'en': {
        'welcome': ("Your 10% code for your first stay",
                "Hello,\n\nThank you for subscribing to Architect’sHouse. Here is your personal code for 10% off your first booking:\n\n    {{code}}\n\nEnter it at checkout. It is a single-use code, valid on your first booking, and the checkout form will fill it in for you automatically.\n\nWarm regards,\nLucas, Architect’sHouse"),
        'confirm': ("Your stay at {{property}} is confirmed",
                "Dear {{guest}},\n\nThank you for booking directly with Architect’sHouse. Your stay at {{property}} from {{checkin}} to {{checkout}} is confirmed (booking {{booking}}).\n\nTotal paid: {{total}}\n\nEverything you need is on your stay page.\n\nWarm regards,\nLucas, Architect’sHouse"),
        'before': ("Your stay is coming up",
                "Dear {{guest}},\n\nWe are looking forward to welcoming you at {{property}} on {{checkin}}. Your arrival details, parking and access code are on your stay page."),
        'checkin': ("Here is everything you need for your arrival",
                "Dear {{guest}},\n\nToday is the day. Check-in is from {{checkintime}}. Your key box code and Wi-Fi details are on your stay page."),
        'during': ("Is everything going well?",
                "Dear {{guest}},\n\nWe hope you are settling in well at {{property}}. If anything is missing, just reply to this message."),
        'departure': ("Your check-out instructions",
                "Dear {{guest}},\n\nCheck-out is until {{checkouttime}} tomorrow. A short checklist is on your stay page."),
        'after': ("Thank you for staying with us",
                "Dear {{guest}},\n\nThank you for staying at {{property}}. We hope to welcome you again — next time, book directly and save."),
        'review': ("We would love to hear about your experience",
                "Dear {{guest}},\n\nCould you spare a minute to tell us about your stay at {{property}}? Your review helps other travellers and helps us improve."),
    },
    'es': {
        'welcome': ("Tu código del 10 % para tu primera estancia",
                "Hola,\n\nGracias por suscribirte a Architect’sHouse. Aquí tienes tu código personal del 10 % de descuento en tu primera reserva:\n\n    {{code}}\n\nIntrodúcelo al pagar. Es un código de un solo uso, válido en tu primera reserva, y el formulario lo rellenará por ti automáticamente.\n\nUn saludo,\nLucas, Architect’sHouse"),
        'confirm': ("Tu estancia en {{property}} está confirmada",
                "Hola {{guest}},\n\nGracias por reservar directamente con Architect’sHouse. Tu estancia en {{property}} del {{checkin}} al {{checkout}} queda confirmada (reserva {{booking}}).\n\nTotal pagado: {{total}}\n\nToda la información de tu estancia está en tu página.\n\nUn saludo,\nLucas, Architect’sHouse"),
        'before': ("Tu estancia está al caer",
                "Hola {{guest}},\n\nTenemos muchas ganas de recibirte en {{property}} el {{checkin}}. Los detalles de la llegada, el aparcamiento y el código de acceso están en tu página de estancia."),
        'checkin': ("Todo lo que necesitas para tu llegada",
                "Hola {{guest}},\n\nHoy es el día. La entrada es a partir de las {{checkintime}}. El código de la caja de llaves y el wifi están en tu página de estancia."),
        'during': ("¿Todo va bien?",
                "Hola {{guest}},\n\nEsperamos que te estés instalando bien en {{property}}. Si te falta cualquier cosa, responde a este mismo correo."),
        'departure': ("Instrucciones para la salida",
                "Hola {{guest}},\n\nLa salida es hasta las {{checkouttime}} de mañana. Tienes una lista breve para la salida en tu página de estancia."),
        'after': ("Gracias por alojarte con nosotros",
                "Hola {{guest}},\n\nGracias por alojarte en {{property}}. Esperamos volver a recibirte: la próxima vez, reserva directo y ahorra."),
        'review': ("Nos encantaría conocer tu experiencia",
                "Hola {{guest}},\n\n¿Nos dedicas un minuto para contarnos qué tal tu estancia en {{property}}? Tu opinión ayuda a otros viajeros y nos ayuda a mejorar."),
    },
    'fr': {
        'welcome': ("Votre code de 10 % pour votre premier séjour",
                "Bonjour,\n\nMerci de vous être abonné à Architect’sHouse. Voici votre code personnel de 10 % de réduction sur votre première réservation :\n\n    {{code}}\n\nSaisissez-le au moment du paiement. Il est à usage unique, valable sur votre première réservation, et le formulaire le remplira automatiquement pour vous.\n\nBien à vous,\nLucas, Architect’sHouse"),
        'confirm': ("Votre séjour à {{property}} est confirmé",
                "Bonjour {{guest}},\n\nMerci d’avoir réservé directement auprès d’Architect’sHouse. Votre séjour à {{property}}, du {{checkin}} au {{checkout}}, est confirmé (réservation {{booking}}).\n\nTotal réglé : {{total}}\n\nToutes les informations utiles se trouvent sur la page de votre séjour.\n\nBien à vous,\nLucas, Architect’sHouse"),
        'before': ("Votre séjour approche",
                "Bonjour {{guest}},\n\nNous avons hâte de vous accueillir à {{property}} le {{checkin}}. Les détails d’arrivée, le stationnement et le code d’accès se trouvent sur la page de votre séjour."),
        'checkin': ("Tout ce qu’il vous faut pour votre arrivée",
                "Bonjour {{guest}},\n\nC’est le grand jour. L’arrivée se fait à partir de {{checkintime}}. Le code de la boîte à clés et le Wi-Fi se trouvent sur la page de votre séjour."),
        'during': ("Tout se passe bien ?",
                "Bonjour {{guest}},\n\nNous espérons que vous vous installez bien à {{property}}. S’il vous manque quelque chose, répondez simplement à ce message."),
        'departure': ("Vos instructions de départ",
                "Bonjour {{guest}},\n\nLe départ se fait jusqu’à {{checkouttime}} demain. Une petite liste vous attend sur la page de votre séjour."),
        'after': ("Merci d’avoir séjourné chez nous",
                "Bonjour {{guest}},\n\nMerci d’avoir séjourné à {{property}}. Nous espérons vous accueillir à nouveau : la prochaine fois, réservez en direct et économisez."),
        'review': ("Votre avis nous intéresse",
                "Bonjour {{guest}},\n\nAuriez-vous une minute pour nous parler de votre séjour à {{property}} ? Votre avis aide d’autres voyageurs et nous aide à nous améliorer."),
    },
    'it': {
        'welcome': ("Il tuo codice del 10 % per il primo soggiorno",
                "Ciao,\n\nGrazie per esserti iscritto ad Architect’sHouse. Ecco il tuo codice personale per il 10 % di sconto sulla prima prenotazione:\n\n    {{code}}\n\nInseriscilo al momento del pagamento. È un codice valido una sola volta, sulla tua prima prenotazione, e il modulo lo compilerà automaticamente per te.\n\nUn caro saluto,\nLucas, Architect’sHouse"),
        'confirm': ("Il tuo soggiorno a {{property}} è confermato",
                "Ciao {{guest}},\n\nGrazie per aver prenotato direttamente con Architect’sHouse. Il tuo soggiorno a {{property}} dal {{checkin}} al {{checkout}} è confermato (prenotazione {{booking}}).\n\nTotale pagato: {{total}}\n\nTutto quello che ti serve è nella pagina del tuo soggiorno.\n\nUn caro saluto,\nLucas, Architect’sHouse"),
        'before': ("Il tuo soggiorno si avvicina",
                "Ciao {{guest}},\n\nNon vediamo l’ora di accoglierti a {{property}} il {{checkin}}. I dettagli per l’arrivo, il parcheggio e il codice di accesso sono nella pagina del tuo soggiorno."),
        'checkin': ("Tutto quello che ti serve per l’arrivo",
                "Ciao {{guest}},\n\nÈ arrivato il giorno. Il check-in è dalle {{checkintime}}. Il codice della cassetta delle chiavi e il Wi-Fi sono nella pagina del tuo soggiorno."),
        'during': ("Tutto bene?",
                "Ciao {{guest}},\n\nSperiamo che tu ti stia ambientando bene a {{property}}. Se ti manca qualcosa, rispondi pure a questo messaggio."),
        'departure': ("Le istruzioni per la partenza",
                "Ciao {{guest}},\n\nIl check-out è entro le {{checkouttime}} di domani. Trovi una breve lista per la partenza nella pagina del tuo soggiorno."),
        'after': ("Grazie per aver soggiornato con noi",
                "Ciao {{guest}},\n\nGrazie per aver soggiornato a {{property}}. Speriamo di ospitarti di nuovo: la prossima volta, prenota direttamente e risparmia."),
        'review': ("Ci piacerebbe conoscere la tua esperienza",
                "Ciao {{guest}},\n\nHai un minuto per raccontarci com’è andato il tuo soggiorno a {{property}}? La tua recensione aiuta altri viaggiatori e aiuta noi a migliorare."),
    },
}


# Textos fijos del diseño del correo: el botón y la línea del pie.
TEXTS = {
    'en': {'boton': "Open your stay page",
            'pie': "You are receiving this email because you have a booking with us. Just reply to this message if you need anything."},
    'es': {'boton': "Abrir la página de mi estancia",
            'pie': "Recibes este correo porque tienes una reserva con nosotros. Si necesitas cualquier cosa, responde a este mismo mensaje."},
    'fr': {'boton': "Ouvrir la page de mon séjour",
            'pie': "Vous recevez cet e-mail parce que vous avez une réservation chez nous. Si vous avez besoin de quoi que ce soit, répondez simplement à ce message."},
    'it': {'boton': "Apri la pagina del mio soggiorno",
            'pie': "Ricevi questa e-mail perché hai una prenotazione con noi. Se ti serve qualsiasi cosa, rispondi pure a questo messaggio."},
}


def pick_template(settings, key, lang='en'):
    """Texto de un correo en un idioma concreto.

    Orden de preferencia:
      1. el que hayas escrito tú en el panel para ese idioma (s_es, b_es…);
      2. la plantilla de fábrica en ese idioma;
      3. para el inglés (y los idiomas que no estén traducidos), la tuya del
         panel y, si no hay ninguna, la de fábrica en inglés.
    """
    try:
        e = ((settings or {}).get('emails') or {}).get(key) or {}
    except Exception:
        e = {}
    idi = str(lang or 'en').split('-')[0].lower()
    limpio = lambda t: t if (t is not None and str(t).strip()) else None
    if idi in ('es', 'fr', 'it'):
        fab = DEFAULT.get(idi, DEFAULT['en']).get(key, ('', ''))
        return (limpio(e.get('s_' + idi)) or fab[0],
                limpio(e.get('b_' + idi)) or fab[1])
    fab = DEFAULT['en'].get(key, ('', ''))
    return limpio(e.get('s')) or fab[0], limpio(e.get('b')) or fab[1]

def due_messages(reservations, props, settings, site_url, today=None, catchup=2,
                hour=None):
    today = today or TODAY()
    out = []
    for r in reservations:
        if str(r.get('status', '')) == 'cancelled': continue
        email = (r.get('email') or '').strip()
        if not email or '@' not in email: continue
        p = props.get(r.get('propId'), {})
        idi = str(r.get('lang') or 'en').split('-')[0].lower()
        ci, co = parse_date(r.get('ci')), parse_date(r.get('co'))
        if not ci or not co: continue
        noches = (co - ci).days
        created = parse_date(r.get('created'))
        if created and 0 <= (today - created).days <= catchup:
            s, b = pick_template(settings, 'confirm', idi)
            out.append(dict(key='confirm', res=r, prop=p, to=email, lang=idi,
                            subject=render(s, r, p, site_url, idi), body=render(b, r, p, site_url, idi),
                            link=stay_link(r, site_url)))
        for key, (field, delta) in OFFSETS.items():
            if hour is not None and HORA.get(key, 0) > hour: continue
            if key == 'during' and noches <= 1: continue   # ya se ha ido ese mismo día
            base = ci if field == 'ci' else co
            when = base + dt.timedelta(days=delta)
            lag = (today - when).days
            if lag < 0 or lag > catchup: continue
            if lag > 0 and (co + dt.timedelta(days=3)) < today: continue
            s, b = pick_template(settings, key, idi)
            out.append(dict(key=key, res=r, prop=p, to=email, lang=idi,
                            subject=render(s, r, p, site_url, idi), body=render(b, r, p, site_url, idi),
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
                                     m.get('link', ''), m.get('prop'), m.get('lang'))
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
                link='', prop=None, lang='en'):
    texto = body
    if link and link not in texto:                 # el HTML lleva botón; el
        texto = texto.rstrip() + '\n\n' + link     # texto plano necesita la URL
    payload = {'from': from_email, 'to': [to], 'subject': subject,
               'html': html_wrap(body, site_url, link, prop, lang), 'text': texto}
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
    log('[1/5] Comprobando secretos…')
    for n, v in (('FIREBASE_SA_JSON', sa), ('RESEND_API_KEY', api), ('FROM_EMAIL', frm)):
        estado = 'OK' if v else 'FALTA'
        if not v: ok = False
        log('      %-18s %s' % (n, estado))
    if not (sa and api and frm):
        log('\nFaltan secretos. Créalos en Settings → Secrets and variables → Actions.')
        return 2

    log('[2/5] Validando el JSON de Firebase…')
    try:
        info = json.loads(sa)
        log('      proyecto: %s' % info.get('project_id'))
        log('      cuenta:   %s' % info.get('client_email'))
    except Exception as e:
        log('      JSON inválido: %s' % e)
        return 3

    log('[3/5] Conectando con Firestore…')
    fs = Firestore(sa)
    props = dict((i, d) for i, d in fs.docs('props'))
    res = fs.docs('reservations')
    log('      alojamientos leídos: %d' % len(props))
    log('      reservas leídas:     %d' % len(res))
    if not props and not res:
        log('      AVISO: no he leído nada. Revisa permisos del service account.')
        ok = False

    log('[4/5] Comprobando la clave de Resend…')
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

    log('[5/5] Revisando las plantillas guardadas…')
    try:
        ajustes = fs.get('private/data/meta/main').get('settings') or {}
        emails = ajustes.get('emails') or {}
        for k in sorted(emails.keys()):
            e = emails[k] or {}
            for campo, que in (('s', 'asunto'), ('b', 'cuerpo')):
                if not str(e.get(campo) or '').strip():
                    log('      AVISO: %s · %s vacío -> se usará el texto de fábrica' % (k, que))
                    ok = False
            for lg in ('es', 'fr', 'it'):
                if e.get('s_' + lg) is not None and not str(e.get('b_' + lg) or '').strip():
                    log('      AVISO: %s · cuerpo en %s vacío -> se usará el de fábrica' % (k, lg))
                    ok = False
        log('      %d plantilla(s) revisada(s)' % len(emails))
    except Exception as e:
        log('      no se han podido revisar: %s' % e)

    log('\nRESULTADO: %s' % ('todo correcto, ya puedes enviar' if ok else 'hay algo que arreglar'))
    return 0 if ok else 4

def procesar_outbox(fs, api, frm, site, reply, dry, test_to):
    """Envía lo que la web ha dejado en cola (bandeja de salida).

    La web deja los correos pendientes en dos sitios distintos:

      · la colección 'outbox': la usan los visitantes, que no pueden escribir
        en el documento privado del panel (las reglas de Firestore no lo
        permiten). Ahí acaba el código del 10 % del boletín.
      · private/data/meta/main -> outbox: la bandeja del propio panel, para lo
        que encolas tú a mano (botón de prueba, avisos, etc.).

    En los dos casos sale por Resend, con el mismo diseño y el mismo remitente
    que todos los demás correos.
    """
    enviados = fallidos = 0

    # ---------------------------------------------------------- 1. cola pública --
    try:
        cola = fs.docs('outbox')
    except Exception as e:
        cola = []
        log('  (aviso: no he podido leer la cola pública: %s)' % e)
    for did, m in cola:
        if not isinstance(m, dict): continue
        if str(m.get('status', 'queued')) == 'sent': continue
        if '@' not in str(m.get('to') or ''): continue
        clave = 'outbox_' + str(did)
        if fs.get('mail_log/' + clave) is not None:
            if not dry:
                try: fs.patch('outbox/' + str(did), {'status': 'sent'})
                except Exception: pass
            continue
        dest   = test_to or m.get('to')
        asunto = str(m.get('subject') or '')
        if test_to: asunto = '[PRUEBA] ' + asunto
        ok, info = send_resend(api, frm, dest, asunto, str(m.get('body') or ''),
                               site, reply, dry, '', None, m.get('lang'))
        if ok:
            enviados += 1
            if not dry and not test_to:
                fs.set('mail_log', clave, {
                    'resId': '', 'key': m.get('key', 'welcome'), 'to': dest,
                    'subject': asunto, 'date': TODAY().isoformat(),
                    'sentAt': dt.datetime.now(TZ).isoformat(),
                    'provider': 'resend', 'msgId': info, 'status': 'sent'})
                try: fs.patch('outbox/' + str(did), {'status': 'sent'})
                except Exception as e: log('  (aviso: no he podido marcarlo: %s)' % e)
            log('  \u2713 cola p\u00fablica %s \u2192 %s%s' % (m.get('key') or '?', dest,
                                              '  [PRUEBA]' if test_to else ''))
        else:
            fallidos += 1
            log('  \u2717 cola p\u00fablica %s \u2192 %s: %s' % (m.get('key') or '?', dest, info))
    if cola:
        log('Cola p\u00fablica: %d enviados \u00b7 %d fallidos' % (enviados, fallidos))

    # ------------------------------------------------- 2. bandeja del panel ------
    meta = fs.get('private/data/meta/main') or {}
    outbox = meta.get('outbox') or []
    if isinstance(outbox, list) and outbox:
        pend = [m for m in outbox if isinstance(m, dict)
                and str(m.get('status', 'queued')) != 'sent'
                and '@' in str(m.get('to') or '')]
        if pend:
            log('[cola] %d mensaje(s) pendiente(s) en la bandeja de salida' % len(pend))
        env2 = fall2 = 0
        cambiado = False
        for m in pend:
            clave = 'outbox_' + str(m.get('id') or ('%s_%s' % (m.get('to'), m.get('date'))))
            if fs.get('mail_log/' + clave) is not None:
                m['status'] = 'sent'; cambiado = True
                continue
            dest   = test_to or m['to']
            asunto = str(m.get('subject') or '')
            if test_to: asunto = '[PRUEBA] ' + asunto
            ok, info = send_resend(api, frm, dest, asunto, str(m.get('body') or ''),
                                   site, reply, dry, '', None, m.get('lang'))
            if ok:
                env2 += 1
                if not dry and not test_to:
                    fs.set('mail_log', clave, {
                        'resId': '', 'key': m.get('key', 'welcome'), 'to': dest,
                        'subject': asunto, 'date': TODAY().isoformat(),
                        'sentAt': dt.datetime.now(TZ).isoformat(),
                        'provider': 'resend', 'msgId': info, 'status': 'sent'})
                m['status'] = 'sent'
                m['date'] = TODAY().isoformat()
                cambiado = True
                log('  \u2713 cola %s \u2192 %s%s' % (m.get('key') or '?', dest,
                                                  '  [PRUEBA]' if test_to else ''))
            else:
                fall2 += 1
                log('  \u2717 cola %s \u2192 %s: %s' % (m.get('key') or '?', dest, info))
        if cambiado and not dry:
            fs.patch('private/data/meta/main', {'outbox': outbox})
        if env2 or fall2:
            log('Bandeja de salida: %d enviados \u00b7 %d fallidos' % (env2, fall2))
        enviados += env2; fallidos += fall2
    return enviados


# ------------------------------------------- candado contra los duplicados ---
# El programa se ejecuta cada 5 minutos. El candado dura 4: lo justo para que
# dos pasadas no se pisen, sin retrasar la confirmación de una reserva.
ESPERA_ENTRE_ENVIOS = 4           # minutos mínimos entre dos pasadas con correo
MAX_POR_PASADA      = 10          # tope de correos por pasada (red de seguridad)


def reclamar_turno(fs, minutos=ESPERA_ENTRE_ENVIOS):
    """Pide sitio para enviar. Devuelve False si ya hay otra pasada en ello.

    GitHub a veces lanza dos ejecuciones seguidas (o tú pulsas "Run workflow"
    mientras la programada sigue corriendo). Si las dos llegan a la vez a la
    lista de mensajes, el huésped recibe el correo dos veces.

    El sitio se pide ANTES de empezar a mandar, no después: si se pidiera al
    final, la segunda ejecución todavía no vería nada y mandaría lo mismo.
    """
    if os.environ.get('FORCE'):
        log('      (me han pedido enviar sin esperar: salto el candado)')
        return True
    mio = os.urandom(4).hex()
    try:
        ahora = dt.datetime.now(dt.timezone.utc)
        d = fs.get('mailrun/last') or {}
        t = d.get('at')
        if t:
            ult = dt.datetime.fromisoformat(str(t))
            if ult.tzinfo is None:
                ult = ult.replace(tzinfo=dt.timezone.utc)
            if (ahora - ult) < dt.timedelta(minutes=minutos):
                return False
        fs.set('mailrun', 'last', {'at': ahora.isoformat(), 'run': mio, 'sent': 0})
        time.sleep(3)                       # por si dos ejecuciones coinciden
        d2 = fs.get('mailrun/last') or {}
        return d2.get('run') == mio or not d2.get('run')
    except Exception as e:
        log('  (aviso: no he podido comprobar el turno, sigo igualmente: %s)' % e)
        return True


def anotar_ejecucion(fs, enviados, hora):
    """Deja constancia de cuánto ha enviado esta pasada."""
    try:
        d = fs.get('mailrun/last') or {}
        d.pop('run', None)
        d['sent'] = enviados
        d['hour'] = hora
        fs.set('mailrun', 'last', d)
    except Exception as e:
        log('  (aviso: no he podido anotar el turno: %s)' % e)

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

    # --- Horario de cortesía --------------------------------------------------
    # El programa se ejecuta cada hora, también de noche, para que los
    # calendarios estén al día. Pero de noche (22:00-07:59 hora de Suiza)
    # SÓLO se envían mensajes que el huésped espera en ese instante:
    # la confirmación de su reserva y el código de descuento.
    # El resto se aplaza a la mañana siguiente (nada se pierde: la ventana
    # de recuperación es de 2 días).
    if not (8 <= ahora.hour <= 21):
        nocturnos = [m for m in pend if m.get('key') == 'confirm']
        if len(nocturnos) != len(pend):
            log('      (%d mensaje(s) aplazado(s) a la manana: es de noche)'
                % (len(pend) - len(nocturnos)))
        pend = nocturnos
    # Candado: si otra pasada acaba de enviar, ésta se aparta. Así nadie
    # recibe el mismo correo dos veces cuando GitHub lanza dos ejecuciones.
    sin_anotar = 0
    if not dry and not test_to and not reclamar_turno(fs):
        log('[5/5] Otra ejecución ha enviado correos hace menos de %d minutos:' % ESPERA_ENTRE_ENVIOS)
        log('      esta pasada no manda nada (los calendarios sí se sincronizan).')
        log('      Vuelve a darle a Run workflow dentro de %d minutos, o márcala' % ESPERA_ENTRE_ENVIOS)
        log('      con la casilla "Send now" para saltarte la espera.')
        return 0

    log('[5/5] Mensajes que tocan hoy: %d%s' % (len(pend), '  (DRY RUN)' if dry else ''))

    sent = skipped = failed = vacios = 0
    for m in pend:
        rid = m['res'].get('id', '')
        if not dry and not test_to and fs.get('mail_log/%s_%s' % (rid, m['key'])) is not None:
            skipped += 1
            log('  · ya enviado: %s %s' % (rid, m['key']))
            continue
        dest   = test_to or m['to']
        asunto = ('[PRUEBA] ' + m['subject']) if test_to else m['subject']
        # Nunca se manda un correo en blanco: si la plantilla se ha quedado sin
        # texto se avisa y se deja para la siguiente pasada. Así se evitan los
        # correos vacíos que llegaban una y otra vez.
        if not str(m['subject'] or '').strip() or not str(m['body'] or '').strip():
            vacios += 1
            log('  ! %s %s -> la plantilla esta vacia: NO envio nada' % (rid, m['key']))
            log('    (Ajustes -> Email templates: reescribela o dejala en blanco')
            log('     para que se use el texto de fabrica)')
            continue
        if sent >= MAX_POR_PASADA:
            log('  · %s %s -> para la siguiente pasada (tope de %d por pasada)'
                % (rid, m['key'], MAX_POR_PASADA))
            continue
        ok, info = send_resend(api, frm, dest, asunto, m['body'], site, reply, dry,
                                     m.get('link', ''), m.get('prop'), m.get('lang'))
        if ok:
            sent += 1
            if not dry and not test_to:
                clave = '%s_%s' % (rid, m['key'])
                datos = {'resId': rid, 'key': m['key'], 'to': dest, 'subject': asunto,
                         'date': TODAY().isoformat(), 'sentAt': dt.datetime.now(TZ).isoformat(),
                         'provider': 'resend', 'msgId': info, 'status': 'sent'}
                r = fs.set('mail_log', clave, datos)
                if (not r) or ('__error__' in r):
                    r = fs.set('mail_log', clave, {'resId': rid, 'key': m['key'],
                                                   'to': dest, 'status': 'sent'})
                if (not r) or ('__error__' in r):
                    sin_anotar += 1
                    log('  !! ENVIADO pero NO anotado en el registro: %s' % clave)
                    log('     %s' % ((r or {}).get('__detail__') or (r or {}).get('__error__')))
                    log('     Sin esa anotacion el correo se repetiria.')
            log('  ✓ %s %s -> %s%s' % (rid, m['key'], dest, '  [PRUEBA]' if test_to else ''))
        else:
            failed += 1
            log('  ✗ %s %s -> %s: %s' % (rid, m['key'], dest, info))

    cola = procesar_outbox(fs, api, frm, site, reply, dry, test_to)
    if not dry and not test_to:
        anotar_ejecucion(fs, sent + cola, ahora.strftime('%H:%M'))

    log('Resumen: %d enviados · %d ya enviados · %d fallidos · %d de la bandeja' %
        (sent, skipped, failed, cola))
    if vacios:
        log('AVISO: %d mensaje(s) NO enviados porque su plantilla esta vacia.' % vacios)
    if sin_anotar:
        log('AVISO: %d mensaje(s) enviados que no se han podido anotar.' % sin_anotar)
    return 0

if __name__ == '__main__':
    sys.exit(main())
