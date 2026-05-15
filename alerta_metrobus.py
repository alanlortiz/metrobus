import os
import logging
import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url: str):
        self.url = url

    def obtener_estado_detallado(self) -> str:
        if not SCRAPER_API_KEY:
            return "Error: Falta la clave de ScraperAPI en los Secrets."

        problemas = []
        try:
            logging.info("Consultando la página del Metrobús vía ScraperAPI...")
            parametros_proxy = {
                'api_key': SCRAPER_API_KEY,
                'url': self.url,
                'country_code': 'mx'
            }
            
            respuesta = requests.get('http://api.scraperapi.com/', params=parametros_proxy, timeout=60)
            respuesta.raise_for_status()
            
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            tablas = soup.find_all('table')

            # Diccionario de mapeo basado estrictamente en tu regla de posición de filas
            mapeo_lineas = {
                1: "Línea 1",
                2: "Línea 2",
                3: "Línea 3",
                4: "Línea 4",
                5: "Línea 4",  # Excepción: La fila 5 también pertenece a la Línea 4
                6: "Línea 5",
                7: "Línea 6",
                8: "Línea 7"
            }

            for tabla in tablas:
                if 'estaciones afectadas' in tabla.text.lower():
                    # Usamos enumerate empezando en 1 para contar las filas de datos reales
                    for num_fila, fila in enumerate(tabla.find_all('tr')[1:], start=1):
                        celdas = fila.find_all('td')
                        
                        if len(celdas) >= 3:
                            # Asignamos el nombre de la línea según el número de fila actual
                            linea_nombre = mapeo_lineas.get(num_fila, f"Línea {num_fila}")
                            
                            est = celdas[1].get_text(strip=True)
                            afec = celdas[2].get_text(strip=True)
                            
                            est_limpio = est.lower().replace("estado", "").strip()
                            afec_limpio = afec.lower().replace("estaciones afectadas", "").strip()
                            
                            # Extraemos la cuarta columna (Información adicional) si existe
                            info_adicional = ""
                            if len(celdas) >= 4:
                                info_adicional = celdas[3].get_text(strip=True).replace("Información adicional", "").strip()
                            
                            if "servicio regular" not in est_limpio or "ninguna" not in afec_limpio:
                                # Construimos el renglón base de la afectación
                                reporte_linea = f"- *{linea_nombre}*: {est} | {afec}"
                                
                                # Si hay información adicional válida, la agregamos al final del reporte
                                if info_adicional and info_adicional.lower() != "ninguna":
                                    reporte_linea += f" | Info: {info_adicional}"
                                    
                                problemas.append(reporte_linea)
            
            return "Servicio Regular. Todo en orden." if not problemas else "AFECTACIONES DETECTADAS:\n" + "\n".join(problemas)

        except Exception as e:
            return f"Error en la extracción: {str(e)}"

    def enviar_telegram(self, mensaje: str) -> None:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("ERROR CRÍTICO: Faltan credenciales de Telegram.")
            exit(1)
            
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"*REPORTE METROBÚS*\n\n{mensaje}",
            "parse_mode": "Markdown"
        }
        try:
            respuesta = requests.post(url, json=payload, timeout=15)
            respuesta.raise_for_status()
            logging.info("Telegram enviado correctamente.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error al enviar Telegram: {str(e)}")
            exit(1)

if __name__ == "__main__":
    url_directa = "https://incidentesmovilidad.cdmx.gob.mx/public/bandejaEstadoServicio.xhtml?idMedioTransporte=mb"
    monitor = MetrobusMonitor(url_directa)
    monitor.enviar_telegram(monitor.obtener_estado_detallado())
