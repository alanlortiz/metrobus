import os
import logging
import requests
import zipfile
import io
import csv
import math
import datetime
from bs4 import BeautifulSoup
from google.transit import gtfs_realtime_pb2

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
USUARIO = os.getenv("USUARIO_API_KEY")
SENHA = os.getenv("SENHA_API_KEY")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url_semovi: str):
        self.url_semovi = url_semovi

    @staticmethod
    def calcular_distancia(lat1, lon1, lat2, lon2):
        """Calcula la distancia en línea recta (en km) entre dos coordenadas usando la fórmula de Haversine."""
        R = 6371.0 
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    def obtener_estado_oficial(self) -> str:
        """Extrae las alertas oficiales de la web del gobierno"""
        if not SCRAPER_API_KEY:
            return "Error: Falta la clave de ScraperAPI."

        problemas = []
        try:
            parametros_proxy = {
                'api_key': SCRAPER_API_KEY,
                'url': self.url_semovi,
                'country_code': 'mx'
            }
            
            respuesta = requests.get('http://api.scraperapi.com/', params=parametros_proxy, timeout=60)
            respuesta.raise_for_status()
            
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            tablas = soup.find_all('table')

            mapeo_lineas = {
                1: "Línea 1", 2: "Línea 2", 3: "Línea 3", 
                4: "Línea 4", 5: "Línea 4", 6: "Línea 5", 
                7: "Línea 6", 8: "Línea 7"
            }

            for tabla in tablas:
                if 'estaciones afectadas' in tabla.text.lower():
                    for num_fila, fila in enumerate(tabla.find_all('tr')[1:], start=1):
                        celdas = fila.find_all('td')
                        
                        if len(celdas) >= 3:
                            linea_nombre = mapeo_lineas.get(num_fila, f"Línea {num_fila}")
                            est = celdas[1].get_text(strip=True)
                            afec = celdas[2].get_text(strip=True)
                            
                            est_limpio = est.lower().replace("estado", "").strip()
                            afec_limpio = afec.lower().replace("estaciones afectadas", "").strip()
                            
                            info_adicional = ""
                            if len(celdas) >= 4:
                                info_adicional = celdas[3].get_text(strip=True).replace("Información adicional", "").strip()
                            
                            if "servicio regular" not in est_limpio or "ninguna" not in afec_limpio:
                                reporte_linea = f"- *{linea_nombre}*: {est} | {afec}"
                                if info_adicional and info_adicional.lower() != "ninguna":
                                    reporte_linea += f" | Info: {info_adicional}"
                                problemas.append(reporte_linea)
            
            return "Servicio Regular. Todo en orden." if not problemas else "*AFECTACIONES DETECTADAS (Oficial):*\n" + "\n".join(problemas)

        except Exception as e:
            return f"Error en extracción SEMOVI: {str(e)}"

    def obtener_asistente_personal(self) -> str:
        """Calcula el tiempo de llegada (ETA) de los autobuses a tus estaciones específicas"""
        if not USUARIO or not SENHA:
            return ""

        try:
            url_auth = "https://metrobus-gtfs.sinopticoplus.com/gtfs-api/partnerValidation"
            credenciales = {"usuario": USUARIO, "senha": SENHA}
            
            auth_res = requests.post(url_auth, json=credenciales, timeout=15)
            auth_res.raise_for_status()
            urls = auth_res.json()
            
            zip_res = requests.get(urls['urlStatic'], timeout=30)
            mapa_rutas = {}
            with zipfile.ZipFile(io.BytesIO(zip_res.content)) as z:
                with z.open('routes.txt') as f:
                    lector = csv.DictReader(io.TextIOWrapper(f, 'utf-8'))
                    for fila in lector:
                        nombre_corto = fila.get('route_short_name', '').strip()
                        mapa_rutas[fila['route_id']] = f"Línea {nombre_corto}" if nombre_corto else ""

            rt_res = requests.get(urls['urlRealTime'], timeout=30)
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(rt_res.content)
            
            hora_cdmx = (datetime.datetime.utcnow() - datetime.timedelta(hours=6)).hour
            es_manana = hora_cdmx < 12 

            if es_manana:
                estacion = "Indios Verdes"
                destino = "Poliforum"
                lat_origen, lon_origen = 19.4954, -99.1195
            else:
                estacion = "Poliforum"
                destino = "Indios Verdes"
                lat_origen, lon_origen = 19.3946, -99.1746

            buses_utiles = []
            
            for entidad in feed.entity:
                if entidad.vehicle.HasField("trip") and entidad.vehicle.HasField("position"):
                    r_id = entidad.vehicle.trip.route_id
                    
                    if mapa_rutas.get(r_id) == "Línea 1":
                        lat_bus = entidad.vehicle.position.latitude
                        lon_bus = entidad.vehicle.position.longitude
                        bearing = entidad.vehicle.position.bearing 
                        
                        distancia = self.calcular_distancia(lat_origen, lon_origen, lat_bus, lon_bus)
                        
                        if es_manana:
                            if distancia <= 1.5:
                                buses_utiles.append(distancia)
                        else:
                            va_al_norte = (bearing < 90 or bearing > 270)
                            esta_al_sur = lat_bus < lat_origen
                            
                            if va_al_norte and esta_al_sur and distancia <= 6.0:
                                buses_utiles.append(distancia)

            buses_utiles.sort()
            
            titulo = f"🎯 *ASISTENTE PERSONAL (GPS en vivo)*\n_Tu viaje: {estacion} ➔ {destino}_\n"
            
            if es_manana:
                cantidad = len(buses_utiles)
                if cantidad >= 4:
                    estado = "🟢 Excelente (Línea fluyendo)"
                elif cantidad >= 2:
                    estado = "🟡 Normal"
                else:
                    estado = "🔴 Baja disponibilidad (Posible retraso para abordar)"
                    
                return titulo + f"Terminal: {estado} ({cantidad} unidades en zona de abordaje)."
                
            else:
                if not buses_utiles:
                    return titulo + "⚠️ No se detectan unidades acercándose en 6km. Fuerte retraso en Línea 1."
                    
                el_proximo = buses_utiles[0]
                tiempo_min = max(1, int(el_proximo * 3.75)) 
                
                msg = titulo + f"🚌 *Próximo Metrobús:* A {el_proximo:.1f} km de distancia.\n"
                msg += f"⏱️ *Llegada estimada:* ~{tiempo_min} minutos.\n"
                msg += f"📊 Vienen {len(buses_utiles)} unidades más en camino (radio de 6km)."
                return msg

        except Exception as e:
            logging.error(f"Error en Asistente GTFS: {str(e)}")
            return ""

    def enviar_reporte_completo(self):
        reporte_oficial = self.obtener_estado_oficial()
        reporte_asistente = self.obtener_asistente_personal()
        
        mensaje_final = f"{reporte_oficial}"
        if reporte_asistente:
            mensaje_final += f"\n\n{reporte_asistente}"
        
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("ERROR CRÍTICO: Faltan credenciales de Telegram.")
            exit(1)
            
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"🚇 *REPORTE METROBÚS*\n\n{mensaje_final}",
            "parse_mode": "Markdown"
        }
        
        try:
            logging.info("Enviando reporte a Telegram...")
            respuesta = requests.post(url, json=payload, timeout=15)
            respuesta.raise_for_status()
            logging.info("✅ Telegram enviado correctamente.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error al enviar Telegram: {str(e)}")
            exit(1)

if __name__ == "__main__":
    url_directa = "https://incidentesmovilidad.cdmx.gob.mx/public/bandejaEstadoServicio.xhtml?idMedioTransporte=mb"
    monitor = MetrobusMonitor(url_directa)
    monitor.enviar_reporte_completo()
