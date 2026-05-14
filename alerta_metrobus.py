import os
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup

MI_NUMERO = os.getenv("MI_NUMERO")
API_KEY = os.getenv("API_KEY")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url: str):
        self.url = url

    def obtener_estado_detallado(self) -> str:
        if not SCRAPER_API_KEY:
            return "Error: Falta la clave de ScraperAPI en los Secrets de GitHub."

        try:
            parametros_proxy = {
                'api_key': SCRAPER_API_KEY,
                'url': self.url,
                'premium': 'true',
                'country_code': 'mx',
                'render': 'true'  # <-- CORRECCIÓN 1: Forzamos a ScraperAPI a ejecutar JavaScript
            }
            
            respuesta = requests.get('http://api.scraperapi.com/', params=parametros_proxy, timeout=60)
            respuesta.raise_for_status()
            
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            problemas = []
            tablas = soup.find_all('table')
            
            # Verificación de seguridad para los logs en GitHub Actions
            if not tablas:
                logging.warning("No se detectaron tablas. La estructura del sitio pudo cambiar.")

            for tabla in tablas:
                # CORRECCIÓN 2: Búsqueda robusta insensible a mayúsculas
                if 'estaciones afectadas' in tabla.text.lower():
                    for fila in tabla.find_all('tr')[1:]:
                        celdas = fila.find_all('td')
                        if len(celdas) >= 3:
                            linea = celdas[0].get_text(strip=True)
                            est = celdas[1].get_text(strip=True)
                            afec = celdas[2].get_text(strip=True)
                            
                            # CORRECCIÓN 3: Limpieza profunda antes de la validación lógica
                            est_limpio = est.lower().replace("estado", "").strip()
                            afec_limpio = afec.lower().replace("estaciones afectadas", "").strip()
                            
                            if "servicio regular" not in est_limpio or "ninguna" not in afec_limpio:
                                problemas.append(f"• {linea}: {est} | {afec}")
            
            return "*Servicio Regular*" if not problemas else " *AFECTACIONES DETECTADAS:*\n" + "\n".join(problemas)
        
        except requests.exceptions.RequestException as e:
            return f"Error de conexión con el Metrobús: {str(e)}"
        except Exception as e:
            return f"Error inesperado: {str(e)}"

    def enviar_whatsapp(self, mensaje: str) -> None:
        if not MI_NUMERO or not API_KEY:
            logging.error("Faltan las credenciales de WhatsApp en los Secrets.")
            return
        
        msg_codificado = urllib.parse.quote(f"🚇 *REPORTE METROBÚS*\n\n{mensaje}")
        url = f"https://api.callmebot.com/whatsapp.php?phone={MI_NUMERO}&text={msg_codificado}&apikey={API_KEY}"
        
        try:
            respuesta = requests.get(url)
            respuesta.raise_for_status()
            logging.info("WhatsApp enviado correctamente.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Fallo al enviar el WhatsApp a través de CallMeBot: {str(e)}")

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    monitor.enviar_whatsapp(monitor.obtener_estado_detallado())
