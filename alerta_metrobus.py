import os
import re
import sys
import time
import html
import logging
import requests
import zipfile
import io
import json
import csv
import math
import unicodedata
import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from google.transit import gtfs_realtime_pb2

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
USUARIO = os.getenv("USUARIO_API_KEY")
SENHA = os.getenv("SENHA_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

ZONA_CDMX = ZoneInfo("America/Mexico_City")
MI_LINEA = "Línea 1"

# Posiciones GPS más viejas que esto se descartan (buses apagados / en patio)
MAX_ANTIGUEDAD_GPS_SEG = 300
# Velocidades por encima de esto se consideran error de GPS
VEL_MAX_VALIDA_KMH = 65.0
# El feed de SinopticoPlus reporta position.speed en km/h (verificado comparando contra el
# desplazamiento real de los buses), NO en m/s como dice el estándar GTFS-RT.
FACTOR_VELOCIDAD_A_KMH = 1.0
# Tope y respaldo de velocidad para el ETA (km/h)
VEL_ETA_TOPE = 22.0
VEL_ETA_RESPALDO = 15.0
# Termómetro: promedio de velocidad (incluye buses detenidos en estación)
TERMO_FLUIDO_KMH = 18.0
TERMO_MODERADO_KMH = 12.0
# Un bus cuenta como "en el corredor de L1" si está a menos de esto de una estación de L1
CORREDOR_MAX_KM = 0.3
# Una parada es "de L1" si la usa al menos este % de los viajes de servicios propios de L1
UMBRAL_PARADA_L1 = 0.02
# Tu viaje
ESTACION_NORTE = ("Indios Verdes", 19.4954, -99.1195)
ESTACION_SUR = ("Polifórum", 19.3932, -99.1727)
# Hotspots: N+ unidades del mismo sentido en este radio, con velocidad promedio menor al umbral
HOTSPOT_RADIO_KM = 0.4
HOTSPOT_MIN_UNIDADES = 4
HOTSPOT_VEL_MAX_KMH = 10.0
HOTSPOT_MAX_MOSTRADOS = 3
# Cache del GTFS estático (rutas + paradas de L1). Se guarda en el repo junto al historial.
ARCHIVO_CACHE_GTFS = "gtfs_estatico.json"
CACHE_GTFS_VERSION = 2       # si cambia el formato, las caches viejas se regeneran solas
GTFS_CACHE_DIAS = 7            # se renueva al menos una vez por semana
GTFS_RECARGA_MIN_HORAS = 6     # si aparecen rutas desconocidas, renueva solo si la cache tiene más de esto

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def crear_sesion() -> requests.Session:
    """Sesión HTTP con reintentos automáticos (backoff) ante errores transitorios."""
    sesion = requests.Session()
    reintentos = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
    )
    adaptador = HTTPAdapter(max_retries=reintentos)
    sesion.mount("https://", adaptador)
    sesion.mount("http://", adaptador)
    return sesion


def normalizar(texto: str) -> str:
    """'Polifórum.' -> 'poliforum'  |  'Indios Verdes L1' -> 'indios verdes l1'"""
    sin_acentos = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", sin_acentos.lower()).strip()


def esc(texto) -> str:
    """Escapa texto dinámico para parse_mode HTML de Telegram."""
    return html.escape(str(texto), quote=False)


