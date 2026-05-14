import os
import logging
import urllib.parse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

MI_NUMERO = os.getenv("MI_NUMERO")
API_KEY = os.getenv("API_KEY")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url: str):
        self.url = url
        self.sesion = self._configurar_sesion_robusta()

    def _configurar_sesion_robusta(self):
        """Configura una sesión con reintentos automáticos para lidiar con errores 500 del proxy."""
        sesion = requests.Session()
        # Intentará 3 veces, esperando un poco más entre cada fallo (backoff_factor)
        estrategia_reintento = Retry(
            total=3,
            backoff_factor=2, 
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adaptador = HTTPAdapter(max_retries=estrategia_reintento)
        sesion.mount("http://", adaptador)
        sesion.mount("https://", adaptador)
        return sesion

    def obtener_estado_detallado(self) -> str:
        if not SCRAPER_API_KEY:
            return "❌ Error: Falta la clave de ScraperAPI."

        try:
            parametros_proxy = {
                'api_key': SCRAPER_API_KEY,
                'url': self.url,
                'country_code': 'mx',
                'render': 'true'
                # NOTA: 'premium': 'true' fue eliminado para evitar el Error 500 en cuentas estándar
            }
            
            logging.info("Consultando el Metrobús a través de ScraperAPI...")
            respuesta = self.sesion.get('http://api.scraperapi.com/', params=parametros_proxy, timeout=60)
            respuesta.raise_for_status()
            
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            problemas = []
            tablas = soup.find_all('table')
            
            if not tablas:
                logging.warning("El proxy devolvió la página, pero no se encontraron tablas. El renderizado JS podría estar fallando.")

            for tabla in tablas:
                if 'estaciones afectadas' in tabla.text.lower():
                    for fila in tabla.find_all('tr')[1:]:
                        celdas = fila.find_all('td')
                        if len(celdas) >= 3:
                            linea = celdas[0].get_text(strip=True)
                            est = celdas[1].get_text(strip=True)
                            afec = celdas[2].get_text(strip=True)
                            
                            est_limpio = est.lower().replace("estado", "").strip()
                            afec_limpio = afec.lower().replace("estaciones afectadas", "").strip()
                            
                            if "servicio regular" not in est_limpio or "ninguna" not in afec_limpio:
                                problemas.append(f"• {linea}: {est} | {afec}")
            
            return "✅ *Servicio Regular*" if not problemas else "⚠️ *AFECTACIONES DETECTADAS:*\n" + "\n".join(problemas)
        
        except requests.exceptions.RequestException as e:
            return f"❌ Error de conexión tras varios intentos: {str(e)}"
        except Exception as e:
            return f"❌ Error inesperado analizando la web: {str(e)}"

    def enviar_whatsapp(self, mensaje: str) -> None:
        if not MI_NUMERO or not API_KEY:
            logging.error("Faltan credenciales de CallMeBot.")
            return
        
        msg_codificado = urllib.parse.quote(f"🚇 *REPORTE METROBÚS*\n\n{mensaje}")
        url = f"https://api.callmebot.com/whatsapp.php?phone={MI_NUMERO}&text={msg_codificado}&apikey={API_KEY}"
        
        try:
            # También usamos la sesión robusta para asegurar el envío del mensaje
            respuesta = self.sesion.get(url, timeout=15)
            respuesta.raise_for_status()
            logging.info("WhatsApp enviado correctamente.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Fallo al enviar el WhatsApp: {str(e)}")

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    monitor.enviar_whatsapp(monitor.obtener_estado_detallado())
