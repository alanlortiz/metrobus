import os
import re
import sys
import time
import html
import logging
import requests
import zipfile
import io
import csv
import math
import datetime
from zoneinfo import ZoneInfo
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
# Tope y respaldo de velocidad para el ETA (km/h)
VEL_ETA_TOPE = 16.0
VEL_ETA_RESPALDO = 12.0

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

    def procesar_datos_gtfs(self) -> tuple:
        if not USUARIO or not SENHA:
            return "", "", ""

        try:
            url_auth = "https://metrobus-gtfs.sinopticoplus.com/gtfs-api/partnerValidation"
            auth_res = self.http.post(url_auth, json={"usuario": USUARIO, "senha": SENHA}, timeout=15)
            auth_res.raise_for_status()
            urls = auth_res.json()

            zip_res = self.http.get(urls['urlStatic'], timeout=30)
            zip_res.raise_for_status()
            mapa_rutas, mapa_paradas = {}, []

            with zipfile.ZipFile(io.BytesIO(zip_res.content)) as z:
                with z.open('routes.txt') as f:
                    for fila in csv.DictReader(io.TextIOWrapper(f, 'utf-8-sig')):
                        nombre_corto = fila.get('route_short_name', '').strip()
                        mapa_rutas[fila['route_id']] = f"Línea {nombre_corto}" if nombre_corto else ""
                with z.open('stops.txt') as f:
                    for fila in csv.DictReader(io.TextIOWrapper(f, 'utf-8-sig')):
                        try:
                            mapa_paradas.append({
                                'nombre': fila.get('stop_name', 'Estación Desconocida'),
                                'lat': float(fila['stop_lat']),
                                'lon': float(fila['stop_lon'])
                            })
                        except (KeyError, ValueError):
                            continue

            rt_res = self.http.get(urls['urlRealTime'], timeout=30)
            rt_res.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(rt_res.content)

            # Referencia de tiempo: la del feed si viene, si no la hora actual
            ahora_ref = feed.header.timestamp if feed.header.HasField("timestamp") else int(time.time())

            es_manana = self.datos_historicos["Turno"] == "Mañana"

            if es_manana:
                estacion, destino = "Indios Verdes", "Poliforum"
                lat_origen, lon_origen = 19.4954, -99.1195
            else:
                estacion, destino = "Poliforum", "Indios Verdes"
                lat_origen, lon_origen = 19.3946, -99.1746

            buses_por_ruta = {}
            descartados_viejos = 0

            for entidad in feed.entity:
                v = entidad.vehicle
                if not (entidad.HasField("vehicle") and v.HasField("trip") and v.HasField("position")):
                    continue

                # Descarta posiciones viejas (buses apagados o en patio)
                if v.HasField("timestamp") and (ahora_ref - v.timestamp) > MAX_ANTIGUEDAD_GPS_SEG:
                    descartados_viejos += 1
                    continue

                nombre_linea = mapa_rutas.get(v.trip.route_id, "Línea Desconocida")
                if not nombre_linea:
                    continue

                pos = v.position
                # Sin HasField, protobuf devuelve 0: el bus "iría al norte" y "estaría parado"
                bearing = pos.bearing if pos.HasField("bearing") else None
                velocidad = pos.speed * 3.6 if pos.HasField("speed") else None
                if velocidad is not None and velocidad > VEL_MAX_VALIDA_KMH:
                    velocidad = None  # error de GPS

                buses_por_ruta.setdefault(nombre_linea, []).append({
                    'id': v.vehicle.id if v.HasField("vehicle") and v.vehicle.id else str(entidad.id),
                    'lat': pos.latitude,
                    'lon': pos.longitude,
                    'speed': velocidad,
                    'bearing': bearing,
                })

            if descartados_viejos:
                logging.info(f"Descartadas {descartados_viejos} posiciones GPS con más de {MAX_ANTIGUEDAD_GPS_SEG}s de antigüedad.")

            buses_l1 = buses_por_ruta.get(MI_LINEA, [])

            def va_norte(b):
                return b['bearing'] is not None and (b['bearing'] < 90 or b['bearing'] > 270)

            def va_sur(b):
                return b['bearing'] is not None and 90 <= b['bearing'] <= 270

            # 1. TERMÓMETRO DIRECCIONAL (se calcula primero: su velocidad alimenta el ETA)
            reporte_termometro = ""
            vel_norte = vel_sur = 0.0

            def evaluar_estado_velocidad(buses_filtrados):
                con_vel = [b for b in buses_filtrados if b['speed'] is not None]
                if not con_vel:
                    return "⚪ Sin datos", 0.0
                avg_speed = sum(b['speed'] for b in con_vel) / len(con_vel)
                if avg_speed >= 14.0:
                    return "🟢 Fluido", avg_speed
                elif avg_speed >= 10.0:
                    return "🟡 Moderado", avg_speed
                return "🔴 Tráfico Pesado", avg_speed

            if buses_l1:
                estado_norte, vel_norte = evaluar_estado_velocidad([b for b in buses_l1 if va_norte(b)])
                estado_sur, vel_sur = evaluar_estado_velocidad([b for b in buses_l1 if va_sur(b)])

                self.datos_historicos["Velocidad_Norte"] = round(vel_norte, 1)
                self.datos_historicos["Velocidad_Sur"] = round(vel_sur, 1)

                reporte_termometro = (
                    "🌡️ <b>TERMÓMETRO DE RUTA (L1)</b>\n"
                    f"⬆️ <b>Norte (Indios Verdes):</b> {estado_norte} ({vel_norte:.1f} km/h)\n"
                    f"⬇️ <b>Sur (Caminero):</b> {estado_sur} ({vel_sur:.1f} km/h)"
                )

            # 2. ASISTENTE PERSONAL
            buses_utiles = []
            for b in buses_l1:
                distancia = self.calcular_distancia(lat_origen, lon_origen, b['lat'], b['lon'])
                if es_manana:
                    if distancia <= 1.5:
                        buses_utiles.append(distancia)
                else:
                    if va_norte(b) and b['lat'] < lat_origen and distancia <= 6.0:
                        buses_utiles.append(distancia)
            buses_utiles.sort()

            titulo_asis = f"🎯 <b>ASISTENTE PERSONAL (GPS)</b>\n<i>Tu viaje: {estacion} ➔ {destino}</i>\n"
            self.datos_historicos["Unidades_Cercanas"] = len(buses_utiles)

            if es_manana:
                cantidad = len(buses_utiles)
                if cantidad >= 4:
                    estado = "🟢 Excelente (Línea fluyendo)"
                elif cantidad >= 2:
                    estado = "🟡 Normal"
                else:
                    estado = "🔴 Baja disponibilidad (Posible retraso)"
                reporte_asistente = titulo_asis + f"Terminal: {estado} ({cantidad} unidades listas)."
            else:
                if not buses_utiles:
                    reporte_asistente = titulo_asis + "⚠️ No hay unidades acercándose en 6km. Fuerte retraso."
                else:
                    el_proximo = buses_utiles[0]
                    # ETA con la velocidad real hacia el norte (con tope), no una constante fija
                    vel_eta = min(vel_norte, VEL_ETA_TOPE) if vel_norte > 0 else VEL_ETA_RESPALDO
                    tiempo_min = max(1, round(el_proximo / vel_eta * 60))
                    self.datos_historicos["ETA_Minutos"] = tiempo_min
                    restantes = len(buses_utiles) - 1
                    reporte_asistente = titulo_asis + (
                        f"🚌 <b>Próximo Metrobús:</b> A {el_proximo:.1f} km.\n"
                        f"⏱️ <b>Llegada estimada:</b> ~{tiempo_min} minutos (a {vel_eta:.0f} km/h).\n"
                        f"📊 Vienen {restantes} unidades más en camino (radio 6km)."
                    )

            # 3. HOTSPOTS (Embotellamientos)
            hotspots_msg = []
            terminales_ignoradas = ["indios verdes", "caminero", "gálvez", "colonia del valle",
                                    "tepalcates", "tacubaya", "etiopía", "tenayuca", "santa cruz atoyac",
                                    "balderas", "buenavista", "san lázaro", "pantitlán", "alameda oriente",
                                    "remedios", "preparatoria 1", "rosario", "villa de aragón", "hospital infantil", "campo marte"]

            buses_procesados = set()
            for i, bus_origen in enumerate(buses_l1):
                if bus_origen['id'] in buses_procesados:
                    continue
                cluster = [bus_origen]
                for j, bus_destino in enumerate(buses_l1):
                    if i == j or bus_destino['id'] in buses_procesados:
                        continue
                    dist = self.calcular_distancia(bus_origen['lat'], bus_origen['lon'], bus_destino['lat'], bus_destino['lon'])
                    if dist <= 0.4:
                        cluster.append(bus_destino)

                if len(cluster) >= 4:
                    cluster_valido = [b for b in cluster if b['speed'] is not None]
                    if not cluster_valido:
                        continue

                    avg_speed = sum(b['speed'] for b in cluster_valido) / len(cluster_valido)
                    if avg_speed < 12.0:
                        centro_lat = sum(b['lat'] for b in cluster_valido) / len(cluster_valido)
                        centro_lon = sum(b['lon'] for b in cluster_valido) / len(cluster_valido)

                        estacion_cercana = "Desconocida"
                        min_dist = 999.0
                        for parada in mapa_paradas:
                            d = self.calcular_distancia(centro_lat, centro_lon, parada['lat'], parada['lon'])
                            if d < min_dist:
                                min_dist = d
                                estacion_cercana = parada['nombre']

                        if not any(t in estacion_cercana.lower() for t in terminales_ignoradas):
                            hotspots_msg.append(
                                f"- 🚨 Aglomeración ({len(cluster_valido)} unidades) cerca de "
                                f"<b>{esc(estacion_cercana)}</b>. Vel: {avg_speed:.1f} km/h."
                            )
                            self.datos_historicos["Hotspots_L1"] += 1
                            for b in cluster_valido:
                                buses_procesados.add(b['id'])

            reporte_hotspots = ("⚠️ <b>ALERTAS DE TRÁFICO (Hotspots L1)</b>\n" + "\n".join(hotspots_msg)) if hotspots_msg else ""

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

    def enviar_reporte_completo(self):
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("Faltan credenciales de Telegram.")
            sys.exit(1)

        # ACTIVACIÓN DEL ESCUDO ANTIRREBOTE
        if self.ejecucion_reciente():
            logging.info("🛑 Petición ignorada: Telegram envió un rebote duplicado (Filtro Antirrebote activado).")
            return

        reporte_oficial = self.obtener_estado_oficial()
        reporte_clima = self.obtener_clima()
        reporte_asistente, reporte_termometro, reporte_hotspots = self.procesar_datos_gtfs()

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
