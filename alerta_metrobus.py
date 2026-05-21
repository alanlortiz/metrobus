import os
import logging
import requests
import zipfile
import io
import csv
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

    def obtener_estado_oficial(self) -> str:
        """Extrae las alertas de texto oficiales usando ScraperAPI"""
        if not SCRAPER_API_KEY:
            return "Error: Falta la clave de ScraperAPI."

        problemas = []
        try:
            logging.info("Consultando la página de SEMOVI vía ScraperAPI...")
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

    def obtener_radar_gtfs(self) -> str:
        """Se conecta a la API de Sonda, mapea IDs y cuenta camiones detenidos"""
        if not USUARIO or not SENHA:
            logging.error("Faltan las credenciales de GTFS Sonda en los Secrets.")
            return ""

        try:
            logging.info("Autenticando en API GTFS de Sonda...")
            url_auth = "https://metrobus-gtfs.sinopticoplus.com/gtfs-api/partnerValidation"
            
            # CORRECCIÓN AQUÍ: Quitamos las comillas para usar las variables reales
            credenciales = {"usuario": USUARIO, "senha": SENHA}
            
            auth_res = requests.post(url_auth, json=credenciales, timeout=15)
            auth_res.raise_for_status()
            urls = auth_res.json()
            
            logging.info("Descargando mapa de rutas (Estático)...")
            zip_res = requests.get(urls['urlStatic'], timeout=30)
            mapa_rutas = {}
            with zipfile.ZipFile(io.BytesIO(zip_res.content)) as z:
                with z.open('routes.txt') as f:
                    lector = csv.DictReader(io.TextIOWrapper(f, 'utf-8'))
                    for fila in lector:
                        nombre_corto = fila.get('route_short_name', '').strip()
                        mapa_rutas[fila['route_id']] = f"Línea {nombre_corto}" if nombre_corto else "Línea Desconocida"

            logging.info("Descargando radar de autobuses (Realtime)...")
            rt_res = requests.get(urls['urlRealTime'], timeout=30)
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(rt_res.content)
            
            estadisticas = {}
            for entidad in feed.entity:
                if entidad.vehicle.HasField("trip") and entidad.vehicle.HasField("position"):
                    r_id = entidad.vehicle.trip.route_id
                    nombre_linea = mapa_rutas.get(r_id, "Línea Desconocida")
                    
                    if nombre_linea not in estadisticas:
                        estadisticas[nombre_linea] = {"total": 0, "detenidos": 0}
                        
                    estadisticas[nombre_linea]["total"] += 1
                    
                    if entidad.vehicle.position.speed == 0:
                        estadisticas[nombre_linea]["detenidos"] += 1

            alertas_radar = []
            for linea in sorted(estadisticas.keys()):
                stats = estadisticas[linea]
                if stats["detenidos"] >= 6 and linea != "Línea Desconocida":
                    alertas_radar.append(f"- 📡 *{linea}*: {stats['detenidos']} de {stats['total']} autobuses sin movimiento.")

            if alertas_radar:
                return "🚦 *RADAR EN VIVO (GPS):*\n_Anomalías de tráfico detectadas_\n" + "\n".join(alertas_radar)
            else:
                return "🚦 *RADAR EN VIVO (GPS):* Flujo vehicular normal en todas las líneas."

        except Exception as e:
            logging.error(f"Error en Radar GTFS: {str(e)}")
            return "" 

    def enviar_reporte_completo(self):
        reporte_oficial = self.obtener_estado_oficial()
        reporte_radar = self.obtener_radar_gtfs()
        
        mensaje_final = f"{reporte_oficial}\n\n{reporte_radar}"
        
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