class MetrobusMonitor:
    def __init__(self, url_semovi: str):
        self.url_semovi = url_semovi
        self.http = crear_sesion()

        tiempo_cdmx = datetime.datetime.now(ZONA_CDMX)
        self.tiempo_cdmx = tiempo_cdmx
        self.datos_historicos = {
            "Fecha": tiempo_cdmx.strftime("%Y-%m-%d"),
            "Hora": tiempo_cdmx.strftime("%H:%M"),
            "Turno": "Mañana" if tiempo_cdmx.hour < 12 else "Tarde",
            "Falla_Oficial": 0,
            "Lluvia": 0,
            "ETA_Minutos": 0,
            "Unidades_Cercanas": 0,
            "Velocidad_Norte": 0.0,
            "Velocidad_Sur": 0.0,
            "Hotspots_L1": 0
        }

    # ESCUDO ANTIRREBOTE
    def ejecucion_reciente(self) -> bool:
        """Verifica si el script ya envió un reporte en los últimos 3 minutos"""
        nombre_archivo = "historial_metrobus.csv"
        if not os.path.isfile(nombre_archivo):
            return False

        try:
            with open(nombre_archivo, mode='r', encoding='utf-8') as archivo:
                filas = list(csv.DictReader(archivo))
                if not filas:
                    return False

                ultima_fila = filas[-1]
                fecha_str = ultima_fila.get("Fecha")
                hora_str = ultima_fila.get("Hora")

                if not fecha_str or not hora_str:
                    return False

                ultima_ejecucion = datetime.datetime.strptime(
                    f"{fecha_str} {hora_str}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=ZONA_CDMX)

                minutos_diferencia = (self.tiempo_cdmx - ultima_ejecucion).total_seconds() / 60.0

                # Si han pasado menos de 3 minutos, es un rebote de Telegram
                if 0 <= minutos_diferencia < 3.0:
                    return True

        except Exception as e:
            logging.error(f"Error comprobando historial antirrebote: {e}")

        return False

    @staticmethod
    def calcular_distancia(lat1, lon1, lat2, lon2):
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    def obtener_clima(self) -> str:
        if not OPENWEATHER_API_KEY:
            return ""

        es_manana = self.datos_historicos["Turno"] == "Mañana"
        lat = 19.4954 if es_manana else 19.3946
        lon = -99.1195 if es_manana else -99.1746

        try:
            res = self.http.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "lang": "es", "units": "metric"},
                timeout=10,
            )
            res.raise_for_status()
            data = res.json()

            weather_id = data['weather'][0]['id']
            if weather_id < 700:
                self.datos_historicos["Lluvia"] = 1
                desc = esc(data['weather'][0]['description'].capitalize())
                return (f"🌧️ <b>ALERTA METEOROLÓGICA</b>\n<i>Clima en tu zona:</i> {desc}. "
                        "Se activa marcha de seguridad. Considera +15 min en tu traslado.")

            return ""
        except Exception as e:
            logging.error(f"Error consultando OpenWeather: {e}")
            return ""

    @staticmethod
    def _nombre_linea_desde_celda(celda):
        """Intenta leer 'Línea N' del texto, alt/title o src de la imagen de la primera celda."""
        candidatos = [celda.get_text(" ", strip=True)]
        for img in celda.find_all("img"):
            candidatos += [img.get("alt", ""), img.get("title", ""), img.get("src", "")]
        for texto in candidatos:
            m = re.search(r"l[ií]nea\s*(\d+)", texto or "", re.IGNORECASE)
            if m:
                return f"Línea {m.group(1)}"
        return None

    def obtener_estado_oficial(self) -> str:
        if not SCRAPER_API_KEY:
            return "Error: Falta la clave de ScraperAPI."

        problemas_mi_linea, problemas_otras = [], []
        try:
            parametros_proxy = {
                'api_key': SCRAPER_API_KEY,
                'url': self.url_semovi,
                'country_code': 'mx'
            }

            # HTTPS: la API key ya no viaja en texto plano
            respuesta = self.http.get('https://api.scraperapi.com/', params=parametros_proxy, timeout=70)
            respuesta.raise_for_status()

            soup = BeautifulSoup(respuesta.text, 'html.parser')

            # Respaldo por posición (si no se puede leer el nombre de la línea de la tabla)
            mapeo_lineas = {
                1: "Línea 1", 2: "Línea 2", 3: "Línea 3",
                4: "Línea 4", 5: "Línea 4", 6: "Línea 5",
                7: "Línea 6", 8: "Línea 7"
            }

            for tabla in soup.find_all('table'):
                if 'estaciones afectadas' not in tabla.text.lower():
                    continue
                for num_fila, fila in enumerate(tabla.find_all('tr')[1:], start=1):
                    celdas = fila.find_all('td')
                    if len(celdas) < 3:
                        continue

                    linea_nombre = (self._nombre_linea_desde_celda(celdas[0])
                                    or mapeo_lineas.get(num_fila, f"Línea {num_fila}"))
                    est = celdas[1].get_text(strip=True)
                    afec = celdas[2].get_text(strip=True)

                    est_limpio = est.lower().replace("estado", "").strip()
                    afec_limpio = afec.lower().replace("estaciones afectadas", "").strip()

                    info_adicional = ""
                    if len(celdas) >= 4:
                        info_adicional = celdas[3].get_text(strip=True).replace("Información adicional", "").strip()

                    if "servicio regular" not in est_limpio or "ninguna" not in afec_limpio:
                        reporte_linea = f"- <b>{esc(linea_nombre)}</b>: {esc(est)} | {esc(afec)}"
                        if info_adicional and info_adicional.lower() != "ninguna":
                            reporte_linea += f" | Info: {esc(info_adicional)}"

                        if linea_nombre == MI_LINEA:
                            problemas_mi_linea.append(reporte_linea)
                        else:
                            problemas_otras.append(reporte_linea)

            bloques = []
            if problemas_mi_linea:
                # Solo una afectación en TU línea cuenta como falla
                self.datos_historicos["Falla_Oficial"] = 1
                bloques.append(f"🚨 <b>AFECTACIÓN EN {MI_LINEA.upper()} (Oficial):</b>\n" + "\n".join(problemas_mi_linea))
            else:
                bloques.append(f"✅ <b>{MI_LINEA}:</b> Servicio regular (Oficial).")

            if problemas_otras:
                bloques.append("ℹ️ <i>Otras líneas con afectación:</i>\n" + "\n".join(problemas_otras))

            return "\n\n".join(bloques)

        except Exception as e:
            logging.error(f"Error en extracción SEMOVI: {e}")
            return f"⚠️ Estado oficial no disponible (SEMOVI): {esc(type(e).__name__)}"

    @staticmethod
    def cargar_estatico_cache():
        """Lee la cache del GTFS estático. Devuelve None si no existe, está corrupta o caducó."""
        try:
            with open(ARCHIVO_CACHE_GTFS, encoding='utf-8') as f:
                datos = json.load(f)
            edad_dias = (time.time() - datos['generado_ts']) / 86400
            if datos.get('version') != CACHE_GTFS_VERSION:
                logging.info("Cache GTFS con formato anterior; se regenerará.")
                return None
            if edad_dias > GTFS_CACHE_DIAS or not datos.get('rutas') or not datos.get('paradas'):
                logging.info(f"Cache GTFS caducada ({edad_dias:.1f} días); se renovará.")
                return None
            logging.info(f"Cache GTFS usada (generada hace {edad_dias:.1f} días).")
            return datos
        except FileNotFoundError:
            logging.info("No hay cache GTFS; se descargará el estático.")
        except Exception as e:
            logging.warning(f"Cache GTFS ilegible ({e}); se descargará el estático.")
        return None

    def descargar_estatico(self, url_static: str) -> dict:
        """
        Descarga el ZIP estático y extrae lo necesario para TU línea:
          - rutas: route_id -> "Línea N" (todas, para identificar buses)
          - info_rutas: por cada servicio de tu línea, su sentido (N/S) y estaciones
          - paradas: estaciones reales de tu línea (para corredor y nombres de hotspots)
        Lo guarda como JSON compacto para no volver a bajar el ZIP en cada ejecución.
        """
        t0 = time.monotonic()
        zip_res = self.http.get(url_static, timeout=90)
        zip_res.raise_for_status()

        mapa_rutas, info_rutas, todas_paradas = {}, {}, {}
        num_linea = MI_LINEA.split()[-1]

        with zipfile.ZipFile(io.BytesIO(zip_res.content)) as z:
            with z.open('routes.txt') as f:
                for fila in csv.DictReader(io.TextIOWrapper(f, 'utf-8-sig')):
                    corto = fila.get('route_short_name', '').strip()
                    mapa_rutas[fila['route_id']] = f"Línea {corto}" if corto else ""
                    if corto == num_linea:
                        nombre_largo = fila.get('route_long_name', '').strip()
                        info_rutas[fila['route_id']] = {
                            'nombre': nombre_largo,
                            'sentido': self._sentido_desde_nombre(nombre_largo),
                            # "L01..." = servicio propio de L1; "L21c21", "L31a31" = servicios
                            # combinados con otras líneas (tienen paradas de L2/L3)
                            'propia': normalizar(nombre_largo).startswith(f"l{int(num_linea):02d}"),
                        }

            with z.open('stops.txt') as f:
                for fila in csv.DictReader(io.TextIOWrapper(f, 'utf-8-sig')):
                    try:
                        todas_paradas[fila['stop_id']] = {
                            'nombre': fila.get('stop_name', 'Estación Desconocida').strip().rstrip('.'),
                            'lat': round(float(fila['stop_lat']), 6),
                            'lon': round(float(fila['stop_lon']), 6)
                        }
                    except (KeyError, ValueError):
                        continue

            viaje_a_ruta = {}
            with z.open('trips.txt') as f:
                for fila in csv.DictReader(io.TextIOWrapper(f, 'utf-8-sig')):
                    rid = fila.get('route_id')
                    if rid in info_rutas:
                        viaje_a_ruta[fila.get('trip_id')] = rid
                        if info_rutas[rid]['sentido'] is None:  # respaldo: Ida = sur, Volta = norte
                            hs = normalizar(fila.get('trip_headsign', ''))
                            info_rutas[rid]['sentido'] = 'S' if hs == 'ida' else 'N' if hs == 'volta' else None

            # stop_times es enorme (~57 MB): se lee en streaming
            paradas_por_ruta = {rid: set() for rid in info_rutas}
            viajes_por_parada, viajes_propios = {}, set()
            with z.open('stop_times.txt') as f:
                for fila in csv.DictReader(io.TextIOWrapper(f, 'utf-8-sig')):
                    rid = viaje_a_ruta.get(fila.get('trip_id'))
                    if rid is None:
                        continue
                    sid = fila.get('stop_id')
                    paradas_por_ruta[rid].add(sid)
                    if info_rutas[rid]['propia']:
                        viajes_propios.add(fila.get('trip_id'))
                        viajes_por_parada[sid] = viajes_por_parada.get(sid, 0) + 1

        # Estaciones de L1: las que usa una fracción relevante de los viajes propios de L1.
        # Así quedan fuera las de L2/L3 que solo tocan servicios combinados o esporádicos.
        minimo = max(1, len(viajes_propios) * UMBRAL_PARADA_L1)
        ids_l1 = sorted(sid for sid, n in viajes_por_parada.items() if n >= minimo and sid in todas_paradas)
        paradas_l1 = [todas_paradas[s] for s in ids_l1]
        if not paradas_l1:
            logging.warning("No se pudieron identificar las paradas de L1; se usan todas las paradas.")

        for rid, info in info_rutas.items():
            info['paradas'] = sorted({normalizar(todas_paradas[s]['nombre'])
                                      for s in paradas_por_ruta[rid] if s in todas_paradas})

        datos = {
            'version': CACHE_GTFS_VERSION,
            'generado': datetime.datetime.now(ZONA_CDMX).isoformat(timespec='seconds'),
            'generado_ts': int(time.time()),
            'solo_l1': bool(paradas_l1),
            'rutas': mapa_rutas,
            'info_rutas': info_rutas,
            'paradas': paradas_l1 or list(todas_paradas.values()),
        }
        try:
            with open(ARCHIVO_CACHE_GTFS, 'w', encoding='utf-8') as f:
                json.dump(datos, f, ensure_ascii=False, separators=(',', ':'))
        except Exception as e:
            logging.warning(f"No se pudo guardar la cache GTFS: {e}")

        sin_sentido = [r for r, i in info_rutas.items() if i['sentido'] is None]
        logging.info(f"⏱️ GTFS estático descargado y procesado en {time.monotonic() - t0:.1f}s "
                     f"({len(datos['paradas'])} estaciones de L1, {len(info_rutas)} servicios"
                     f"{f', sin sentido: {sin_sentido}' if sin_sentido else ''}).")
        return datos

    @staticmethod
    def _sentido_desde_nombre(nombre_largo: str):
        """'L01a01-2 dr. gálvez - indios verdes' -> 'N'.  Sufijo -1 = hacia el sur, -2 = hacia el norte."""
        m = re.match(r"\s*\S+-([12])\s", nombre_largo or "")
        return {'1': 'S', '2': 'N'}.get(m.group(1)) if m else None

    @staticmethod
    def _coordenadas_estacion(paradas: list, nombre: str, respaldo: tuple) -> tuple:
        """Promedio de las paradas cuyo nombre contiene `nombre` (ambos andenes); si no hay, el respaldo."""
        objetivo = normalizar(nombre)
        coincidencias = [p for p in paradas if objetivo in normalizar(p['nombre'])]
        if not coincidencias:
            return respaldo
        return (sum(p['lat'] for p in coincidencias) / len(coincidencias),
                sum(p['lon'] for p in coincidencias) / len(coincidencias))

    def detectar_hotspots(self, buses: list, paradas: list) -> list:
        """
        Agrupa buses LENTOS del MISMO sentido que estén a <= HOTSPOT_RADIO_KM entre sí.
        Cada bus pertenece a un solo cluster, así que no hay hotspots repetidos.
        Devuelve lista de dicts ordenada por severidad.
        """
        terminales_ignoradas = ["indios verdes", "caminero", "gálvez", "galvez"]
        resultados = []

        for sentido, flecha, codigo in (("Norte", "⬆️", "N"), ("Sur", "⬇️", "S")):
            candidatos = [b for b in buses if b['sentido'] == codigo and b['speed'] is not None]

            # Vecinos de cada bus (mismo sentido, dentro del radio)
            vecinos = {}
            for b in candidatos:
                vecinos[b['id']] = [o for o in candidatos
                                    if self.calcular_distancia(b['lat'], b['lon'], o['lat'], o['lon']) <= HOTSPOT_RADIO_KM]

            # Se siembra primero desde los buses con más vecinos (cluster más denso)
            asignados = set()
            for semilla in sorted(candidatos, key=lambda b: -len(vecinos[b['id']])):
                if semilla['id'] in asignados:
                    continue
                cluster = [o for o in vecinos[semilla['id']] if o['id'] not in asignados]
                if len(cluster) < HOTSPOT_MIN_UNIDADES:
                    continue
                for o in cluster:  # se marcan SIEMPRE, sea o no hotspot, para no recontar
                    asignados.add(o['id'])

                vel_prom = sum(o['speed'] for o in cluster) / len(cluster)
                if vel_prom >= HOTSPOT_VEL_MAX_KMH:
                    continue

                c_lat = sum(o['lat'] for o in cluster) / len(cluster)
                c_lon = sum(o['lon'] for o in cluster) / len(cluster)
                estacion = min(paradas, key=lambda p: self.calcular_distancia(c_lat, c_lon, p['lat'], p['lon']),
                               default={'nombre': 'Desconocida'})['nombre']

                if any(t in estacion.lower() for t in terminales_ignoradas):
                    continue

                resultados.append({'sentido': sentido, 'flecha': flecha, 'estacion': estacion,
                                   'unidades': len(cluster), 'velocidad': vel_prom})

        # Más severo primero: más unidades y más lento
        resultados.sort(key=lambda h: (-h['unidades'], h['velocidad']))
        return resultados

    def procesar_datos_gtfs(self) -> tuple:
        if not USUARIO or not SENHA:
            return "", "", ""

        try:
            url_auth = "https://metrobus-gtfs.sinopticoplus.com/gtfs-api/partnerValidation"
            auth_res = self.http.post(url_auth, json={"usuario": USUARIO, "senha": SENHA}, timeout=15)
            auth_res.raise_for_status()
            urls = auth_res.json()

            t0 = time.monotonic()
            rt_res = self.http.get(urls['urlRealTime'], timeout=30)
            rt_res.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(rt_res.content)
            logging.info(f"⏱️ GTFS tiempo real descargado en {time.monotonic() - t0:.1f}s")

            # GTFS estático: desde la cache local; solo se descarga si no hay, está vieja
            # o el feed trae rutas que la cache no conoce (señal de que el GTFS cambió)
            estatico = self.cargar_estatico_cache()
            rutas_feed = {e.vehicle.trip.route_id for e in feed.entity
                          if e.HasField("vehicle") and e.vehicle.HasField("trip")}
            if estatico is not None:
                desconocidas = rutas_feed - set(estatico['rutas'])
                edad_h = (time.time() - estatico['generado_ts']) / 3600
                if desconocidas and edad_h > GTFS_RECARGA_MIN_HORAS:
                    logging.info(f"Rutas nuevas en el feed {sorted(desconocidas)[:5]}: se renueva la cache GTFS.")
                    estatico = None
            if estatico is None:
                estatico = self.descargar_estatico(urls['urlStatic'])

            mapa_rutas = estatico['rutas']
            info_rutas = estatico.get('info_rutas', {})
            mapa_paradas = estatico['paradas']

            # Referencia de tiempo: la del feed si viene, si no la hora actual
            ahora_ref = feed.header.timestamp if feed.header.HasField("timestamp") else int(time.time())

            es_manana = self.datos_historicos["Turno"] == "Mañana"
            norte = (ESTACION_NORTE[0],) + self._coordenadas_estacion(mapa_paradas, ESTACION_NORTE[0], ESTACION_NORTE[1:])
            sur = (ESTACION_SUR[0],) + self._coordenadas_estacion(mapa_paradas, ESTACION_SUR[0], ESTACION_SUR[1:])
            if es_manana:
                (estacion, lat_origen, lon_origen), destino, sentido_viaje = norte, sur[0], 'S'
            else:
                (estacion, lat_origen, lon_origen), destino, sentido_viaje = sur, norte[0], 'N'

            # Servicios que te sirven: pasan por tu origen y tu destino, en tu sentido
            rutas_utiles = {rid for rid, info in info_rutas.items()
                            if info.get('sentido') == sentido_viaje
                            and any(normalizar(estacion) in p for p in info.get('paradas', []))
                            and any(normalizar(destino) in p for p in info.get('paradas', []))}
            if not rutas_utiles:
                logging.warning("No se identificaron servicios útiles para tu viaje; se usan todos los de tu sentido.")

            buses_l1 = []
            descartados = {'viejos': 0, 'fuera_corredor': 0, 'sin_sentido': 0}

            for entidad in feed.entity:
                v = entidad.vehicle
                if not (entidad.HasField("vehicle") and v.HasField("trip") and v.HasField("position")):
                    continue
                rid = v.trip.route_id
                if mapa_rutas.get(rid) != MI_LINEA:
                    continue

                # Descarta posiciones viejas (buses apagados o en patio)
                if v.HasField("timestamp") and (ahora_ref - v.timestamp) > MAX_ANTIGUEDAD_GPS_SEG:
                    descartados['viejos'] += 1
                    continue

                pos = v.position
                # Los servicios combinados (L1-L2, L1-L3) recorren otros corredores: solo
                # cuentan cuando el bus está físicamente sobre la L1
                dist_corredor = min((self.calcular_distancia(pos.latitude, pos.longitude, p['lat'], p['lon'])
                                     for p in mapa_paradas), default=0.0)
                if dist_corredor > CORREDOR_MAX_KM:
                    descartados['fuera_corredor'] += 1
                    continue

                # Sentido: del servicio (confiable). El bearing del GPS falla ~25% y a veces trae >360°
                sentido = info_rutas.get(rid, {}).get('sentido')
                if sentido is None and v.trip.HasField("direction_id"):
                    sentido = 'S' if v.trip.direction_id == 0 else 'N'
                if sentido is None:
                    descartados['sin_sentido'] += 1
                    continue

                velocidad = pos.speed * FACTOR_VELOCIDAD_A_KMH if pos.HasField("speed") else None
                if velocidad is not None and velocidad > VEL_MAX_VALIDA_KMH:
                    velocidad = None  # error de GPS

                buses_l1.append({
                    'id': v.vehicle.id if v.HasField("vehicle") and v.vehicle.id else str(entidad.id),
                    'ruta': rid,
                    'lat': pos.latitude,
                    'lon': pos.longitude,
                    'speed': velocidad,
                    'sentido': sentido,
                })

            logging.info(f"Buses L1 usados: {len(buses_l1)} | descartados: {descartados} | "
                         f"servicios útiles para tu viaje: {sorted(rutas_utiles)}")

            # 1. TERMÓMETRO DIRECCIONAL (se calcula primero: su velocidad alimenta el ETA)
            reporte_termometro = ""
            vel_norte = vel_sur = 0.0

            def evaluar_estado_velocidad(buses_filtrados):
                con_vel = [b for b in buses_filtrados if b['speed'] is not None]
                if not con_vel:
                    return "⚪ Sin datos", 0.0
                avg_speed = sum(b['speed'] for b in con_vel) / len(con_vel)
                if avg_speed >= TERMO_FLUIDO_KMH:
                    return "🟢 Fluido", avg_speed
                elif avg_speed >= TERMO_MODERADO_KMH:
                    return "🟡 Moderado", avg_speed
                return "🔴 Tráfico Pesado", avg_speed

            if buses_l1:
                buses_n = [b for b in buses_l1 if b['sentido'] == 'N']
                buses_s = [b for b in buses_l1 if b['sentido'] == 'S']
                estado_norte, vel_norte = evaluar_estado_velocidad(buses_n)
                estado_sur, vel_sur = evaluar_estado_velocidad(buses_s)

                self.datos_historicos["Velocidad_Norte"] = round(vel_norte, 1)
                self.datos_historicos["Velocidad_Sur"] = round(vel_sur, 1)

                reporte_termometro = (
                    "🌡️ <b>TERMÓMETRO DE RUTA (L1)</b>\n"
                    f"⬆️ <b>Norte (Indios Verdes):</b> {estado_norte} ({vel_norte:.1f} km/h, {len(buses_n)} unidades)\n"
                    f"⬇️ <b>Sur (Caminero):</b> {estado_sur} ({vel_sur:.1f} km/h, {len(buses_s)} unidades)"
                )

            # 2. ASISTENTE PERSONAL: solo buses de servicios que te llevan a tu destino
            def te_sirve(b):
                return b['sentido'] == sentido_viaje and (not rutas_utiles or b['ruta'] in rutas_utiles)

            buses_utiles = []
            for b in buses_l1:
                if not te_sirve(b):
                    continue
                distancia = self.calcular_distancia(lat_origen, lon_origen, b['lat'], b['lon'])
                if es_manana:
                    if distancia <= 1.5:
                        buses_utiles.append((distancia, b))
                elif b['lat'] < lat_origen and distancia <= 6.0:  # viene del sur hacia ti
                    buses_utiles.append((distancia, b))
            buses_utiles.sort(key=lambda x: x[0])

            titulo_asis = f"🎯 <b>ASISTENTE PERSONAL (GPS)</b>\n<i>Tu viaje: {esc(estacion)} ➔ {esc(destino)}</i>\n"
            self.datos_historicos["Unidades_Cercanas"] = len(buses_utiles)

            if es_manana:
                cantidad = len(buses_utiles)
                if cantidad >= 4:
                    estado = "🟢 Excelente (Línea fluyendo)"
                elif cantidad >= 2:
                    estado = "🟡 Normal"
                else:
                    estado = "🔴 Baja disponibilidad (Posible retraso)"
                reporte_asistente = titulo_asis + f"Terminal: {estado} ({cantidad} unidades hacia {esc(destino)})."
            else:
                if not buses_utiles:
                    reporte_asistente = titulo_asis + "⚠️ No hay unidades hacia Indios Verdes acercándose en 6 km. Fuerte retraso."
                else:
                    el_proximo, bus = buses_utiles[0]
                    servicio = re.sub(r"^\S+\s+", "", info_rutas.get(bus['ruta'], {}).get('nombre', '')).title()
                    # ETA con la velocidad real hacia el norte (con tope), no una constante fija
                    vel_eta = min(vel_norte, VEL_ETA_TOPE) if vel_norte > 0 else VEL_ETA_RESPALDO
                    tiempo_min = max(1, round(el_proximo / vel_eta * 60))
                    self.datos_historicos["ETA_Minutos"] = tiempo_min
                    restantes = len(buses_utiles) - 1
                    reporte_asistente = titulo_asis + (
                        f"🚌 <b>Próximo Metrobús:</b> A {el_proximo:.1f} km"
                        f"{f' ({esc(servicio)})' if servicio else ''}.\n"
                        f"⏱️ <b>Llegada estimada:</b> ~{tiempo_min} minutos (a {vel_eta:.0f} km/h).\n"
                        f"📊 Detrás vienen {restantes} unidades más (radio 6 km)."
                    )

            # 3. HOTSPOTS (Embotellamientos reales: mismo sentido, sin duplicados, solo estaciones L1)
            hotspots = self.detectar_hotspots(buses_l1, mapa_paradas)
            self.datos_historicos["Hotspots_L1"] = len(hotspots)
            reporte_hotspots = ""
            if hotspots:
                lineas = [
                    f"- {h['flecha']} <b>{esc(h['estacion'])}</b> (hacia {'Indios Verdes' if h['sentido'] == 'Norte' else 'el sur'}): "
                    f"{h['unidades']} unidades a {h['velocidad']:.1f} km/h"
                    for h in hotspots[:HOTSPOT_MAX_MOSTRADOS]
                ]
                extra = len(hotspots) - HOTSPOT_MAX_MOSTRADOS
                if extra > 0:
                    lineas.append(f"<i>…y {extra} más.</i>")
                reporte_hotspots = "⚠️ <b>EMBOTELLAMIENTOS L1</b>\n" + "\n".join(lineas)

            if not buses_l1:
                reporte_asistente = titulo_asis + "⚠️ El GPS no reportó unidades activas de L1 en este momento."

            return reporte_asistente, reporte_termometro, reporte_hotspots

        except Exception as e:
            logging.error(f"Error en procesamiento GTFS: {e}")
            aviso = f"⚠️ <b>GPS no disponible</b> (SinopticoPlus: {esc(type(e).__name__)}). Este reporte no incluye ETA ni tráfico."
            return aviso, "", ""

    def guardar_registro_csv(self):
        nombre_archivo = "historial_metrobus.csv"
        archivo_existe = os.path.isfile(nombre_archivo)

        try:
            with open(nombre_archivo, mode='a', newline='', encoding='utf-8') as archivo:
                escritor = csv.DictWriter(archivo, fieldnames=self.datos_historicos.keys())
                if not archivo_existe:
                    escritor.writeheader()
                escritor.writerow(self.datos_historicos)
            logging.info("Registro guardado exitosamente en CSV local.")
        except Exception as e:
            logging.error(f"Error al guardar el CSV: {e}")

    def enviar_telegram(self, mensaje_html: str):
        """Envía en HTML; si Telegram rechaza el formato (400), reintenta en texto plano."""
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje_html, "parse_mode": "HTML",
                   "disable_web_page_preview": True}

        res = self.http.post(url, json=payload, timeout=15)
        if res.status_code == 400:
            logging.warning(f"Telegram rechazó el formato HTML ({res.text[:200]}). Reenviando en texto plano.")
            texto_plano = html.unescape(re.sub(r"<[^>]+>", "", mensaje_html))
            res = self.http.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": texto_plano}, timeout=15)
        res.raise_for_status()

    @staticmethod
    def _cronometrar(nombre, funcion):
        t0 = time.monotonic()
        try:
            return funcion()
        finally:
            logging.info(f"⏱️ {nombre}: {time.monotonic() - t0:.1f}s")

    def enviar_reporte_completo(self):
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("Faltan credenciales de Telegram.")
            sys.exit(1)

        # ACTIVACIÓN DEL ESCUDO ANTIRREBOTE
        if self.ejecucion_reciente():
            logging.info("🛑 Petición ignorada: Telegram envió un rebote duplicado (Filtro Antirrebote activado).")
            return

        # Las tres fuentes son independientes: se consultan en paralelo y el
        # reporte tarda lo que la más lenta, no la suma de las tres
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_oficial = pool.submit(self._cronometrar, "SEMOVI", self.obtener_estado_oficial)
            f_clima = pool.submit(self._cronometrar, "Clima", self.obtener_clima)
            f_gtfs = pool.submit(self._cronometrar, "GPS", self.procesar_datos_gtfs)
            reporte_oficial = f_oficial.result()
            reporte_clima = f_clima.result()
            reporte_asistente, reporte_termometro, reporte_hotspots = f_gtfs.result()
        logging.info(f"⏱️ Consultas completas en {time.monotonic() - t0:.1f}s")

        self.guardar_registro_csv()

        secciones = [reporte_oficial, reporte_clima, reporte_asistente, reporte_termometro, reporte_hotspots]
        mensaje_final = "\n\n".join(s for s in secciones if s)

        try:
            self.enviar_telegram(f"🚇 <b>REPORTE METROBÚS</b>\n\n{mensaje_final}")
            logging.info("Telegram enviado correctamente.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error al enviar Telegram: {e}")
            sys.exit(1)


if __name__ == "__main__":
    url_directa = "https://incidentesmovilidad.cdmx.gob.mx/public/bandejaEstadoServicio.xhtml?idMedioTransporte=mb"
    monitor = MetrobusMonitor(url_directa)
    monitor.enviar_reporte_completo()
