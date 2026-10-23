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
FIRMA_MARCA  = True            # la firma del correo es el nombre de la marca
WATCHDOG = 240          # segundos máximo de ejecución
UA = 'architects-house-mailer/1.0'   # Cloudflare bloquea peticiones sin User-Agent

def log(msg):
    print(msg, flush=True)


def como_dic(x):
    """Un dict siempre: un fallo transitorio de lectura no debe romper nada."""
    return x if isinstance(x, dict) else {}

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
        """None = no existe (404). '__unknown__' = error transitorio (mejor no fiarse)."""
        res = self._call('/' + urllib.parse.quote(path, safe='/'), timeout=timeout)
        if '__error__' in res:
            return None if res['__error__'] == 404 else '__unknown__'
        return self.decode_fields(res.get('fields', {}))

    def create(self, collection, doc_id, fields, timeout=25):
        """Crea el documento SOLO si no existe. Si ya existe llega un error 409."""
        body = {'fields': {k: self.encode_value(v) for k, v in fields.items()}}
        path = '/' + collection + '?documentId=' + urllib.parse.quote(doc_id, safe='')
        return self._call(path, method='POST', body=body, timeout=timeout)

    def set(self, collection, doc_id, fields, timeout=25):
        """Escribe el documento: lo crea si no existe y lo actualiza si ya existe."""
        res = self.create(collection, doc_id, fields, timeout)
        if '__error__' in res and res['__error__'] == 409:
            return self.patch(collection + '/' + doc_id, fields, timeout)
        return res

    def delete(self, path, timeout=25):
        return self._call('/' + urllib.parse.quote(path, safe='/'), method='DELETE',
                          timeout=timeout)

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

MESES.update({"de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"], "pt": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"], "nl": ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"], "pl": ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września", "października", "listopada", "grudnia"], "cs": ["ledna", "února", "března", "dubna", "května", "června", "července", "srpna", "září", "října", "listopadu", "prosince"], "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"], "uk": ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"], "tr": ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"], "ar": ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"], "zh": ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"], "ja": ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"], "ko": ["1월", "2월", "3월", "4월", "5월", "6월", "7월", "8월", "9월", "10월", "11월", "12월"], "sv": ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober", "november", "december"], "da": ["januar", "februar", "marts", "april", "maj", "juni", "juli", "august", "september", "oktober", "november", "december"], "no": ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "desember"], "fi": ["tammikuuta", "helmikuuta", "maaliskuuta", "huhtikuuta", "toukokuuta", "kesäkuuta", "heinäkuuta", "elokuuta", "syyskuuta", "lokakuuta", "marraskuuta", "joulukuuta"]})
DIAS.update({"de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"], "pt": ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"], "nl": ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"], "pl": ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"], "cs": ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"], "ru": ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"], "uk": ["понеділок", "вівторок", "середа", "четвер", "'ятниця", "субота", "неділя"], "tr": ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"], "ar": ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"], "zh": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"], "ja": ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"], "ko": ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"], "sv": ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"], "da": ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"], "no": ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"], "fi": ["maanantai", "tiistai", "keskiviikko", "torstai", "perjantai", "lauantai", "sunnuntai"]})
FMT_FECHA = {"de": "{dia}, {d}. {mes} {y}", "pt": "{dia}, {d} de {mes} de {y}", "nl": "{dia} {d} {mes} {y}", "pl": "{dia}, {d} {mes} {y}", "cs": "{dia} {d}. {mes} {y}", "ru": "{dia}, {d} {mes} {y}", "uk": "{dia}, {d} {mes} {y}", "tr": "{dia}, {d} {mes} {y}", "ar": "{dia}، {d} {mes} {y}", "zh": "{y}年{mes}{d}日（{dia}）", "ja": "{y}年{mes}{d}日（{dia}）", "ko": "{y}년 {mes} {d}일 ({dia})", "sv": "{dia} {d} {mes} {y}", "da": "{dia} {d}. {mes} {y}", "no": "{dia} {d}. {mes} {y}", "fi": "{dia} {d}. {mes} {y}"}

FMT_FECHA_OLD = {'en': '{dia}, {mes} {d}, {y}', 'es': '{dia}, {d} de {mes} de {y}',
                 'fr': '{dia} {d} {mes} {y}', 'it': '{dia} {d} {mes} {y}'}

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
    fmt = FMT_FECHA.get(L) or FMT_FECHA_OLD.get(L) or FMT_FECHA_OLD['en']
    return fmt.format(dia=DIAS[L][d.weekday()], d=d.day, mes=MESES[L][d.month - 1], y=d.year)

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


DESPEDIDAS = ('un saludo', 'un cordial saludo', 'atentamente', 'muchas gracias',
              'best regards', 'kind regards', 'warm regards', 'best wishes',
              'regards', 'sincerely', 'bien à vous', 'bien cordialement',
              'cordialement', 'à bientôt', 'un cordiale saluto',
              'cordiali saluti', 'un caro saluto', 'mit freundlichen grüßen', 'mit freundlichen grüssen', 'freundliche grüße',
              'com os melhores cumprimentos', 'met vriendelijke groet', 'met vriendelijke groeten',
              'z poważaniem', 'serdecznie pozdrawiam', 's pozdravem', 'srdečně zdravím',
              'с уважением', 'з повагою', 'saygılarımla', 'مع خالص التحيات',
              '此致敬礼', '敬上', 'よろしくお願いいたします', '감사합니다',
              'vänliga hälsningar', 'med vänlig hälsning', 'venlig hilsen', 'med venlig hilsen',
              'vennlig hilsen', 'med vennlig hilsen', 'ystävällisin terveisin', 'terveisin')


def quitar_firma(texto):
    """Quita del final las líneas de despedida y de nombre.

    El cuerpo de la plantilla acaba con algo tipo "Un saludo,\nLucas,
    Architect'sHouse". Como ahora la firma se dibuja con la foto, esas
    líneas se retiran del correo en HTML para que el nombre no salga
    dos veces. Si el cuerpo no acaba así, se deja tal cual.
    """
    lineas = [l.rstrip() for l in str(texto or '').split('\n')]
    while lineas and not lineas[-1].strip():
        lineas.pop()
    if not lineas:
        return ''
    if 'architect' in lineas[-1].lower():
        lineas.pop()
        while lineas and not lineas[-1].strip():
            lineas.pop()
        if lineas:
            pen = lineas[-1].strip().lower().rstrip(',;.').strip()
            if pen in DESPEDIDAS or (len(pen) <= 30 and lineas[-1].strip().endswith(',')):
                lineas.pop()
    while lineas and not lineas[-1].strip():
        lineas.pop()
    return '\n'.join(lineas)


