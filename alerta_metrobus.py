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
        
        # Mapeo de las filas a las líneas del Metrobús (Fila 4 y 5 son Línea 4)
        nombres_lineas = [
            "Línea 1",
            "Línea 2",
            "Línea 3",
            "Línea 4", 
            "Línea 4", 
            "Línea 5",
            "Línea 6",
            "Línea 7"
        ]

        try:
            logging.info("Consultando la página a través de ScraperAPI...")
            parametros_proxy = {
                'api_key': SCRAPER_API_KEY,
                'url': self.url,
                'country_code': 'mx',
                'render': 'true' # Forzamos el renderizado JS para que cargue la tabla
            }
            
            # El timeout sube a 60 porque ScraperAPI tarda un poco en procesar el proxy
            respuesta = requests.get('http://api.scraperapi.com/', params=parametros_proxy, timeout=60)
            respuesta.raise_for_status()
            
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            tablas = soup.find_all('table')

            for tabla in tablas:
                if 'estaciones afectadas' in tabla.text.lower():
                    filas = tabla.find_all('tr')[1:]
                    
                    for i, fila in enumerate(filas):
                        celdas = fila.find_all('td')
                        
                        if len(celdas) >= 4:
                            # Asignamos el nombre de la línea
                            if i < len(nombres_lineas):
                                linea = nombres_lineas[i]
                            else:
                                linea = f"Línea Desconocida (Fila {i+1})"
                                
                            est = celdas[1].get_text(strip=True)
                            afec = celdas[2].get_text(strip=True)
                            info_adicional = celdas[3].get_text(strip=True)
                            
                            est_limpio = est.lower().replace("estado", "").strip()
                            afec_limpio = afec.lower().replace("estaciones afectadas", "").strip()
                            info_limpia = info_adicional.replace("Información adicional", "").strip()
                            
                            if "servicio regular" not in est_limpio or "ninguna" not in afec_limpio:
                                mensaje_afectacion = f"- {linea}: {est} | Cerradas: {afec}"
                                
                                if info_limpia:
                                    mensaje_afectacion += f" | Info: {info_limpia}"
                                    
                                problemas.append(mensaje_afectacion)
            
            return "Servicio Regular. Todo en orden." if not problemas else "AFECTACIONES DETECTADAS:\n" + "\n".join(problemas)

        except requests.exceptions.RequestException as e:
            return f"Error de conexión con ScraperAPI: {str(e)}"
        except Exception as e:
            return f"Error inesperado en la extracción: {str(e)}"

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
    monitor = MetrobusMonitor("https://incidentesmovilidad.cdmx.gob.mx/public/bandejaEstadoServicio.xhtml?idMedioTransporte=mb")
    monitor.enviar_telegram(monitor.obtener_estado_detallado())
