import os
import logging
import requests
from bs4 import BeautifulSoup

# Credenciales desde los Secrets de GitHub
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MetrobusMonitor:
    def __init__(self, url: str):
        self.url = url

    def obtener_estado_detallado(self) -> str:
        if not SCRAPER_API_KEY:
            return "❌ Error: Falta la clave de ScraperAPI en los Secrets."

        problemas = []
        try:
            logging.info("Consultando Metrobús a través de ScraperAPI (Modo ligero)...")
            
            # Usamos el proxy en modo básico para no trabar la conexión
            parametros = {
                'api_key': SCRAPER_API_KEY,
                'url': self.url,
                'country_code': 'mx'
            }
            
            respuesta = requests.get('http://api.scraperapi.com/', params=parametros, timeout=45)
            respuesta.raise_for_status()
            
            logging.info("Analizando el HTML...")
            soup = BeautifulSoup(respuesta.text, 'html.parser')
            tablas = soup.find_all('table')

            if not tablas:
                logging.warning("No se detectaron tablas. El portal pudo cambiar.")

            for tabla in tablas:
                if 'estaciones afectadas' in tabla.text.lower():
                    for fila in tabla.find_all('tr')[1:]:
                        celdas = fila.find_all('td')
                        if len(celdas) >= 3:
                            linea = celdas[0].get_text(strip=True)
                            est = celdas[1].get_text(strip=True)
                            afec = celdas[2].get_text(strip=True)
                            
                            # Limpieza profunda de texto para evitar falsos positivos
                            est_limpio = est.lower().replace("estado", "").strip()
                            afec_limpio = afec.lower().replace("estaciones afectadas", "").strip()
                            
                            if "servicio regular" not in est_limpio or "ninguna" not in afec_limpio:
                                problemas.append(f"- {linea}: {est} | {afec}")
            
            return "Servicio Regular. Todo en orden." if not problemas else "AFECTACIONES DETECTADAS:\n" + "\n".join(problemas)

        except Exception as e:
            return f"❌ Error leyendo la página a través del proxy: {str(e)}"

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
            requests.post(url, json=payload, timeout=15)
            logging.info("Telegram enviado correctamente.")
        except requests.exceptions.RequestException as e:
            logging.error(f"Fallo de red al conectar con Telegram: {str(e)}")
            exit(1)

if __name__ == "__main__":
    monitor = MetrobusMonitor("https://www.metrobus.cdmx.gob.mx/ServicioMB")
    monitor.enviar_telegram(monitor.obtener_estado_detallado())