def html_wrap(text, site_url, link='', prop=None, lang='en'):
    """Correo sencillo y limpio: nombre en texto, cuerpo, botón y pie.

    Sin imágenes y sin franjas de color: así se ve igual en Gmail, en el móvil
    y en modo oscuro, y nunca aparece un hueco de imagen bloqueada.
    """
    t = TEXTS.get(str(lang or 'en').split('-')[0].lower(), TEXTS['en'])
    limpio = quitar_firma(text)
    if not limpio.strip():
        limpio = (text or '')
    body = (limpio or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    body = body.replace('\n', '<br>')
    host = (site_url or '').replace('https://', '').replace('http://', '').rstrip('/')
    marca = ('<div style="font-family:Georgia,\'Times New Roman\',Times,serif;font-size:21px;'
             'letter-spacing:-.01em;line-height:1.25;color:#1b1b19">Architect’s'
             '<i style="font-style:italic">House</i></div>')
    boton = ('<a href="%s" style="display:inline-block;background:#1b1b19;color:#ffffff;'
             'text-decoration:none;padding:12px 22px;border-radius:8px;font-size:14px">'
             '%s</a>' % (link, t['boton'])) if link else ''
    fila_boton = ('<tr><td class="ah-pad" style="padding:2px 30px 28px">%s</td></tr>' % boton) if boton else ''
    # la firma: el nombre, igual que en la cabecera de la web (sin foto)
    firma = ('<tr><td class="ah-pad" style="padding:2px 30px 26px;'
             "font-family:Georgia,'Times New Roman',Times,serif;font-size:20px;"
             'letter-spacing:-.01em;line-height:1.25;color:#1A1916">'
             'Architect\u2019s<i style="font-style:italic;color:#80603B">House</i>'
             '</td></tr>')
    idi = str(lang or 'en').split('-')[0].lower()
    return ('<!doctype html><html lang="%s"%s><head><meta charset="utf-8">'
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
            'text-align:%s">'
            '<tr><td class="ah-pad" style="padding:26px 30px 16px">%s</td></tr>'
            '<tr><td><div style="border-top:1px solid #efece4"></div></td></tr>'
            '<tr><td class="ah-pad" style="padding:24px 30px 16px;font-size:15px;line-height:1.75">%s</td></tr>'
            '%s'
            '%s'
            '<tr><td class="ah-pad" style="padding:18px 30px 28px;border-top:1px solid #efece4;'
            'font-size:11px;line-height:1.6;color:#9a9a94">'
            '<b style="color:#6b6b64">%s</b> · %s<br>'
            '%s'
            '</td></tr></table></td></tr></table></body></html>'
            % (idi, (' dir="rtl"' if idi == 'ar' else ''), ('right' if idi == 'ar' else 'left'), marca, body, fila_boton, firma, BRAND, host, t['pie']))


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
                "Dear {{guest}},\n\nThank you for staying at {{property}}. We hope you had a good stay.\n\nIf you need anything, just reply to this email."),
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
                "Hola {{guest}},\n\nGracias por alojarte en {{property}}. Esperamos que hayas tenido una buena estancia.\n\nSi necesitas cualquier cosa, responde a este mismo correo."),
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
                "Bonjour {{guest}},\n\nMerci d’avoir séjourné à {{property}}. Nous espérons que votre séjour s’est bien passé.\n\nSi vous avez besoin de quoi que ce soit, répondez simplement à cet e-mail."),
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
                "Ciao {{guest}},\n\nGrazie per aver soggiornato a {{property}}. Speriamo che il soggiorno sia stato piacevole.\n\nSe ti serve qualsiasi cosa, rispondi pure a questa e-mail."),
        'review': ("Ci piacerebbe conoscere la tua esperienza",
                "Ciao {{guest}},\n\nHai un minuto per raccontarci com’è andato il tuo soggiorno a {{property}}? La tua recensione aiuta altri viaggiatori e aiuta noi a migliorare."),
    },
}


DEFAULT.update({"de": {"welcome": ["Ihr 10-%-Code für Ihren ersten Aufenthalt", "Hallo,\n\nvielen Dank für Ihre Anmeldung bei Architect’sHouse. Hier ist Ihr persönlicher Code für 10 % Rabatt auf Ihre erste Buchung:\n\n    {{code}}\n\nGeben Sie ihn beim Bezahlen ein. Es ist ein einmaliger Code, gültig auf Ihre erste Buchung, und das Formular fügt ihn automatisch für Sie ein.\n\nMit freundlichen Grüßen,\nLucas, Architect’sHouse"], "confirm": ["Ihr Aufenthalt in {{property}} ist bestätigt", "Guten Tag {{guest}},\n\nvielen Dank, dass Sie direkt bei Architect’sHouse gebucht haben. Ihr Aufenthalt in {{property}} vom {{checkin}} bis {{checkout}} ist bestätigt (Buchung {{booking}}).\n\nBezahlt gesamt: {{total}}\n\nAlles, was Sie brauchen, finden Sie auf Ihrer Aufenthaltsseite.\n\nMit freundlichen Grüßen,\nLucas, Architect’sHouse"], "before": ["Ihr Aufenthalt steht bevor", "Guten Tag {{guest}},\n\nwir freuen uns darauf, Sie am {{checkin}} in {{property}} willkommen zu heißen. Ihre Anreisedetails, der Parkplatz und der Zugangscode finden sich auf Ihrer Aufenthaltsseite."], "checkin": ["Alles für Ihre Anreise", "Guten Tag {{guest}},\n\nheute ist es soweit. Der Check-in ist ab {{checkintime}}. Der Code der Schlüsselbox und die WLAN-Daten finden sich auf Ihrer Aufenthaltsseite."], "during": ["Läuft alles gut?", "Guten Tag {{guest}},\n\nwir hoffen, Sie fühlen sich in {{property}} wohl. Falls etwas fehlt, antworten Sie einfach auf diese Nachricht."], "departure": ["Ihre Abreise-Anleitung", "Guten Tag {{guest}},\n\nder Check-out ist morgen bis {{checkouttime}}. Eine kurze Checkliste finden Sie auf Ihrer Aufenthaltsseite."], "after": ["Danke für Ihren Aufenthalt", "Guten Tag {{guest}},\n\nvielen Dank, dass Sie in {{property}} übernachtet haben. Wir hoffen, Sie hatten einen schönen Aufenthalt.\n\nFalls Sie etwas brauchen, antworten Sie einfach auf diese E-Mail."], "review": ["Wir würden gern von Ihren Erfahrungen hören", "Guten Tag {{guest}},\n\nhätten Sie eine Minute, um uns von Ihrem Aufenthalt in {{property}} zu erzählen? Ihre Bewertung hilft anderen Reisenden und hilft uns, uns zu verbessern."]}, "pt": {"welcome": ["O seu código de 10% para a primeira estadia", "Olá,\n\nObrigado por subscrever a Architect’sHouse. Aqui está o seu código pessoal de 10% de desconto na primeira reserva:\n\n    {{code}}\n\nInsira-o no pagamento. É um código de uso único, válido na primeira reserva, e o formulário preenche-o automaticamente por si.\n\nCom os melhores cumprimentos,\nLucas, Architect’sHouse"], "confirm": ["A sua estadia em {{property}} está confirmada", "Olá {{guest}},\n\nObrigado por reservar diretamente com a Architect’sHouse. A sua estadia em {{property}}, de {{checkin}} a {{checkout}}, está confirmada (reserva {{booking}}).\n\nTotal pago: {{total}}\n\nTudo o que precisa está na página da sua estadia.\n\nCom os melhores cumprimentos,\nLucas, Architect’sHouse"], "before": ["A sua estadia está a chegar", "Olá {{guest}},\n\nEstamos com vontade de o receber em {{property}} no dia {{checkin}}. Os detalhes da chegada, o estacionamento e o código de acesso estão na página da sua estadia."], "checkin": ["Tudo o que precisa para a sua chegada", "Olá {{guest}},\n\nHoje é o dia. O check-in é a partir das {{checkintime}}. O código da caixa de chaves e o Wi-Fi estão na página da sua estadia."], "during": ["Está tudo bem?", "Olá {{guest}},\n\nEsperemos que esteja a instalar-se bem em {{property}}. Se falta alguma coisa, responda a esta mensagem."], "departure": ["As suas instruções de saída", "Olá {{guest}},\n\nO check-out é até às {{checkouttime}} de amanhã. Tem uma lista breve na página da sua estadia."], "after": ["Obrigado por ficar connosco", "Olá {{guest}},\n\nObrigado por ter ficado em {{property}}. Esperemos que tenha tido uma boa estadia.\n\nSe precisar de qualquer coisa, responda a este email."], "review": ["Gostaríamos de conhecer a sua experiência", "Olá {{guest}},\n\nTem um minuto para nos contar como foi a sua estadia em {{property}}? A sua opinião ajuda outros viajantes e ajuda-nos a melhorar."]}, "nl": {"welcome": ["Uw 10%-code voor uw eerste verblijf", "Hallo,\n\nDank u voor uw inschrijving bij Architect’sHouse. Hier is uw persoonlijke code voor 10% korting op uw eerste boeking:\n\n    {{code}}\n\nVoer hem in bij het afrekenen. Het is een eenmalige code, geldig op uw eerste boeking, en het formulier vult hem automatisch voor u in.\n\nMet vriendelijke groet,\nLucas, Architect’sHouse"], "confirm": ["Uw verblijf in {{property}} is bevestigd", "Beste {{guest}},\n\nDank u voor het direct boeken bij Architect’sHouse. Uw verblijf in {{property}} van {{checkin}} tot {{checkout}} is bevestigd (boeking {{booking}}).\n\nTotaal betaald: {{total}}\n\nAlles wat u nodig heeft staat op de pagina van uw verblijf.\n\nMet vriendelijke groet,\nLucas, Architect’sHouse"], "before": ["Uw verblijf komt eraan", "Beste {{guest}},\n\nWe kijken ernaar uit u te verwelkomen in {{property}} op {{checkin}}. Uw aankomstdetails, parkeren en toegangscode staan op de pagina van uw verblijf."], "checkin": ["Alles voor uw aankomst", "Beste {{guest}},\n\nVandaag is het zover. Check-in is vanaf {{checkintime}}. De code van de sleutelkluis en de Wi-Fi staan op de pagina van uw verblijf."], "during": ["Gaat alles goed?", "Beste {{guest}},\n\nWe hopen dat u zich goed voelt in {{property}}. Als er iets mist, antwoord dan gewoon op dit bericht."], "departure": ["Uw uitcheckinstructies", "Beste {{guest}},\n\nUitchecken is morgen tot {{checkouttime}}. Een korte checklist staat op de pagina van uw verblijf."], "after": ["Dank u voor uw verblijf", "Beste {{guest}},\n\nDank u voor uw verblijf in {{property}}. We hopen dat u een fijn verblijf heeft gehad.\n\nAls u iets nodig heeft, antwoord dan gewoon op deze e-mail."], "review": ["We horen graag over uw ervaring", "Beste {{guest}},\n\nHeeft u een minuut om ons te vertellen over uw verblijf in {{property}}? Uw beoordeling helpt andere reizigers en helpt ons te verbeteren."]}, "pl": {"welcome": ["Twój kod 10% na pierwszy pobyt", "Dzień dobry,\n\nDziękujemy za zapis do Architect’sHouse. Oto Twój osobisty kod na 10% zniżki na pierwszą rezerwację:\n\n    {{code}}\n\nWpisz go przy płatności. To jednorazowy kod, ważny przy pierwszej rezerwacji, a formularz wypełni go automatycznie.\n\nZ poważaniem,\nLucas, Architect’sHouse"], "confirm": ["Twój pobyt w {{property}} jest potwierdzony", "Dzień dobry {{guest}},\n\nDziękujemy za rezerwację bezpośrednio w Architect’sHouse. Twój pobyt w {{property}} od {{checkin}} do {{checkout}} jest potwierdzony (rezerwacja {{booking}}).\n\nZapłacono łącznie: {{total}}\n\nWszystko, czego potrzebujesz, znajdziesz na stronie swojego pobytu.\n\nZ poważaniem,\nLucas, Architect’sHouse"], "before": ["Twój pobyt zbliża się wielkimi krokami", "Dzień dobry {{guest}},\n\nZ niecierpliwością czekamy, by powitać Cię w {{property}} {{checkin}}. Szczegóły przyjazdu, parking i kod dostępu znajdziesz na stronie swojego pobytu."], "checkin": ["Wszystko, czego potrzebujesz na przyjazd", "Dzień dobry {{guest}},\n\nDzisiaj ten dzień. Zameldowanie od {{checkintime}}. Kod skrzynki na klucze i Wi-Fi znajdziesz na stronie swojego pobytu."], "during": ["Czy wszystko w porządku?", "Dzień dobry {{guest}},\n\nMamy nadzieję, że dobrze czujesz się w {{property}}. Jeśli czegoś brakuje, po prostu odpowiedz na tę wiadomość."], "departure": ["Instrukcje wyjazdu", "Dzień dobry {{guest}},\n\nWymeldowanie jutro do {{checkouttime}}. Krótką listę znajdziesz na stronie swojego pobytu."], "after": ["Dziękujemy za pobyt", "Dzień dobry {{guest}},\n\nDziękujemy za pobyt w {{property}}. Mamy nadzieję, że pobyt był udany.\n\nJeśli czegoś potrzebujesz, po prostu odpowiedz na ten e-mail."], "review": ["Chętnie poznamy Twoje wrażenia", "Dzień dobry {{guest}},\n\nCzy masz minutę, by opowiedzieć nam o swoim pobycie w {{property}}? Twoja opinia pomaga innym podróżnym i pomaga nam się rozwijać."]}, "cs": {"welcome": ["Váš 10% kód na první pobyt", "Dobrý den,\n\nděkujeme za přihlášení k odběru od Architect’sHouse. Zde je váš osobní kód na 10% slevu z první rezervace:\n\n    {{code}}\n\nZadejte jej při placení. Je to jednorázový kód, platný na první rezervaci, a formulář jej za vás automaticky vyplní.\n\nS pozdravem,\nLucas, Architect’sHouse"], "confirm": ["Váš pobyt v {{property}} je potvrzen", "Dobrý den, {{guest}},\n\nděkujeme za rezervaci přímo u Architect’sHouse. Váš pobyt v {{property}} od {{checkin}} do {{checkout}} je potvrzen (rezervace {{booking}}).\n\nCelkem zaplaceno: {{total}}\n\nVše, co potřebujete, najdete na stránce svého pobytu.\n\nS pozdravem,\nLucas, Architect’sHouse"], "before": ["Váš pobyt se blíží", "Dobrý den, {{guest}},\n\ntěšíme se, že vás přivítáme v {{property}} {{checkin}}. Podrobnosti o příjezdu, parkování a přístupový kód najdete na stránce svého pobytu."], "checkin": ["Vše pro váš příjezd", "Dobrý den, {{guest}},\n\ndnes je ten den. Check-in od {{checkintime}}. Kód klíčové schránky a Wi-Fi najdete na stránce svého pobytu."], "during": ["Jde všechno v pořádku?", "Dobrý den, {{guest}},\n\ndoufáme, že se vám v {{property}} líbí. Pokud vám něco chybí, stačí odpovědět na tuto zprávu."], "departure": ["Pokyny k odjezdu", "Dobrý den, {{guest}},\n\ncheck-out je zítra do {{checkouttime}}. Krátký seznam najdete na stránce svého pobytu."], "after": ["Děkujeme za pobyt", "Dobrý den, {{guest}},\n\nděkujeme, že jste pobývali v {{property}}. Doufáme, že pobyt byl příjemný.\n\nPokud něco potřebujete, stačí odpovědět na tento e-mail."], "review": ["Rádi bychom slyšeli o vaší zkušenosti", "Dobrý den, {{guest}},\n\nmáte minutku, abyste nám řekli o svém pobytu v {{property}}? Váš názor pomáhá dalším cestovatelům a pomáhá nám se zlepšovat."]}, "ru": {"welcome": ["Ваш промокод 10% на первое проживание", "Здравствуйте!\n\nСпасибо за подписку на Architect’sHouse. Вот ваш персональный промокод на 10% скидку при первом бронировании:\n\n    {{code}}\n\nВведите его при оплате. Это одноразовый код, действующий на первое бронирование, и форма заполнит его автоматически.\n\nС уважением,\nLucas, Architect’sHouse"], "confirm": ["Ваше проживание в {{property}} подтверждено", "Здравствуйте, {{guest}}!\n\nСпасибо, что забронировали напрямую у Architect’sHouse. Ваше проживание в {{property}} с {{checkin}} по {{checkout}} подтверждено (бронирование {{booking}}).\n\nОплачено всего: {{total}}\n\nВсё необходимое — на странице вашего проживания.\n\nС уважением,\nLucas, Architect’sHouse"], "before": ["Ваше проживание уже скоро", "Здравствуйте, {{guest}}!\n\nМы с нетерпением ждём вас в {{property}} {{checkin}}. Детали заезда, парковка и код доступа — на странице вашего проживания."], "checkin": ["Всё необходимое для заезда", "Здравствуйте, {{guest}}!\n\nСегодня этот день. Заезд с {{checkintime}}. Код от ключевого ящика и Wi-Fi — на странице вашего проживания."], "during": ["Всё ли в порядке?", "Здравствуйте, {{guest}}!\n\nНадеемся, вам комфортно в {{property}}. Если чего-то не хватает, просто ответьте на это сообщение."], "departure": ["Инструкции по выезду", "Здравствуйте, {{guest}}!\n\nВыезд завтра до {{checkouttime}}. Короткий чек-лист — на странице вашего проживания."], "after": ["Спасибо, что остановились у нас", "Здравствуйте, {{guest}}!\n\nСпасибо, что остановились в {{property}}. Надеемся, проживание прошло хорошо.\n\nЕсли вам что-то нужно, просто ответьте на это письмо."], "review": ["Нам важно ваше мнение", "Здравствуйте, {{guest}}!\n\nНе могли бы вы уделить минуту и рассказать о проживании в {{property}}? Ваш отзыв помогает другим путешественникам и помогает нам становиться лучше."]}, "uk": {"welcome": ["Ваш промокод 10% на перше проживання", "Вітаємо!\n\nДякуємо за підписку на Architect’sHouse. Ось ваш персональний промокод на 10% знижки при першому бронюванні:\n\n    {{code}}\n\nВведіть його при оплаті. Це одноразовий код, що діє на перше бронювання, і форма заповнить його автоматично.\n\nЗ повагою,\nLucas, Architect’sHouse"], "confirm": ["Ваше проживання в {{property}} підтверджено", "Вітаємо, {{guest}}!\n\nДякуємо, що забронювали напряму в Architect’sHouse. Ваше проживання в {{property}} з {{checkin}} по {{checkout}} підтверджено (бронювання {{booking}}).\n\nСплачено разом: {{total}}\n\nУсе необхідне — на сторінці вашого проживання.\n\nЗ повагою,\nLucas, Architect’sHouse"], "before": ["Ваше проживання вже скоро", "Вітаємо, {{guest}}!\n\nМи з нетерпінням чекаємо на вас у {{property}} {{checkin}}. Деталі заїзду, паркування та код доступу — на сторінці вашого проживання."], "checkin": ["Усе необхідне для заїзду", "Вітаємо, {{guest}}!\n\nСьогодні цей день. Заїзд з {{checkintime}}. Код від скриньки для ключів і Wi-Fi — на сторінці вашого проживання."], "during": ["Усе гаразд?", "Вітаємо, {{guest}}!\n\nСподіваємося, вам комфортно в {{property}}. Якщо чогось бракує, просто відповідьте на це повідомлення."], "departure": ["Інструкції щодо виїзду", "Вітаємо, {{guest}}!\n\nВиїзд завтра до {{checkouttime}}. Короткий чекліст — на сторінці вашого проживання."], "after": ["Дякуємо, що зупинилися в нас", "Вітаємо, {{guest}}!\n\nДякуємо, що зупинилися в {{property}}. Сподіваємося, проживання пройшло добре.\n\nЯкщо вам щось потрібно, просто відповідьте на цей лист."], "review": ["Нам важлива ваша думка", "Вітаємо, {{guest}}!\n\nЧи могли б ви приділити хвилину й розповісти про проживання в {{property}}? Ваш відгук допомагає іншим мандрівникам і допомагає нам ставати кращими."]}, "tr": {"welcome": ["İlk konaklamanız için %10 kodunuz", "Merhaba,\n\nArchitect’sHouse'a abone olduğunuz için teşekkürler. İlk rezervasyonunuzda %10 indirim sağlayan kişisel kodunuz:\n\n    {{code}}\n\nÖdeme sırasında girin. Tek kullanımlık bir koddur, ilk rezervasyonunuzda geçerlidir ve form sizin için otomatik doldurur.\n\nSaygılarımla,\nLucas, Architect’sHouse"], "confirm": ["{{property}} konaklamanız onaylandı", "Merhaba {{guest}},\n\nDoğrudan Architect'sHouse'tan rezervasyon yaptığınız için teşekkürler. {{checkin}} - {{checkout}} tarihleri arasındaki {{property}} konaklamanız onaylandı (rezervasyon {{booking}}).\n\nToplam ödenen: {{total}}\n\nİhtiyacınız olan her şey konaklama sayfanızda.\n\nSaygılarımla,\nLucas, Architect’sHouse"], "before": ["Konaklamanız yaklaşıyor", "Merhaba {{guest}},\n\nSizi {{checkin}} tarihinde {{property}}'de ağırlamayı sabırsızlıkla bekliyoruz. Varış detayları, otopark ve giriş kodu konaklama sayfanızda."], "checkin": ["Varışınız için ihtiyacınız olan her şey", "Merhaba {{guest}},\n\nBugün o gün. Giriş {{checkintime}} itibarıyladır. Anahtar kutusu kodu ve Wi-Fi bilgileri konaklama sayfanızda."], "during": ["Her şey yolunda mı?", "Merhaba {{guest}},\n\n{{property}}'de güzel vakit geçirmenizi umuyoruz. Bir şey eksikse bu mesajı yanıtlamanız yeterli."], "departure": ["Çıkış talimatlarınız", "Merhaba {{guest}},\n\nÇıkış yarın {{checkouttime}}'a kadardır. Kısa bir liste konaklama sayfanızda."], "after": ["Bizimle kaldığınız için teşekkürler", "Merhaba {{guest}},\n\n{{property}}'de kaldığınız için teşekkürler. Güzel bir konaklama geçirmenizi umuyoruz.\n\nBir şeye ihtiyacınız olursa bu e-postayı yanıtlamanız yeterli."], "review": ["Deneyiminizi duymak isteriz", "Merhaba {{guest}},\n\n{{property}}'deki konaklamanızı bize anlatmak için bir dakikanız var mı? Yorumunuz diğer gezginlere yardımcı olur ve bizi geliştirir."]}, "ar": {"welcome": ["رمز الخصم 10% لإقامتك الأولى", "مرحباً،\n\nشكراً لاشتراكك في Architect’sHouse. إليك رمزك الشخصي لخصم 10% على حجزك الأول:\n\n    {{code}}\n\nأدخله عند الدفع. إنه رمز لمرة واحدة، صالح على حجزك الأول، وسينشئه النموذج تلقائياً لك.\n\nمع خالص التحيات،\nLucas, Architect’sHouse"], "confirm": ["تم تأكيد إقامتك في {{property}}", "مرحباً {{guest}}،\n\nشكراً لحجزك مباشرة مع Architect’sHouse. لقد تم تأكيد إقامتك في {{property}} من {{checkin}} إلى {{checkout}} (الحجز {{booking}}).\n\nالإجمالي المدفوع: {{total}}\n\nكل ما تحتاجه موجود في صفحة إقامتك.\n\nمع خالص التحيات،\nLucas, Architect’sHouse"], "before": ["إقامتك تقترب", "مرحباً {{guest}}،\n\nنتطلع إلى استقبالك في {{property}} يوم {{checkin}}. تفاصيل الوصول ومواقف السيارات ورمز الدخول موجودة في صفحة إقامتك."], "checkin": ["كل ما تحتاجه لوصولك", "مرحباً {{guest}}،\n\nاليوم هو اليوم. تسجيل الوصول من {{checkintime}}. رمز صندوق المفاتيح وبيانات الواي فاي موجودة في صفحة إقامتك."], "during": ["هل كل شيء يسير على ما يرام؟", "مرحباً {{guest}}،\n\nنأمل أن تستقر بخير في {{property}}. إن كان هناك ناقص، فقط أجب على هذه الرسالة."], "departure": ["تعليمات المغادرة", "مرحباً {{guest}}،\n\nتسجيل المغادرة غداً حتى {{checkouttime}}. ستجد قائمة قصيرة في صفحة إقامتك."], "after": ["شكراً لإقامتك معنا", "مرحباً {{guest}}،\n\nشكراً لإقامتك في {{property}}. نأمل أن تكون قد قضيت وقتاً جيداً.\n\nإذا احتجت أي شيء، فقط أجب على هذا البريد."], "review": ["نحب أن نسمع عن تجربتك", "مرحباً {{guest}}،\n\nهل لديك دقيقة لتخبرنا عن إقامتك في {{property}}؟ مراجعتك تساعد المسافرين الآخرين وتساعدنا على التحسين."]}, "zh": {"welcome": ["首住 10% 优惠码", "您好：\n\n感谢订阅 Architect’sHouse。这是您首次预订立减 10% 的专属优惠码：\n\n    {{code}}\n\n结账时输入即可。此码限用一次，适用于首次预订，表单会自动为您填入。\n\n此致敬意，\nLucas, Architect’sHouse"], "confirm": ["您在 {{property}} 的住宿已确认", "您好 {{guest}}：\n\n感谢您直接通过 Architect’sHouse 预订。您在 {{property}} 从 {{checkin}} 至 {{checkout}} 的住宿已确认（预订号 {{booking}}）。\n\n已付总额：{{total}}\n\n所需信息都在您的住宿页面上。\n\n此致敬意，\nLucas, Architect’sHouse"], "before": ["您的住宿即将到来", "您好 {{guest}}：\n\n我们期待在 {{checkin}} 于 {{property}} 迎接您。到达详情、停车和门禁码都在您的住宿页面上。"], "checkin": ["到达所需的一切", "您好 {{guest}}：\n\n就是今天了。入住时间为 {{checkintime}} 起。钥匙盒密码和 Wi-Fi 信息在您的住宿页面上。"], "during": ["一切都好吗？", "您好 {{guest}}：\n\n希望您在 {{property}} 住得舒心。若缺少什么，直接回复此邮件即可。"], "departure": ["退房说明", "您好 {{guest}}：\n\n明天 {{checkouttime}} 前退房。简短清单在您的住宿页面上。"], "after": ["感谢您入住我们这里", "您好 {{guest}}：\n\n感谢您入住 {{property}}。希望您度过了一段愉快的时光。\n\n如需任何帮助，直接回复此邮件即可。"], "review": ["我们很想听听您的体验", "您好 {{guest}}：\n\n能花一分钟聊聊您在 {{property}} 的住宿吗？您的评价能帮助其他旅行者，也帮助我们改进。"]}, "ja": {"welcome": ["初回滞在 10% クーポン", "こんにちは。\n\nArchitect’sHouse にご登録いただきありがとうございます。初回予約が 10% オフになる専用コードです：\n\n    {{code}}\n\nお支払い時に入力してください。初回予約で使えるワンタイムコードで、フォームに自動入力されます。\n\nよろしくお願いいたします。\nLucas, Architect’sHouse"], "confirm": ["{{property}} へのご滞在が確定しました", "{{guest}} 様\n\nArchitect’sHouse へ直接ご予約いただきありがとうございます。{{checkin}} から {{checkout}} までの {{property}} へのご滞在が確定しました（予約番号 {{booking}}）。\n\nお支払い合計：{{total}}\n\n必要な情報はすべて滞在ページにあります。\n\nよろしくお願いいたします。\nLucas, Architect’sHouse"], "before": ["ご滞生が近づいています", "{{guest}} 様\n\n{{checkin}} に {{property}} でお迎えできるのを楽しみにしています。到着の詳細、駐車場、アクセスコードは滞在ページにあります。"], "checkin": ["ご到着に必要なすべて", "{{guest}} 様\n\nいよいよ本日です。チェックインは {{checkintime}} からです。キーボックスのコードと Wi-Fi の情報は滞在ページにあります。"], "during": ["順調ですか？", "{{guest}} 様\n\n{{property}} で快適にお過ごしであることと思います。不足があれば、このメッセージにご返信ください。"], "departure": ["チェックアウトのご案内", "{{guest}} 様\n\n明日のチェックアウトは {{checkouttime}} までです。短いチェックリストは滞在ページにあります。"], "after": ["ご宿泊ありがとうございました", "{{guest}} 様\n\n{{property}} へのご宿泊ありがとうございました。良い滞在でしたら幸いです。\n\nご入用のことがあれば、このメールにご返信ください。"], "review": ["ご体験をお聞かせください", "{{guest}} 様\n\n{{property}} でのご滞在について、少しお時間をお取りいただけますか？ご評価は他の旅人の助けになり、私たちの改善にもつながります。"]}, "ko": {"welcome": ["첫 숙박 10% 쿠폰", "안녕하세요.\n\nArchitect’sHouse를 구독해 주셔서 감사합니다. 첫 예약에 10% 할인되는 개인 코드입니다:\n\n    {{code}}\n\n결제 시 입력하세요. 첫 예약에 쓸 수 있는 일회용 코드이며, 결제 양식이 자동으로 입력해 드립니다.\n\n감사합니다.\nLucas, Architect’sHouse"], "confirm": ["{{property}} 숙박이 확정되었습니다", "안녕하세요 {{guest}} 님.\n\nArchitect’sHouse에 직접 예약해 주셔서 감사합니다. {{checkin}}부터 {{checkout}}까지의 {{property}} 숙박이 확정되었습니다 (예약번호 {{booking}}).\n\n결제 합계: {{total}}\n\n필요한 모든 정보는 숙박 페이지에 있습니다.\n\n감사합니다.\nLucas, Architect’sHouse"], "before": ["숙박이 곧 다가옵니다", "안녕하세요 {{guest}} 님.\n\n{{checkin}}에 {{property}}에서 맞이할 날을 고대하고 있습니다. 도착 정보, 주차, 출입 코드는 숙박 페이지에 있습니다."], "checkin": ["도착에 필요한 모든 것", "안녕하세요 {{guest}} 님.\n\n오늘이 그날입니다. 체크인은 {{checkintime}}부터입니다. 키박스 코드와 Wi-Fi 정보는 숙박 페이지에 있습니다."], "during": ["잘 지내고 계신가요?", "안녕하세요 {{guest}} 님.\n\n{{property}}에서 편안하게 지내시길 바랍니다. 부족한 것이 있으면 이 메시지에 회신해 주세요."], "departure": ["체크아웃 안내", "안녕하세요 {{guest}} 님.\n\n내일 {{checkouttime}}까지 체크아웃입니다. 간단한 체크리스트는 숙박 페이지에 있습니다."], "after": ["머물러 주셔서 감사합니다", "안녕하세요 {{guest}} 님.\n\n{{property}}에 머물러 주셔서 감사합니다. 즐거운 숙박이셨기를 바랍니다.\n\n필요한 것이 있으시면 이 이메일에 회신해 주세요."], "review": ["경험을 듣고 싶습니다", "안녕하세요 {{guest}} 님.\n\n{{property}} 숙박에 대해 잠시 이야기해 주시겠어요?고객님의 후기는 다른 여행자와 저희 모두에게 큰 도움이 됩니다."]}, "sv": {"welcome": ["Din 10%-kod för din första vistelse", "Hej,\n\nTack för att du prenumererar på Architect’sHouse. Här är din personliga kod för 10 % rabatt på din första bokning:\n\n    {{code}}\n\nAnge den i kassan. Det är en engångskod, giltig på din första bokning, och formuläret fyller i den åt dig automatiskt.\n\nVänliga hälsningar,\nLucas, Architect’sHouse"], "confirm": ["Ditt boende på {{property}} är bekräftat", "Hej {{guest}},\n\nTack för att du bokade direkt hos Architect’sHouse. Ditt boende på {{property}} från {{checkin}} till {{checkout}} är bekräftat (bokning {{booking}}).\n\nTotalt betalt: {{total}}\n\nAllt du behöver finns på sidan för ditt boende.\n\nVänliga hälsningar,\nLucas, Architect’sHouse"], "before": ["Ditt boende närmar sig", "Hej {{guest}},\n\nVi ser fram emot att välkomna dig på {{property}} den {{checkin}}. Dina ankomstdetaljer, parkering och åtkomstkod finns på sidan för ditt boende."], "checkin": ["Allt du behöver för din ankomst", "Hej {{guest}},\n\nIdag är dagen. Incheckning från {{checkintime}}. Kod för nyckelboxen och Wi-Fi finns på sidan för ditt boende."], "during": ["Går allt bra?", "Hej {{guest}},\n\nVi hoppas att du trivs på {{property}}. Om något saknas, svara bara på det här meddelandet."], "departure": ["Dina utcheckningsinstruktioner", "Hej {{guest}},\n\nUtcheckning är imorgon fram till {{checkouttime}}. En kort checklista finns på sidan för ditt boende."], "after": ["Tack för att du bodde hos oss", "Hej {{guest}},\n\nTack för att du bodde på {{property}}. Vi hoppas att du hade en trevlig vistelse.\n\nOm du behöver något, svara bara på det här e-postmeddelandet."], "review": ["Vi vill gärna höra om din upplevelse", "Hej {{guest}},\n\nHar du en minut att berätta om din vistelse på {{property}}? Ditt omdöme hjälper andra resenärer och hjälper oss att bli bättre."]}, "da": {"welcome": ["Din 10%-kode til dit første ophold", "Hej,\n\nTak fordi du abonnerer på Architect’sHouse. Her er din personlige kode til 10 % rabat på din første booking:\n\n    {{code}}\n\nIndtast den ved kassen. Det er en engangskode, gældende på din første booking, og formularen udfylder den automatisk for dig.\n\nVenlig hilsen,\nLucas, Architect’sHouse"], "confirm": ["Dit ophold på {{property}} er bekræftet", "Hej {{guest}},\n\nTak fordi du bookede direkte hos Architect’sHouse. Dit ophold på {{property}} fra {{checkin}} til {{checkout}} er bekræftet (booking {{booking}}).\n\nI alt betalt: {{total}}\n\nAlt hvad du behøver findes på siden for dit ophold.\n\nVenlig hilsen,\nLucas, Architect’sHouse"], "before": ["Dit ophold nærmer sig", "Hej {{guest}},\n\nVi glæder os til at byde dig velkommen på {{property}} den {{checkin}}. Dine ankomstdetaljer, parkering og adgangskode findes på siden for dit ophold."], "checkin": ["Alt hvad du behøver til din ankomst", "Hej {{guest}},\n\nI dag er dagen. Check-in fra {{checkintime}}. Kode til nøgleboksen og Wi-Fi findes på siden for dit ophold."], "during": ["Går alt godt?", "Hej {{guest}},\n\nVi håber, at du falder godt til på {{property}}. Hvis noget mangler, så svar bare på denne besked."], "departure": ["Dine udcheckningsinstruktioner", "Hej {{guest}},\n\nCheck-out er i morgen indtil {{checkouttime}}. En kort tjekliste findes på siden for dit ophold."], "after": ["Tak fordi du boede hos os", "Hej {{guest}},\n\nTak fordi du boede på {{property}}. Vi håber, at du havde et godt ophold.\n\nHvis du har brug for noget, så svar bare på denne e-mail."], "review": ["Vi vil gerne høre om din oplevelse", "Hej {{guest}},\n\nHar du et minut til at fortælle os om dit ophold på {{property}}? Din anmeldelse hjælper andre rejsende og hjælper os med at blive bedre."]}, "no": {"welcome": ["Din 10%-kode til ditt første opphold", "Hei,\n\nTakk for at du abonnerer på Architect’sHouse. Her er din personlige kode for 10 % rabatt på din første bestilling:\n\n    {{code}}\n\nSkriv den inn i kassen. Det er en engangskode, gyldig på din første bestilling, og skjemaet fyller den inn automatisk for deg.\n\nVennlig hilsen,\nLucas, Architect’sHouse"], "confirm": ["Oppholdet ditt på {{property}} er bekreftet", "Hei {{guest}},\n\nTakk for at du bestilte direkte hos Architect’sHouse. Oppholdet ditt på {{property}} fra {{checkin}} til {{checkout}} er bekreftet (bestilling {{booking}}).\n\nTotalt betalt: {{total}}\n\nAlt du trenger finnes på siden for oppholdet ditt.\n\nVennlig hilsen,\nLucas, Architect’sHouse"], "before": ["Oppholdet ditt nærmer seg", "Hei {{guest}},\n\nVi gleder oss til å ønske deg velkommen på {{property}} den {{checkin}}. Ankomstdetaljer, parkering og tilgangskode finnes på siden for oppholdet ditt."], "checkin": ["Alt du trenger til ankomsten", "Hei {{guest}},\n\nI dag er dagen. Innsjekking fra {{checkintime}}. Kode til nøkkelboksen og Wi-Fi finnes på siden for oppholdet ditt."], "during": ["Går alt bra?", "Hei {{guest}},\n\nVi håper du trives på {{property}}. Hvis noe mangler, er det bare å svare på denne meldingen."], "departure": ["Utsjekkingsinstruksjonene dine", "Hei {{guest}},\n\nUtsjekking er i morgen fram til {{checkouttime}}. En kort sjekkliste finnes på siden for oppholdet ditt."], "after": ["Takk for at du bodde hos oss", "Hei {{guest}},\n\nTakk for at du bodde på {{property}}. Vi håper du hadde et fint opphold.\n\nHvis du trenger noe, er det bare å svare på denne e-posten."], "review": ["Vi vil gjerne høre om opplevelsen din", "Hei {{guest}},\n\nHar du et minutt til å fortelle oss om oppholdet ditt på {{property}}? Anmeldelsen din hjelper andre reisende og hjelper oss å bli bedre."]}, "fi": {"welcome": ["10 % koodi ensimmäiseen majoitukseesi", "Hei,\n\nKiitos tilauksestasi Architect’sHouselle. Tässä henkilökohtainen koodisi 10 % alennukseen ensimmäisestä varauksestasi:\n\n    {{code}}\n\nSyötä se kassalla. Se on kertakäyttöinen koodi, joka kelpaa ensimmäiseen varaukseesi, ja lomake täyttää sen automaattisesti.\n\nYstävällisin terveisin,\nLucas, Architect’sHouse"], "confirm": ["Majoituksesi kohteessa {{property}} on vahvistettu", "Hei {{guest}},\n\nKiitos, että varasit suoraan Architect’sHouselta. Majoituksesi kohteessa {{property}} {{checkin}} – {{checkout}} on vahvistettu (varaus {{booking}}).\n\nMaksettu yhteensä: {{total}}\n\nKaikki tarvittava on majoituksesi sivulla.\n\nYstävällisin terveisin,\nLucas, Architect’sHouse"], "before": ["Majoituksesi lähestyy", "Hei {{guest}},\n\nOdotamme innolla saapumistasi kohteeseen {{property}} {{checkin}}. Saapumistiedot, pysäköinti ja pääsykoodi ovat majoituksesi sivulla."], "checkin": ["Kaikki saapumiseen", "Hei {{guest}},\n\nTänään on se päivä. Sisäänkirjautuminen alkaen {{checkintime}}. Avainlaatikon koodi ja Wi-Fi ovat majoituksesi sivulla."], "during": ["Meneekö kaikki hyvin?", "Hei {{guest}},\n\nToivomma, että viihdyt kohteessa {{property}}. Jos jokin puuttuu, vastaa tähän viestiin."], "departure": ["Uloskirjautumisohjeet", "Hei {{guest}},\n\nUloskirjautuminen huomenna klo {{checkouttime}} asti. Lyhyt tarkistuslista on majoituksesi sivulla."], "after": ["Kiitos, että asuitte meillä", "Hei {{guest}},\n\nKiitos, että asuitte kohteessa {{property}}. Toivomme, että majoitus oli miellyttävä.\n\nJos tarvitset jotakin, vastaa tähän sähköpostiin."], "review": ["Kuulisimme mielellämme kokemuksestasi", "Hei {{guest}},\n\nOnko sinulla minuutti kertoa meille majoituksestasi kohteessa {{property}}? Arviosi auttaa muita matkailijoita ja auttaa meitä kehittymään."]}})

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


TEXTS.update({"de": {"boton": "Meine Aufenthaltsseite öffnen", "pie": "Sie erhalten diese E-Mail, weil Sie bei uns eine Buchung haben. Antworten Sie einfach auf diese Nachricht, wenn Sie etwas brauchen."}, "pt": {"boton": "Abrir a página da minha estadia", "pie": "Recebe este email porque tem uma reserva connosco. Se precisar de qualquer coisa, responda a esta mensagem."}, "nl": {"boton": "De pagina van mijn verblijf openen", "pie": "U ontvangt deze e-mail omdat u een boeking bij ons heeft. Antwoord gewoon op dit bericht als u iets nodig heeft."}, "pl": {"boton": "Otwórz stronę mojego pobytu", "pie": "Otrzymujesz ten e-mail, ponieważ masz u nas rezerwację. Jeśli czegoś potrzebujesz, po prostu odpowiedz na tę wiadomość."}, "cs": {"boton": "Otevřít stránku mého pobytu", "pie": "Tento e-mail dostáváte, protože u nás máte rezervaci. Pokud něco potřebujete, stačí odpovědět na tuto zprávu."}, "ru": {"boton": "Открыть страницу моего проживания", "pie": "Вы получили это письмо, потому что у вас есть бронирование у нас. Если вам что-то нужно, просто ответьте на это сообщение."}, "uk": {"boton": "Відкрити сторінку мого проживання", "pie": "Ви отримали цей лист, тому що у вас є бронювання в нас. Якщо вам щось потрібно, просто відповідьте на це повідомлення."}, "tr": {"boton": "Konaklama sayfamı aç", "pie": "Bizde bir rezervasyonunuz olduğu için bu e-postayı alıyorsunuz. Bir şeye ihtiyacınız olursa bu mesajı yanıtlamanız yeterli."}, "ar": {"boton": "فتح صفحة إقامتي", "pie": "تصلك هذه الرسالة لأن لديك حجزاً معنا. إذا احتجت أي شيء، فقط أجب على هذه الرسالة."}, "zh": {"boton": "打开我的住宿页面", "pie": "您收到这封邮件是因为您在我们这里有预订。如需任何帮助，直接回复此邮件即可。"}, "ja": {"boton": "滞在ページを開く", "pie": "ご予約があるため、このメールをお送りしています。ご不明な点があれば、このメッセージにご返信ください。"}, "ko": {"boton": "숙박 페이지 열기", "pie": "저희와 예약이 있으므로 이 이메일을 받으셨습니다. 필요한 것이 있으시면 이 메시지에 회신해 주세요."}, "sv": {"boton": "Öppna sidan för mitt boende", "pie": "Du får det här e-postmeddelandet för att du har en bokning hos oss. Svara bara på det här meddelandet om du behöver något."}, "da": {"boton": "Åbn siden med mit ophold", "pie": "Du modtager denne e-mail, fordi du har en booking hos os. Svar bare på denne besked, hvis du har brug for noget."}, "no": {"boton": "Åpne siden med oppholdet mitt", "pie": "Du får denne e-posten fordi du har en bestilling hos oss. Svar bare på denne meldingen hvis du trenger noe."}, "fi": {"boton": "Avaa majoitukseni sivu", "pie": "Saat tämän sähköpostin, koska sinulla on varaus meillä. Jos tarvitset jotakin, vastaa tähän viestiin."}})


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
    if idi in DEFAULT:
        fab = DEFAULT.get(idi).get(key, ('', ''))
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
        # 'pending' = creada pero aún sin cobrar (pago con tarjeta en curso):
        # no se manda nada hasta que la pasarela la marque como 'paid'.
        if str(r.get('status', '')) == 'pending': continue
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
    settings = como_dic(fs.get('private/data/meta/main')).get('settings') or {}
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
        # Las confirmaciones las manda el bucle principal: si la web dejó una
        # en cola, se descarta (si no, el huésped recibe dos correos).
        if str(m.get('key') or '') == 'confirm':
            if not dry:
                try: fs.patch('outbox/' + str(did), {'status': 'skipped'})
                except Exception: pass
            continue
        cuerpo = str(m.get('body') or '').strip()
        if not cuerpo or not str(m.get('subject') or '').strip():
            log('  \u26a0 cola p\u00fablica %s \u2192 %s: sin texto, NO lo env\u00edo'
                % (m.get('key') or '?', m.get('to')))
            if not dry:
                try: fs.patch('outbox/' + str(did), {'status': 'skipped'})
                except Exception: pass
            continue
        clave = 'outbox_' + str(did)
        prev = fs.get('mail_log/' + clave)
        if prev == '__unknown__':
            continue
        if prev is not None:
            if not dry:
                try: fs.patch('outbox/' + str(did), {'status': 'sent'})
                except Exception: pass
            continue
        dest   = test_to or m.get('to')
        asunto = str(m.get('subject') or '')
        if test_to: asunto = '[PRUEBA] ' + asunto
        if not dry and not test_to:
            r = fs.create('mail_log', clave, {'resId': '', 'key': m.get('key', 'welcome'),
                                              'to': dest, 'subject': asunto,
                                              'date': TODAY().isoformat(),
                                              'sentAt': dt.datetime.now(TZ).isoformat(),
                                              'status': 'sending'})
            if '__error__' in (r or {}):
                continue
        ok, info = send_resend(api, frm, dest, asunto, cuerpo,
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
            if not dry and not test_to:
                try: fs.delete('mail_log/' + clave)
                except Exception: pass
            log('  \u2717 cola p\u00fablica %s \u2192 %s: %s' % (m.get('key') or '?', dest, info))
    if cola:
        log('Cola p\u00fablica: %d enviados \u00b7 %d fallidos' % (enviados, fallidos))

    # ------------------------------------------------- 2. bandeja del panel ------
    meta = como_dic(fs.get('private/data/meta/main'))
    outbox = meta.get('outbox') or []
    if isinstance(outbox, list) and outbox:
        pend = [m for m in outbox if isinstance(m, dict)
                and str(m.get('status', 'queued')) not in ('sent', 'skipped')
                and '@' in str(m.get('to') or '')]
        if pend:
            log('[cola] %d mensaje(s) pendiente(s) en la bandeja de salida' % len(pend))
        env2 = fall2 = 0
        cambiado = False
        for m in pend:
            # Las confirmaciones las manda el bucle principal: las que haya en
            # la bandeja se retiran (si no, el huésped recibe dos correos).
            if str(m.get('key') or '') == 'confirm':
                m['status'] = 'skipped'
                m['nota'] = 'la confirmaci\u00f3n la env\u00eda el programa'
                cambiado = True
                continue
            cuerpo = str(m.get('body') or '').strip()
            if not cuerpo or not str(m.get('subject') or '').strip():
                log('  \u26a0 cola %s \u2192 %s: sin texto, NO lo env\u00edo'
                    % (m.get('key') or '?', m.get('to')))
                m['status'] = 'skipped'; m['nota'] = 'sin texto'
                cambiado = True
                continue
            clave = 'outbox_' + str(m.get('id') or ('%s_%s' % (m.get('to'), m.get('date'))))
            prev = fs.get('mail_log/' + clave)
            if prev == '__unknown__':
                continue
            if prev is not None:
                m['status'] = 'sent'; cambiado = True
                continue
            dest   = test_to or m['to']
            asunto = str(m.get('subject') or '')
            if test_to: asunto = '[PRUEBA] ' + asunto
            if not dry and not test_to:
                r = fs.create('mail_log', clave, {'resId': '', 'key': m.get('key', 'welcome'),
                                                  'to': dest, 'subject': asunto,
                                                  'date': TODAY().isoformat(),
                                                  'sentAt': dt.datetime.now(TZ).isoformat(),
                                                  'status': 'sending'})
                if '__error__' in (r or {}):
                    continue
            ok, info = send_resend(api, frm, dest, asunto, cuerpo,
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
                if not dry and not test_to:
                    try: fs.delete('mail_log/' + clave)
                    except Exception: pass
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
        d = como_dic(fs.get('mailrun/last'))
        t = d.get('at')
        if t:
            ult = dt.datetime.fromisoformat(str(t))
            if ult.tzinfo is None:
                ult = ult.replace(tzinfo=dt.timezone.utc)
            if (ahora - ult) < dt.timedelta(minutes=minutos):
                return False
        fs.set('mailrun', 'last', {'at': ahora.isoformat(), 'run': mio, 'sent': 0})
        time.sleep(3)                       # por si dos ejecuciones coinciden
        d2 = como_dic(fs.get('mailrun/last'))
        return d2.get('run') == mio or not d2.get('run')
    except Exception as e:
        log('  (aviso: no he podido comprobar el turno, sigo igualmente: %s)' % e)
        return True


def anotar_ejecucion(fs, enviados, hora):
    """Deja constancia de cuánto ha enviado esta pasada."""
    try:
        d = como_dic(fs.get('mailrun/last'))
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
    settings = como_dic(fs.get('private/data/meta/main')).get('settings') or {}
    mail_edits = (settings or {}).get('mailEdits') or {}
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
        clave = '%s_%s' % (rid, m['key'])
        es_reenvio = False
        if not dry and not test_to:
            prev = fs.get('mail_log/' + clave)
            if prev == '__unknown__':
                # No puedo comprobar si ya se envió (fallo transitorio):
                # mejor retrasar el correo que mandarlo dos veces.
                skipped += 1
                log('  · no puedo comprobar el registro (error transitorio): %s' % clave)
                continue
            if prev is not None:
                pd = str(prev.get('date') or prev.get('sentAt') or '')[:10]
                rc = str(m['res'].get('created') or '')[:10]
                if not (pd and rc and pd < rc):
                    skipped += 1
                    log('  · ya enviado: %s %s' % (rid, m['key']))
                    continue
                # La referencia se reutilizó (reserva anterior borrada con el
                # mismo id): ese registro es de OTRA reserva y no cuenta.
                log('  · registro viejo de una reserva borrada con la misma referencia: reenvío %s %s' % (rid, m['key']))
                es_reenvio = True
        editado = None
        if not dry and not test_to:
            ed = (mail_edits or {}).get(clave)
            if isinstance(ed, dict) and str(ed.get('status', '')) == 'pendiente':
                try:
                    m['subject'] = render(ed.get('subject') or m['subject'], m['res'], m.get('prop'), site, m.get('lang'))
                    m['body'] = render(ed.get('body') or m['body'], m['res'], m.get('prop'), site, m.get('lang'))
                    editado = clave
                    log('  · mensaje editado a mano en el panel: %s' % clave)
                except Exception as e:
                    log('  (aviso: no he podido aplicar la edicion de %s: %s)' % (clave, e))
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
        if not dry and not test_to and not es_reenvio:
            # Reclamo atómico ANTES de enviar: si el registro ya existe, otra
            # pasada lo mandó (o lo está mandando) y este correo NO sale otra vez.
            r = fs.create('mail_log', clave, {'resId': rid, 'key': m['key'], 'to': dest,
                                              'subject': asunto, 'date': TODAY().isoformat(),
                                              'sentAt': dt.datetime.now(TZ).isoformat(),
                                              'status': 'sending'})
            if '__error__' in (r or {}):
                skipped += 1
                log('  · otra pasada se ha adelantado con este correo: %s' % clave)
                continue
        ok, info = send_resend(api, frm, dest, asunto, m['body'], site, reply, dry,
                                     m.get('link', ''), m.get('prop'), m.get('lang'))
        if ok:
            sent += 1
            if not dry and not test_to:
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
                if editado:
                    try:
                        mail_edits[editado]['status'] = 'usado'
                        fs.patch('private/data/meta/main', {'settings.mailEdits': mail_edits})
                    except Exception as e:
                        log('  (aviso: no he podido marcar la edicion como usada: %s)' % e)
            log('  ✓ %s %s -> %s%s' % (rid, m['key'], dest, '  [PRUEBA]' if test_to else ''))
        else:
            failed += 1
            if not dry and not test_to and not es_reenvio:
                # El envío falló: libero el reclamo para que la próxima pasada
                # pueda intentarlo de nuevo.
                try: fs.delete('mail_log/' + clave)
                except Exception: pass
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
